# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Targeted per-pair soak and evidence pack.
"""Single-pair streaming soak and the per-pair evidence pack.

``run_targeted_soak`` captures one pair with the same hybrid StreamingSource
the broad soaks use (Polymarket WS + Kalshi REST poll unless authenticated
Kalshi WS is configured), deriving real-fee edge rows live and firing the
sub-110ms probe ladder on qualifying edges. ``build_evidence_pack`` is the
pure post-processing half: DQ gate, episodes, survival summary, liquidity
profile, and the time-of-day strike map, written into
``evidence/<kalshi_market_id>/<date>/``.

When the Kalshi leg of "streaming" is a REST-poll stopgap, sub-110ms survival
claims on that leg are labeled ``kalshi_rest_poll_stopgap`` in
``survival_summary.json`` — liquidity/episode/persistence evidence is valid
either way. Public data only; no orders, no credentials.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from arbx.analysis.edges import EdgePair, edge_rows_for_capture, write_edges_jsonl
from arbx.pairs.registry import PairSpec

PROBE_COOLDOWN_S = 600.0
_TOP_N = 5


def _edge_pair(pair: PairSpec) -> EdgePair:
    return EdgePair(
        pair_key=pair.pair_key,
        kalshi_market_id=pair.kalshi_market_id,
        polymarket_market_id=pair.polymarket_yes_token_id or pair.polymarket_condition_id,
        include_in_strategy_metrics=pair.include_in_strategy_metrics,
    )


async def _capture(pair: PairSpec, hours: float, data_dir: Path, *,
                   fee_engine, rest_interval_s: float, stale_after_s: float) -> dict:
    from arbx.analysis.episodes import qualifies
    from arbx.analysis.survival import (
        STREAMING_PROBE_DELAYS_MS,
        run_public_edge_survival_probe,
    )
    from arbx.capture.clock import measure_ntp_offset_ms
    from arbx.capture.engine import KalshiRestPollStream, StreamingSource
    from arbx.capture.sink import ObservationSink
    from arbx.data.recorder import _HeartbeatWriter, build_live_public_connectors

    run_id = f"targeted_{pair.kalshi_market_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    ntp = measure_ntp_offset_ms()
    sink = ObservationSink(data_dir, run_id=run_id, ntp_offset_ms=ntp)
    heartbeat = _HeartbeatWriter(data_dir)
    heartbeat.write_event("recorder_start", run_id, universe_size=2,
                          ntp_offset_ms=ntp, capture_mode="targeted_soak")
    source = StreamingSource(
        [pair], stale_after_s=stale_after_s,
        kalshi_stream=KalshiRestPollStream(interval_s=rest_interval_s),
        heartbeat=heartbeat, run_id=run_id,
    )
    probe_connectors = build_live_public_connectors()
    epair = _edge_pair(pair)

    edge_buffer: list[dict] = []
    probe_rows: list[dict] = []
    last_probe_at = -1e12
    probes_fired = 0
    paired_count = 0
    deadline = time.monotonic() + hours * 3600.0
    print(f"[targeted_soak] run_id={run_id} pair={pair.kalshi_market_id} hours={hours}")
    try:
        async for paired in source.subscribe([pair]):
            k_row, p_row = sink.write_paired(paired)
            paired_count += 1
            rows = edge_rows_for_capture(epair, k_row, p_row, fee_engine=fee_engine)
            edge_buffer.extend(rows)
            if len(edge_buffer) >= 200:
                write_edges_jsonl(data_dir, edge_buffer)
                edge_buffer.clear()

            now = time.monotonic()
            if any(qualifies(row) for row in rows) and now - last_probe_at >= PROBE_COOLDOWN_S:
                last_probe_at = now
                probes_fired += 1
                print(f"[targeted_soak] qualifying edge — probing rungs "
                      f"{STREAMING_PROBE_DELAYS_MS}")
                probe_rows.extend(await asyncio.to_thread(
                    run_public_edge_survival_probe, epair, probe_connectors,
                    delays_ms=STREAMING_PROBE_DELAYS_MS, run_id=run_id,
                ))
            if time.monotonic() >= deadline:
                break
    finally:
        if edge_buffer:
            write_edges_jsonl(data_dir, edge_buffer)
        if probe_rows:
            write_edges_jsonl(data_dir, probe_rows)
        sink.close()
        heartbeat.write_event("recorder_stop", run_id, probes=probes_fired)
        heartbeat.close()
    print(f"[targeted_soak] capture done: paired={paired_count} probes={probes_fired}")
    return {"run_id": run_id, "paired_snapshots": paired_count, "probes": probes_fired}


def _iter_book_rows(data_dir: Path):
    base = data_dir / "raw" / "book"
    for jsonl in sorted(base.rglob("*.jsonl")) if base.exists() else []:
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _edge_rows(data_dir: Path, pair: PairSpec, fee_engine, *,
               legacy_book_fix: bool) -> list[dict]:
    """Stored edge rows when the capture wrote them; else re-derive from raw."""
    from arbx.analysis.episodes import iter_edge_rows

    edge_dir = data_dir / "raw" / "edge"
    rows = [row for f in sorted(edge_dir.glob("*.jsonl"))
            for row in iter_edge_rows(f)] if edge_dir.is_dir() else []
    if rows:
        return rows
    from arbx.analysis.edges import derive_edges
    from arbx.data.legacy import unswap_legacy_book_row

    return derive_edges(
        data_dir, [_edge_pair(pair)], fee_engine=fee_engine,
        row_transform=unswap_legacy_book_row if legacy_book_fix else None,
    )


def _hour_of(ts: Any) -> int | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).hour
    except (TypeError, ValueError):
        return None


def _liquidity_profile(data_dir: Path, pair: PairSpec, *,
                       legacy_book_fix: bool) -> dict:
    """Liquidity evidence: per-hour top-5 depth per venue, spreads, depth ratio."""
    from arbx.data.legacy import unswap_legacy_book_row

    depth_by = {"kalshi": defaultdict(list), "polymarket": defaultdict(list)}
    spreads: dict[str, list[float]] = {"kalshi": [], "polymarket": []}
    for row in _iter_book_rows(data_dir):
        venue = row.get("venue")
        if venue not in depth_by:
            continue
        if legacy_book_fix:
            row = unswap_legacy_book_row(row)
        depth = sum(
            float(row.get(f"{side}_sz_{i}") or 0.0)
            for side in ("bid", "ask") for i in range(1, _TOP_N + 1)
        )
        hour = _hour_of(row.get("capture_ts_utc"))
        if hour is not None:
            depth_by[venue][hour].append(depth)
        spread = row.get("spread")
        if isinstance(spread, (int, float)) and spread >= 0:
            spreads[venue].append(float(spread))

    def _percentile(values: list[float], pct: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))
        return ordered[idx]

    profile: dict[str, Any] = {"venues": {}}
    medians = {}
    for venue, by_hour in depth_by.items():
        all_depths = [d for ds in by_hour.values() for d in ds]
        medians[venue] = median(all_depths) if all_depths else 0.0
        profile["venues"][venue] = {
            "rows": len(all_depths),
            "median_top5_depth": round(medians[venue], 2),
            "median_top5_depth_by_hour_utc": {
                str(h): round(median(ds), 2) for h, ds in sorted(by_hour.items())
            },
            "spread_p50": _percentile(spreads[venue], 0.50),
            "spread_p90": _percentile(spreads[venue], 0.90),
        }
    profile["kalshi_to_poly_depth_ratio"] = (
        round(medians["kalshi"] / medians["polymarket"], 4)
        if medians.get("polymarket") else None
    )
    profile["r7_note"] = ("Kalshi is usually the constraining leg — size by the "
                          "smaller side of this ratio.")
    return profile


def _survival_summary(rows: list[dict], *, hybrid_kalshi: bool) -> dict:
    tiers: Counter = Counter()
    ladders = 0
    max_through = None
    for row in rows:
        if not row.get("public_probe"):
            continue
        if row.get("benchmark_ms") == 0 and row.get("survival_tier"):
            tiers[row["survival_tier"]] += 1
            ladders += 1
        through = row.get("survived_through_ms")
        if isinstance(through, (int, float)):
            max_through = through if max_through is None else max(max_through, through)
    summary = {
        "probe_ladders": ladders,
        "tier_distribution": dict(tiers),
        "max_survived_through_ms": max_through,
        "hybrid_kalshi_rest_poll": hybrid_kalshi,
    }
    if hybrid_kalshi:
        summary["sub_110ms_validity"] = (
            "kalshi_rest_poll_stopgap — the Kalshi leg is polling rather than "
            "using authenticated Kalshi WS; sub-110ms tiers on that leg "
            "are not yet trustworthy. Liquidity/episode evidence is unaffected."
        )
    return summary


def build_evidence_pack(pair: PairSpec, evidence_dir: Path, data_dir: Path, *,
                        fee_engine=None, hybrid_kalshi: bool = True,
                        legacy_book_fix: bool = False,
                        capture_stats: dict | None = None) -> dict:
    """Post-process a captured (or fixture) data dir into the evidence pack."""
    from arbx.analysis.episodes import (
        annotate_survival,
        build_episodes,
        episode_to_dict,
    )
    from arbx.analysis.heatmap import edge_heatmap
    from arbx.data.quality import analyze

    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    dq = analyze(data_dir).to_dict()
    if legacy_book_fix:
        dq["note"] = ("legacy fixture dir: crossed_books fails at rest by design; "
                      "rows corrected at read time (docs/book_semantics_fix.md)")
    (evidence_dir / "dq_summary.json").write_text(
        json.dumps(dq, indent=2, default=str), encoding="utf-8")

    rows = _edge_rows(data_dir, pair, fee_engine, legacy_book_fix=legacy_book_fix)
    pair_rows = [r for r in rows if r.get("pair_key") == pair.pair_key] or rows

    episodes = build_episodes(pair_rows)
    annotate_survival(episodes, pair_rows)
    (evidence_dir / "episodes.json").write_text(json.dumps({
        "pair_key": pair.pair_key,
        "edge_rows": len(pair_rows),
        "episodes": [episode_to_dict(e) for e in episodes],
    }, indent=2, default=str), encoding="utf-8")

    (evidence_dir / "survival_summary.json").write_text(json.dumps(
        _survival_summary(pair_rows, hybrid_kalshi=hybrid_kalshi),
        indent=2), encoding="utf-8")

    (evidence_dir / "liquidity_profile.json").write_text(json.dumps(
        _liquidity_profile(data_dir, pair, legacy_book_fix=legacy_book_fix),
        indent=2), encoding="utf-8")

    heat = edge_heatmap(pair_rows, strategy_only=False)
    (evidence_dir / "strike_map.html").write_text(
        heat.render_html(), encoding="utf-8")

    # Pull the pair's latest rules snapshot + prescreen into the pack if this
    # date dir does not already hold them.
    market_root = evidence_dir.parent
    for name in ("rules_snapshot.json", "prescreen.json", "ai_audit.md"):
        target = evidence_dir / name
        if target.exists():
            continue
        candidates = sorted(market_root.glob(f"*/{name}"))
        if candidates:
            shutil.copy(candidates[-1], target)

    manifest = {
        "pair_key": pair.pair_key,
        "kalshi_market_id": pair.kalshi_market_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "capture": capture_stats or {},
        "files": sorted(p.name for p in evidence_dir.iterdir() if p.is_file()),
    }
    (evidence_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def run_targeted_soak(pair: PairSpec, hours: float, data_dir: Path, *,
                      fee_engine=None, rest_interval_s: float = 5.0,
                      stale_after_s: float = 30.0,
                      evidence_root: Path | None = None) -> Path:
    """Capture one pair for ``hours``, then build its evidence pack."""
    if fee_engine is None:
        from arbx.fees.engine import FeeEngine

        root = Path(__file__).resolve().parents[3]
        fee_engine = FeeEngine.from_configs(
            root / "configs" / "fees_kalshi.yaml",
            root / "configs" / "fees_polymarket.yaml")
    data_dir = Path(data_dir)
    stats = asyncio.run(_capture(
        pair, hours, data_dir, fee_engine=fee_engine,
        rest_interval_s=rest_interval_s, stale_after_s=stale_after_s))

    evidence_root = evidence_root or Path(__file__).resolve().parents[3] / "evidence"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    evidence_dir = evidence_root / pair.kalshi_market_id / date
    build_evidence_pack(pair, evidence_dir, data_dir,
                        fee_engine=fee_engine, capture_stats=stats)
    print(f"[targeted_soak] evidence pack: {evidence_dir}")
    return evidence_dir
