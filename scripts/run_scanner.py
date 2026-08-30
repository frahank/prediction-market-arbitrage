#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — CLI for the live round-robin arbitrage scanner.
#
# Cycles the pair universe in fixed batches (default 20 pairs/tick, 1 tick/s),
# fires both venues concurrently per pair, prices the cross with real fees, and
# logs every qualifying/near-miss opportunity. Public GETs only, paper-only.
# See docs/live_scanner.md.
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import httpx  # noqa: E402 — import after sys.path setup

from arbx.analysis.edges import load_edge_pairs  # noqa: E402
from arbx.capture.clock import measure_ntp_offset_ms  # noqa: E402
from arbx.capture.rest_concurrent import ConcurrentRestSource  # noqa: E402
from arbx.capture.sink import ObservationSink  # noqa: E402
from arbx.pairs.registry import load_pairs  # noqa: E402
from arbx.scanner import (  # noqa: E402
    ArbScanner,
    EdgesWriter,
    OpportunitySink,
    ScannerConfig,
)


def _ms_list(value: str) -> tuple[float, ...]:
    """Parse '100,200,400' -> (100.0, 200.0, 400.0)."""
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return tuple(float(p) for p in parts)


def load_depth_haircut(path: Path | None = None) -> float:
    """`executable.depth_haircut` from configs/modeling.yaml (0.5 fallback)."""
    import yaml

    try:
        modeling = yaml.safe_load((path or ROOT / "configs" / "modeling.yaml").read_text())
        return float(modeling["executable"]["depth_haircut"])
    except (OSError, KeyError, TypeError, ValueError):
        return 0.5


def build_fee_engine(poly_bps: int | None = None):
    """Real FeeEngine from the pinned configs (public fee GETs only).

    ``poly_bps`` pins the Polymarket rate for resolved/offline markets that
    404 on the live ``/fee-rate`` endpoint (they would otherwise re-score at
    the worst-case fallback instead of the applicable rate).
    """
    from arbx.fees.engine import FeeEngine

    kalshi_yaml = ROOT / "configs" / "fees_kalshi.yaml"
    poly_yaml = ROOT / "configs" / "fees_polymarket.yaml"
    if poly_bps is None:
        return FeeEngine.from_configs(kalshi_yaml, poly_yaml)
    from arbx.fees.polymarket import StaticFeeRateProvider

    return FeeEngine.from_configs(
        kalshi_yaml, poly_yaml, provider=StaticFeeRateProvider(poly_bps)
    )


async def _run(args: argparse.Namespace) -> dict:
    pairs = load_pairs(args.pairs)
    edge_pairs = {ep.pair_key: ep for ep in load_edge_pairs(args.pairs)}
    if not pairs:
        print("[run_scanner] no pairs loaded — nothing to scan")
        return {}

    config = ScannerConfig(
        batch_size=args.batch_size,
        tick_s=args.tick_s,
        min_arb_edge=args.min_arb_edge,
        target_size=args.target_size,
        record_books=not args.no_record_books,
        confirm_survival_ms=args.confirm_survival_ms,
        confirm_survival_ms_list=args.confirm_survival_ms_list,
    )
    fee_engine = build_fee_engine(args.poly_bps)
    ntp = measure_ntp_offset_ms()
    run_id = args.run_id or f"scan_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    data_dir = Path(args.data_dir)

    sink = (
        ObservationSink(data_dir, run_id=run_id, ntp_offset_ms=ntp)
        if config.record_books
        else None
    )
    opp_sink = OpportunitySink(data_dir)
    # Always-on standardized EDGES output (M2-T3): edges-only runs persist
    # every detected row; full-record runs persist qualifying rows, so
    # Module 4's edges view is uniform across run shapes.
    edges_writer = EdgesWriter(
        data_dir,
        depth_haircut=load_depth_haircut(),
        qualifying_only=not args.edges_only,
    )

    # One tuned client for the whole run; a 20-pair batch fires <=40 in-flight
    # GETs, comfortably inside the pool.
    client = httpx.AsyncClient(
        timeout=10.0,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    source = ConcurrentRestSource(pairs, interval_s=config.tick_s, client=client)

    async def fetch(pair):
        return await source.fetch_pair(client, pair)

    scanner = ArbScanner(
        pairs, edge_pairs,
        fetch_pair=fetch,
        fee_engine=fee_engine,
        config=config,
        run_id=run_id,
        sink=sink,
        opportunity_sink=opp_sink,
        edges_writer=edges_writer,
        ntp_offset_ms=ntp,
        rotation_state_path=data_dir / "scan_state.json",
        ntp_measure=measure_ntp_offset_ms,
    )

    cycle_s = config.cycle_time_s(len(pairs))
    print(f"[run_scanner] run_id={run_id} pairs={len(pairs)} "
          f"batch={config.batch_size} tick={config.tick_s}s "
          f"cycle~={cycle_s:.0f}s record_books={config.record_books} "
          f"ntp_offset_ms={ntp}")
    try:
        stats = await scanner.run(duration_s=args.duration)
    finally:
        await client.aclose()

    summary = {
        "run_id": run_id,
        "pairs": len(pairs),
        "batch_size": config.batch_size,
        "tick_s": config.tick_s,
        "cycle_time_s": cycle_s,
        "duration_s": args.duration,
        "poly_bps": args.poly_bps,
        "record_books": config.record_books,
        "edges_only": args.edges_only,
        "edges_written": edges_writer.count,
        "ntp_offset_ms_last": scanner.ntp_offset_ms,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **stats.summary(),
    }
    summary_path = data_dir / "scan_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    survival = (
        f" survived_{int(args.confirm_survival_ms)}ms="
        f"{stats.survived_confirmed}/{stats.survival_probes}"
        if args.confirm_survival_ms else ""
    )
    print(f"[run_scanner] ticks={stats.ticks} scanned={stats.pairs_scanned} "
          f"arbs={stats.arbs_detected} qualifying={stats.qualifying}{survival} "
          f"opportunities_logged={opp_sink.count}")
    print(f"[run_scanner] summary written: {summary_path}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live arbitrage scanner")
    parser.add_argument("--pairs", type=Path,
                        default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=900.0,
                        help="Total scan time in seconds")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--tick-s", type=float, default=1.0)
    parser.add_argument("--min-arb-edge", type=float, default=0.0,
                        help="Log a row when fee_adj_edge exceeds this")
    parser.add_argument("--target-size", type=float, default=1.0)
    parser.add_argument("--run-id", type=str, default=None,
                        help="Optional run id stamped into output rows")
    parser.add_argument("--confirm-survival-ms-list", type=_ms_list, default=None,
                        help="comma-separated survival probe rungs, e.g. '100,200,400'; "
                             "overrides --confirm-survival-ms")
    parser.add_argument("--confirm-survival-ms", type=float, default=None,
                        help="Refetch each detected pair once after this delay "
                             "and label whether the edge survived (e.g. 200)")
    parser.add_argument("--poly-bps", type=int, default=None,
                        help="Pin the Polymarket fee rate (bps) for offline markets")
    parser.add_argument("--no-record-books", action="store_true",
                        help="Log opportunities only; do not persist book rows")
    parser.add_argument("--edges-only", action="store_true",
                        help="EDGES-focused run: no book rows; every detected "
                             "row (not just qualifying) lands in EDGES_*.jsonl")
    args = parser.parse_args(argv)
    if args.edges_only:
        args.no_record_books = True
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
