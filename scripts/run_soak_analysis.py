#!/usr/bin/env python3
# Scope: BOT_RUNTIME — One-shot soak analysis battery + dated markdown summary.
#
# Runs the full post-soak battery in one pass: DQ gate, evidence-pack export,
# candidate ranking + fee sensitivity, the survival deep-dive (transient vs basis),
# recurrence/timing, depth/size curve, directional bias, and a cross-soak
# consistency check. Writes a dated summary markdown (soak_analysis_<date>.md).
#
# Public-data only: every figure is a research/candidate signal, never a trade or
# realized-profit claim. Real fills, slippage, and net-of-real-fee profit require
# authenticated execution data this analysis cannot see.
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup

_ROW_CAP = 400_000
_COMPARE_CAP = 200_000


def _edge_rows(data_dir: Path, cap: int = _ROW_CAP) -> list[dict]:
    from arbx.analysis.episodes import iter_edge_rows

    edge_dir = data_dir / "raw" / "edge"
    rows: list[dict] = []
    if edge_dir.is_dir():
        for f in sorted(edge_dir.glob("*.jsonl")):
            for row in iter_edge_rows(f):
                rows.append(row)
                if len(rows) >= cap:
                    return rows
    return rows


def _resolve_run_id(data_dir: Path, rows: list[dict]) -> str:
    latency = data_dir / "raw" / "latency"
    if latency.is_dir():
        for f in sorted(latency.glob("*.jsonl"), reverse=True):
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("kind") in {"recorder_heartbeat", "recorder_start"} and ev.get("run_id"):
                    return str(ev["run_id"])
    for r in rows:
        if r.get("run_id") and not str(r["run_id"]).startswith("probe_"):
            return str(r["run_id"])
    return data_dir.name


def _soak_date(run_id: str, data_dir: Path) -> str:
    m = re.search(r"(\d{8})", run_id) or re.search(r"(\d{8})", data_dir.name)
    if m:
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    edge_dir = data_dir / "raw" / "edge"
    dates = sorted(p.stem for p in edge_dir.glob("*.jsonl")) if edge_dir.is_dir() else []
    return dates[-1] if dates else datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _survival_split(rows: list[dict], bucket_by_market: dict[str, str]) -> dict:
    from arbx.analysis.episodes import kalshi_market

    tiers = ("survived_1000ms", "survived_500ms", "survived_250ms", "expired_lt_250ms")
    counts = {"transient_candidate": Counter(), "basis_suspect": Counter(), "unusable": Counter()}
    transient_probe_events = 0
    transient_ge = {250: 0, 500: 0, 1000: 0}
    for r in rows:
        if not r.get("public_probe") or not r.get("survival_tier"):
            continue
        bucket = bucket_by_market.get(kalshi_market(r.get("pair_key", "")), "unusable")
        counts.setdefault(bucket, Counter())[r["survival_tier"]] += 1
        if bucket == "transient_candidate":
            transient_probe_events += 1
            through = r.get("survived_through_ms")
            if isinstance(through, (int, float)):
                for thr in (250, 500, 1000):
                    if through >= thr:
                        transient_ge[thr] += 1
    return {
        "tiers": tiers,
        "counts": {k: dict(v) for k, v in counts.items()},
        "transient_probe_rows": transient_probe_events,
        "transient_ge": transient_ge,
    }


def _recurrence_timing(episodes) -> list[dict]:
    by_pair: dict[tuple[str, str], list] = defaultdict(list)
    for e in episodes:
        if e.bucket != "transient_candidate":
            continue
        by_pair[(e.kalshi_market, e.direction)].append(e)
    out = []
    for (mkt, direction), eps in by_pair.items():
        starts = sorted(_parse(e.start_ts) for e in eps if _parse(e.start_ts) is not None)
        gaps = [(starts[i + 1] - starts[i]) / 60 for i in range(len(starts) - 1)]
        hours = Counter(datetime.fromtimestamp(s).hour for s in starts) if starts else Counter()
        best_tier = None
        best_through = -1
        for e in eps:
            if e.survived_through_ms is not None and e.survived_through_ms > best_through:
                best_through = e.survived_through_ms
                best_tier = e.best_survival_tier
        out.append({
            "market": mkt, "direction": direction, "episodes": len(eps),
            "median_gap_min": round(median(gaps), 1) if gaps else None,
            "peak_depth_edge": round(max(e.median_depth_edge for e in eps) * 100, 1),
            "best_survival_tier": best_tier,
            "top_hours": [h for h, _ in hours.most_common(4)],
        })
    out.sort(key=lambda d: (d["best_survival_tier"] or "", d["episodes"]), reverse=True)
    return out


