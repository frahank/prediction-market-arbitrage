#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Streaming soak: derive edges live and probe survival below 110ms.
#
# Runs StreamingSource for --hours, derives both directions' edge rows from
# every emitted PairedSnapshot with the real FeeEngine, and fires the public
# refetch probe ladder (rungs 0/25/50/100/250/500/1000ms) whenever a derived
# row qualifies — throttled per pair. Afterwards writes
# reports/survival_streaming/<date>.md comparing survival-tier distributions
# between the old REST-sequential soaks (baseline dirs) and this run.
# Public data only.
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from arbx.analysis.edges import (  # noqa: E402
    EdgePair,
    edge_rows_for_capture,
    write_edges_jsonl,
)
from arbx.analysis.survival import (  # noqa: E402
    STREAMING_PROBE_DELAYS_MS,
    run_public_edge_survival_probe,
)
from arbx.capture.clock import measure_ntp_offset_ms  # noqa: E402
from arbx.capture.engine import KalshiRestPollStream, StreamingSource  # noqa: E402
from arbx.capture.sink import ObservationSink  # noqa: E402
from arbx.data.recorder import (  # noqa: E402
    _HeartbeatWriter,
    build_live_public_connectors,
)
from arbx.pairs.registry import PairSpec, load_pairs  # noqa: E402

PROBE_COOLDOWN_S = 600.0  # at most one probe ladder per pair per 10 minutes

# The auth track migrated Kalshi to the external-api host; keep the WS endpoint
# on the same host family as the REST base the signer authenticates against so
# the signed handshake and the connection target agree. Override with
# --kalshi-ws-url if Kalshi serves market-data WS from a different host.
_DEFAULT_KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


def _build_kalshi_stream(args: argparse.Namespace) -> tuple[Any, str]:
    """Return (stream, human-label) for the Kalshi leg.

    ``auto`` (default): use the authenticated ``KalshiBookStream`` when a usable
    stored credential for ``--kalshi-profile`` exists, else fall back to the
    ``KalshiRestPollStream`` stopgap. ``ws`` requires the credential (hard error
    if missing); ``rest`` forces the stopgap. The WS stream is subscribe-only —
    no order capability — and the signer's private key never leaves its module.
    """
    mode = args.kalshi_stream
    if mode in ("auto", "ws"):
        try:
            from arbx.accounts.kalshi_auth import (
                signer_from_credentials,
                ws_auth_headers,
            )
            from arbx.capture.kalshi_ws import KalshiBookStream

            signer = signer_from_credentials(args.kalshi_profile)
            stream = KalshiBookStream(
                url=args.kalshi_ws_url,
                auth_headers=ws_auth_headers(signer),
            )
            return stream, f"ws (authenticated, profile={args.kalshi_profile}, url={args.kalshi_ws_url})"
        except Exception as exc:  # noqa: BLE001 — missing/invalid credential is expected in auto mode
            if mode == "ws":
                raise SystemExit(
                    f"[streaming_soak] --kalshi-stream ws requested but no usable credential "
                    f"for profile {args.kalshi_profile!r}: {exc}\n"
                    f"Store one first:  python scripts/store_credentials.py kalshi {args.kalshi_profile}"
                )
            print(f"[streaming_soak] no usable Kalshi WS credential ({exc}); "
                  f"falling back to REST-poll stopgap")
    return KalshiRestPollStream(interval_s=args.rest_interval_s), "rest-poll (stopgap, not true streaming)"


def _edge_pair(pair: PairSpec) -> EdgePair:
    return EdgePair(
        pair_key=pair.pair_key,
        kalshi_market_id=pair.kalshi_market_id,
        polymarket_market_id=pair.polymarket_yes_token_id or pair.polymarket_condition_id,
        include_in_strategy_metrics=pair.include_in_strategy_metrics,
    )


async def _soak(args: argparse.Namespace) -> tuple[Path, list[dict]]:
    from scripts.analyze_edge_episodes import build_fee_engine

    from arbx.analysis.episodes import qualifies

    pairs = load_pairs(args.pairs)
    engine = build_fee_engine(args.poly_bps)
    data_dir = Path(args.data_dir)
    run_id = f"streaming_soak_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    ntp = measure_ntp_offset_ms()
    sink = ObservationSink(data_dir, run_id=run_id, ntp_offset_ms=ntp)
    heartbeat = _HeartbeatWriter(data_dir)
    heartbeat.write_event("recorder_start", run_id, universe_size=len(pairs) * 2,
                          ntp_offset_ms=ntp, capture_mode="streaming_soak")
    kalshi_stream, kalshi_mode = _build_kalshi_stream(args)
    print(f"[streaming_soak] kalshi leg: {kalshi_mode}")
    source = StreamingSource(
        pairs,
        stale_after_s=args.stale_after_s,
        kalshi_stream=kalshi_stream,
        heartbeat=heartbeat,
        run_id=run_id,
    )
    probe_connectors = build_live_public_connectors()
    edge_pairs = {p.pair_key: _edge_pair(p) for p in pairs}

    edge_buffer: list[dict] = []
    probe_rows: list[dict] = []
    last_probe_at: dict[str, float] = {}
    probes_fired = 0
    deadline = time.monotonic() + args.hours * 3600.0
    print(f"[streaming_soak] run_id={run_id} pairs={len(pairs)} hours={args.hours} "
          f"rungs={STREAMING_PROBE_DELAYS_MS}")
    try:
        async for paired in source.subscribe(pairs):
            k_row, p_row = sink.write_paired(paired)
            pair = edge_pairs[paired.pair_key]
            rows = edge_rows_for_capture(pair, k_row, p_row, fee_engine=engine)
            edge_buffer.extend(rows)
            if len(edge_buffer) >= 200:
                write_edges_jsonl(data_dir, edge_buffer)
                edge_buffer.clear()

            now = time.monotonic()
            if (
                any(qualifies(row) for row in rows)
                and now - last_probe_at.get(paired.pair_key, -1e12) >= PROBE_COOLDOWN_S
            ):
                last_probe_at[paired.pair_key] = now
                probes_fired += 1
                print(f"[streaming_soak] qualifying edge on {paired.pair_key} — "
                      f"probing rungs {STREAMING_PROBE_DELAYS_MS}")
                rows_probe = await asyncio.to_thread(
                    run_public_edge_survival_probe,
                    pair,
                    probe_connectors,
                    delays_ms=STREAMING_PROBE_DELAYS_MS,
                    run_id=run_id,
                )
                probe_rows.extend(rows_probe)
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
    print(f"[streaming_soak] done: paired updates kalshi={source.updates.get('kalshi', 0)} "
          f"polymarket={source.updates.get('polymarket', 0)} probes={probes_fired}")
    return data_dir, probe_rows


