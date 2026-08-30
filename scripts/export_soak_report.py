#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Export compact per-soak analysis artifacts for comparison.
#
# Writes reports/<run_id>/{dq_summary,episode_rankings,basis_suspects,
# survival_summary}.json + profitability_summary.md so soaks can be compared
# without re-parsing huge JSONL each time. Public-data only: every artifact is a
# research/candidate signal, never a trade or realized-profit claim.
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def _edge_rows(data_dir: Path):
    from arbx.analysis.episodes import iter_edge_rows
    edge_dir = data_dir / "raw" / "edge"
    rows = []
    if edge_dir.is_dir():
        for f in sorted(edge_dir.glob("*.jsonl")):
            rows.extend(iter_edge_rows(f))
    return rows


def _resolve_run_id(data_dir: Path, rows: list[dict]) -> str:
    """Prefer the recorder run id from the continuity log; fall back to dir name."""
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


def _survival_summary(rows: list[dict]) -> dict:
    from arbx.analysis.survival import SURVIVAL_TIER_COLORS
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("public_probe") and r.get("survival_tier"):
            counts[r["survival_tier"]] = counts.get(r["survival_tier"], 0) + 1
    graded = sum(counts.get(t, 0) for t in ("survived_250ms", "survived_500ms", "survived_1000ms"))
    return {"tier_counts": counts, "graded_total": graded, "colors": SURVIVAL_TIER_COLORS,
            "probed": bool(counts)}


def main(argv: list[str] | None = None) -> int:
    from arbx.analysis.episodes import (
        BUCKET_BASIS,
        BUCKET_TRANSIENT,
        BUCKET_UNUSABLE,
        classify_pairs,
        episode_to_dict,
        fee_sensitivity,
        rank_opportunities,
    )
    from arbx.data.quality import analyze

    parser = argparse.ArgumentParser(description="Export per-soak analysis artifacts")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()
    rows = _edge_rows(data_dir)
    run_id = _resolve_run_id(data_dir, rows)
    out = args.out_dir / run_id
    out.mkdir(parents=True, exist_ok=True)

    dq = analyze(data_dir).to_dict()
    ranked = rank_opportunities(rows)
    pairs = classify_pairs(rows)
    fees = fee_sensitivity(rows)
    survival = _survival_summary(rows)
    candidates = [e for e in ranked if e.bucket == BUCKET_TRANSIENT]
    basis = [p for p in pairs if p["bucket"] == BUCKET_BASIS]
    unusable = [p for p in pairs if p["bucket"] == BUCKET_UNUSABLE]

    (out / "dq_summary.json").write_text(json.dumps(dq, indent=2), encoding="utf-8")
    (out / "episode_rankings.json").write_text(
        json.dumps({"run_id": run_id, "candidates": [episode_to_dict(e) for e in candidates[: args.top]],
                    "candidate_count": len(candidates)}, indent=2), encoding="utf-8")
    (out / "basis_suspects.json").write_text(
        json.dumps({"run_id": run_id, "pairs": pairs}, indent=2), encoding="utf-8")
    (out / "survival_summary.json").write_text(
        json.dumps({"run_id": run_id, **survival}, indent=2), encoding="utf-8")

    top = candidates[0] if candidates else None
    md = [
        f"# Profitability summary — `{run_id}`",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "> Public-data research signals only. Not trades, not realized profit. "
        "Fees are heuristic and survival/depth are estimates until real venue "
        "schedules, streaming, and authenticated execution data exist.",
        "",
        "## Data quality",
        f"- recorder health passed: **{dq.get('recorder_health_passed')}**, "
        f"freshness passed: **{dq.get('freshness_passed')}**",
        f"- book rows: {sum(v.get('rows', 0) for v in (dq.get('freshness_by_venue') or {}).values())}"
        f"; edge rows: {len(rows)}",
        "",
        "## Pair classification",
        f"- transient candidates: **{len([p for p in pairs if p['bucket']==BUCKET_TRANSIENT])}**",
        f"- basis-suspect (excluded): **{len(basis)}**",
        f"- unusable: **{len(unusable)}**",
        "",
        "## Survival probes",
        (f"- graded survival rows: **{survival['graded_total']}** — tiers "
         f"{survival['tier_counts']}" if survival["probed"]
         else "- **no survival probes in this soak** — run the deriver with `--probe`"),
        "",
        "## Fee sensitivity (non-basis candidate rows)",
    ]
    for f in fees:
        md.append(f"- fee {f['fee']*100:.1f}c: {f['candidate_rows']} rows across {len(f['candidate_pairs'])} pairs")
    md += ["", "## Top research candidate"]
    if top:
        d = episode_to_dict(top)
        md += [
            f"- **{d['kalshi_market']} / {d['direction']}**",
            f"- bucket: {d['bucket']} — {d['reason']}",
            f"- median depth-adj edge: {d['median_depth_edge']*100:.1f}c, "
            f"survival-adjusted: {d['survival_adjusted_edge']}, recurrence: {d['recurrence_count']} episodes",
            f"- research score: {d['score']}",
        ]
    else:
        md.append("- (none cleared the thresholds)")
    md.append("")
    (out / "profitability_summary.md").write_text("\n".join(md), encoding="utf-8")

    print(f"Wrote report for run '{run_id}' to {out}")
    for name in ("dq_summary.json", "episode_rankings.json", "basis_suspects.json",
                 "survival_summary.json", "profitability_summary.md"):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