def _parse(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def _depth_and_direction(rows: list[dict], basis: set[str]) -> dict:
    from arbx.analysis.episodes import kalshi_market, qualifies

    sizes, dirs = [], Counter()
    for r in rows:
        if r.get("public_probe") or kalshi_market(r.get("pair_key", "")) in basis:
            continue
        if qualifies(r):
            sizes.append(float(r.get("max_profitable_size") or 0.0))
            dirs[r.get("direction")] += 1
    sizes.sort()
    curve = {}
    if sizes:
        for floor in (1, 100, 1000, 5000):
            curve[floor] = round(100 * sum(1 for s in sizes if s >= floor) / len(sizes))
    return {
        "qualifying_rows": len(sizes),
        "median_size": round(median(sizes)) if sizes else 0,
        "max_size": round(max(sizes)) if sizes else 0,
        "fillable_pct": curve,
        "direction": dict(dirs),
    }


def _cross_soak(root: Path, current: Path) -> list[dict]:
    from arbx.analysis.episodes import classify_pairs

    out = []
    for other in sorted(root.glob("data_strategy_30_modeling_*")):
        if not other.is_dir() or other.resolve() == current.resolve():
            continue
        rows = _edge_rows(other, cap=_COMPARE_CAP)
        if not rows:
            continue
        buckets = {c["kalshi_market"]: c["bucket"] for c in classify_pairs(rows)}
        transient = sorted(m for m, b in buckets.items() if b == "transient_candidate")
        out.append({"soak": other.name, "transient_candidates": transient})
    return out


def main(argv: list[str] | None = None) -> int:
    from arbx.analysis.episodes import (
        classify_pairs,
        fee_sensitivity,
        rank_opportunities,
    )
    from arbx.data.quality import analyze

    parser = argparse.ArgumentParser(description="Run the full post-soak analysis battery")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--skip-heatmaps", action="store_true")
    parser.add_argument("--no-compare", action="store_true")
    parser.add_argument("--real-fees", action="store_true",
                        help="Re-derive edges from raw book rows with the real FeeEngine "
                             "instead of reading stored (flat-fee) edge files")
    parser.add_argument("--pairs", type=Path, default=ROOT / "configs" / "pairs.approved.yaml",
                        help="Pair registry for --real-fees re-derivation")
    parser.add_argument("--poly-bps", type=int, default=None,
                        help="With --real-fees: pin Polymarket base_fee bps offline instead of "
                             "querying /fee-rate (resolved markets 404 into the worst-case fallback)")
    parser.add_argument("--legacy-book-fix", action="store_true",
                        help="Recover legacy rows with swapped bid/ask labels "
                             "(docs/book_semantics_fix.md). Forces re-derivation from raw book "
                             "rows and skips the evidence-pack/heatmap subprocesses, which read "
                             "the stored (contaminated) edge files")
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()
    fee_model_version = None
    if args.real_fees or args.legacy_book_fix:
        if str(ROOT) not in sys.path:  # so `scripts.` imports work when run directly
            sys.path.insert(0, str(ROOT))
        from scripts.analyze_edge_episodes import build_fee_engine

        from arbx.analysis.edges import derive_edges, load_edge_pairs

        row_transform = None
        if args.legacy_book_fix:
            from arbx.data.legacy import unswap_legacy_book_row

            row_transform = unswap_legacy_book_row
        engine = build_fee_engine(args.poly_bps) if args.real_fees else None
        fee_model_version = engine.fee_model_version if engine is not None else None
        rows = derive_edges(data_dir, load_edge_pairs(args.pairs),
                            fee_engine=engine, row_transform=row_transform)
        rows = rows[:_ROW_CAP]
    else:
        rows = _edge_rows(data_dir)
    run_id = _resolve_run_id(data_dir, rows)
    soak_date = _soak_date(run_id, data_dir)

    # --- Tier 1: trust + evidence pack + heatmaps (best-effort subprocesses) ---
    dq = analyze(data_dir).to_dict()
    py = sys.executable
    if args.legacy_book_fix:
        # Evidence-pack export and heatmaps read the stored edge files, which in
        # a legacy dir were derived from swapped books; and heatmaps write into
        # data_dir, which may live in the read-only research repo. Skip both.
        print("[legacy-book-fix] skipping evidence-pack export and heatmaps "
              "(stored edge files predate the book-semantics fix)")
    else:
        subprocess.run([py, str(ROOT / "scripts" / "export_soak_report.py"),
                        "--data-dir", str(data_dir), "--out-dir", str(args.out_dir)], check=False)
        if not args.skip_heatmaps:
            subprocess.run([py, str(ROOT / "scripts" / "build_heatmaps.py"),
                            "--data-dir", str(data_dir), "--strategy-only",
                            "--html", str(data_dir / "heatmaps.html")], check=False)

    ranked = rank_opportunities(rows)
    pairs = classify_pairs(rows)
    bucket_by_market = {c["kalshi_market"]: c["bucket"] for c in pairs}
    basis = {c["kalshi_market"] for c in pairs if c["bucket"] == "basis_suspect"}
    candidates = [e for e in ranked if e.bucket == "transient_candidate"]
    # The assumed-fee ladder is only meaningful on flat-fee derived rows.
    fees = None if args.real_fees else fee_sensitivity(rows)

    # --- Tiers 2-4 ---
    survival = _survival_split(rows, bucket_by_market)
    timing = _recurrence_timing(ranked)
    depthdir = _depth_and_direction(rows, basis)
    compare = [] if args.no_compare else _cross_soak(ROOT, data_dir)

    # --- go/no-go read per candidate pair ---
    verdicts = []
    other_transients = {m for c in compare for m in c["transient_candidates"]}
    seen = set()
    for t in timing:
        if t["market"] in seen:
            continue
        seen.add(t["market"])
        tier = t["best_survival_tier"]
        consistent = t["market"] in other_transients
        surv_ms = {"survived_1000ms": 1000, "survived_500ms": 500,
                   "survived_250ms": 250}.get(tier or "", 0)
        if surv_ms >= 500 and consistent:
            v = "STRONG research candidate — survives latency & recurs across soaks; pair-test next"
        elif surv_ms >= 250:
            v = "PROMISING — survives ≥250ms; needs cross-soak confirmation + real fees"
        elif tier is None:
            v = "UNPROBED — recurs but survival not yet measured; probe more"
        else:
            v = "WEAK — edge dies before 250ms; likely not capturable"
        verdicts.append((t["market"], v, tier, t["episodes"], consistent))

    # --- write dated markdown summary ---
    args.out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ("_realfees" if args.real_fees else "") + (
        "_legacyfix" if args.legacy_book_fix else "")
    out_path = args.out_dir / f"soak_analysis_{soak_date}{suffix}.md"
    fee_note = (f" · fees: **real** (`{fee_model_version}`)" if args.real_fees else "") + (
        " · **legacy book fix applied** (rows recovered per docs/book_semantics_fix.md; "
        "the `crossed_books` DQ failure below reflects the at-rest data, not the "
        "corrected rows analyzed here)" if args.legacy_book_fix else "")
    L: list[str] = []
    L += [f"# Soak analysis — {soak_date}  (`{run_id}`)", "",
          f"Generated {datetime.now(timezone.utc).isoformat()}Z · dataset `{data_dir.name}` · {len(rows)} edge rows{fee_note}", "",
          "> Public-data research signals only — not trades, not realized profit. Real fills, "
          "slippage, and net-of-real-fee profit need authenticated execution data.", ""]

    L += ["## Go / no-go read (per candidate pair)", ""]
    if verdicts:
        for mkt, v, tier, eps, consistent in verdicts:
            L.append(f"- **{mkt}** — {v}  _(best tier: {tier or 'none'}, {eps} episodes, "
                     f"{'seen in other soaks' if consistent else 'only this soak'})_")
    else:
        L.append("- No transient research candidates cleared the filters this soak.")
    L += [""]

    L += ["## 1. Data quality (trust gate)",
          f"- recorder health passed: **{dq.get('recorder_health_passed')}** · "
          f"freshness passed: **{dq.get('freshness_passed')}**",
          f"- book rows: {sum(v.get('rows', 0) for v in (dq.get('freshness_by_venue') or {}).values())} · "
          f"edge rows: {len(rows)}", ""]

    L += ["## 2. Bucket summary",
          f"- transient candidates: **{sum(1 for p in pairs if p['bucket']=='transient_candidate')}** · "
          f"basis-suspect: **{len(basis)}** · unusable: **{sum(1 for p in pairs if p['bucket']=='unusable')}**", ""]

    L += ["## 3. Survival deep-dive (the point of --probe)",
          f"- transient-candidate probe rows: **{survival['transient_probe_rows']}**"]
    if survival["transient_probe_rows"]:
        g = survival["transient_ge"]
        n = survival["transient_probe_rows"]
        L.append(f"  - survived ≥250ms: {g[250]} ({100*g[250]//max(n,1)}%) · "
                 f"≥500ms: {g[500]} ({100*g[500]//max(n,1)}%) · "
                 f"≥1000ms: {g[1000]} ({100*g[1000]//max(n,1)}%)")
    else:
        L.append("  - **no transient candidates were probed** — survival is inconclusive; "
                 "probe more or lengthen the soak")
    L.append(f"- basis-pair probe rows (trivially survive, uninformative): "
             f"{sum(survival['counts'].get('basis_suspect', {}).values())}")
    L += [""]

    L += ["## 4. Recurrence & timing (where/when to strike)", ""]
    if timing:
        L.append("| pair | dir | episodes | median gap (min) | peak depth edge | best survival | top UTC hours |")
        L.append("|---|---|---|---|---|---|---|")
        for t in timing[: args.top]:
            L.append(f"| {t['market']} | {t['direction']} | {t['episodes']} | {t['median_gap_min']} | "
                     f"{t['peak_depth_edge']}c | {t['best_survival_tier'] or 'unprobed'} | "
                     f"{', '.join(str(h) for h in t['top_hours'])} |")
    else:
        L.append("_(no transient episodes)_")
    L += [""]

    if fees is None:
        L += ["## 5. Fees",
              f"- edges re-derived with **real venue fee formulas** (`{fee_model_version}`); "
              "`fee_adj_edge` is net of real per-leg taker fees — see `reports/fee_truth/` "
              "for the flat-vs-real collapse.", ""]
    else:
        L += ["## 5. Fee sensitivity (fragility)",
              "| assumed round-trip fee | candidate rows | pairs |",
              "|---|---|---|"]
        for f in fees:
            L.append(f"| {f['fee']*100:.1f}c | {f['candidate_rows']} | {len(f['candidate_pairs'])} |")
        L += ["", "_Fees are heuristic — no profitability claim until real venue fee schedules are wired in._", ""]

    L += ["## 6. Executable-size reality & direction",
          f"- qualifying non-basis rows: {depthdir['qualifying_rows']} · "
          f"median displayed size: {depthdir['median_size']} · max: {depthdir['max_size']}",
          f"- fillable-at ≥ size (%): {depthdir['fillable_pct']}",
          f"- direction split: {depthdir['direction']}",
          "- _Displayed size is an upper bound, not a fill — real depth needs execution data._", ""]

    L += ["## 7. Cross-soak consistency", ""]
    if compare:
        for c in compare:
            L.append(f"- `{c['soak']}` transient candidates: {', '.join(c['transient_candidates']) or 'none'}")
        L.append(f"- **this soak (`{data_dir.name}`)**: "
                 f"{', '.join(c.kalshi_market for c in candidates[:1]) if candidates else 'none'} "
                 f"(+{max(len({c.kalshi_market for c in candidates})-1,0)} more)")
    else:
        L.append("- no other soaks found to compare against")
    L += [""]

    L += ["## What this can & cannot conclude",
          "- **Can:** which pairs are real transient candidates, whether edges survive latency, "
          "when/where they recur, fee-fragility, day-over-day consistency.",
          "- **Cannot (needs new data):** real fill probability, real slippage, true net edge — "
          "these need real venue fee schedules (desk research) and authenticated execution (gated phase).",
          "",
          f"Evidence pack: `{(args.out_dir / run_id)}/`", ""]

    out_path.write_text("\n".join(L), encoding="utf-8")

    # --- stdout summary ---
    print(f"\n{'='*70}\nSOAK ANALYSIS — {soak_date} ({run_id})\n{'='*70}")
    print(f"edge rows: {len(rows)} | DQ health={dq.get('recorder_health_passed')} freshness={dq.get('freshness_passed')}")
    print(f"buckets: transient={sum(1 for p in pairs if p['bucket']=='transient_candidate')} "
          f"basis={len(basis)} unusable={sum(1 for p in pairs if p['bucket']=='unusable')}")
    print("\nGO/NO-GO:")
    for mkt, v, *_ in verdicts or []:
        print(f"  {mkt}: {v}")
    if not verdicts:
        print("  (no transient candidates)")
    print(f"\nSummary written: {out_path}")
    print(f"Evidence pack:   {(args.out_dir / run_id)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