def _tier_distribution(edge_files: list[Path]) -> Counter:
    tiers: Counter = Counter()
    for path in edge_files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tier = row.get("survival_tier")
            if tier and row.get("benchmark_ms") == 0:  # one count per probe ladder
                tiers[tier] += 1
    return tiers


def write_report(new_dir: Path, baseline_dirs: list[Path], out_dir: Path) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_tiers = _tier_distribution(sorted((new_dir / "raw" / "edge").glob("*.jsonl")))
    old_tiers: Counter = Counter()
    for base in baseline_dirs:
        old_tiers += _tier_distribution(sorted((base / "raw" / "edge").glob("*.jsonl")))

    order = ["survived_1000ms", "survived_500ms", "survived_250ms",
             "survived_100ms", "survived_25ms", "expired_lt_250ms"]
    L = [f"# Streaming survival report — {date}", "",
         f"Generated {datetime.now(timezone.utc).isoformat()}Z.",
         f"New run: `{new_dir}` (streaming capture, probe rungs "
         f"{list(STREAMING_PROBE_DELAYS_MS)}ms).",
         f"Baselines: {', '.join(f'`{b.name}`' for b in baseline_dirs) or 'none supplied'} "
         "(REST-sequential probes, 110ms skew floor, no sub-250ms resolution).", "",
         "| survival tier | old (REST-sequential) | new (streaming) |", "|---|---|---|"]
    for tier in order:
        L.append(f"| {tier} | {old_tiers.get(tier, 0)} | {new_tiers.get(tier, 0)} |")
    L += ["",
          "- *old* probes could not resolve below 250ms (the sub-250 bucket is all gray).",
          "- **Old-baseline caveat:** pre-2026-07-03 probes ran on bid/ask-swapped books "
          "(docs/book_semantics_fix.md), so their edges — and therefore their survival "
          "tiers — describe mislabeled quantities. Treat the old column as context only; "
          "the new column is the first survival evidence on corrected books.",
          "- *new* probes measure 25/50/100ms rungs; `survived_25ms`/`survived_100ms` "
          "counts are the first direct evidence of edge life below the old floor.",
          "- Counts are one per probe ladder (baseline rung), per direction.", ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.md"
    out_path.write_text("\n".join(L), encoding="utf-8")
    print(f"[streaming_soak] report written: {out_path}")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Streaming soak + sub-110ms survival probes")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--pairs", type=Path, default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--rest-interval-s", type=float, default=5.0,
                        help="poll cadence for the Kalshi REST-poll stopgap (rest/fallback modes)")
    parser.add_argument("--kalshi-stream", choices=("auto", "ws", "rest"), default="auto",
                        help="Kalshi leg: auto = authenticated WS if a credential exists else "
                             "REST-poll; ws = require WS (error if no credential); rest = force "
                             "REST-poll stopgap")
    parser.add_argument("--kalshi-profile", default="paper",
                        help="stored credential profile for the Kalshi WS handshake (default: paper)")
    parser.add_argument("--kalshi-ws-url", default=_DEFAULT_KALSHI_WS_URL,
                        help="Kalshi WS endpoint (default tracks the migrated external-api host)")
    parser.add_argument("--stale-after-s", type=float, default=30.0)
    parser.add_argument("--poly-bps", type=int, default=None,
                        help="Pin Polymarket bps for deterministic offline analysis")
    parser.add_argument("--baseline-dir", type=Path, action="append", default=None,
                        help="Old REST-sequential soak dir for the comparison (repeatable)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports" / "survival_streaming")
    parser.add_argument("--report-only", action="store_true",
                        help="Skip capture; regenerate the report from --data-dir")
    args = parser.parse_args(argv)

    if not args.report_only:
        asyncio.run(_soak(args))
    write_report(Path(args.data_dir), [d.resolve() for d in (args.baseline_dir or [])],
                 args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
