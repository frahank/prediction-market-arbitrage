#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Run a capture session (concurrent REST or streaming) to JSONL.
#
# Writes book_observations rows through ObservationSink (same Tier-1 layout as
# the recorder, so the DQ CLI and edge tooling read them unchanged) plus a
# capture_summary.json with per-venue update rates, skew percentiles, and gap
# counts. Public market data only; subscribe-only WS; no credentials.
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402

from arbx.capture.clock import measure_ntp_offset_ms  # noqa: E402
from arbx.capture.engine import KalshiRestPollStream, StreamingSource  # noqa: E402
from arbx.capture.rest_concurrent import ConcurrentRestSource  # noqa: E402
from arbx.capture.sink import ObservationSink  # noqa: E402
from arbx.data.recorder import NTP_REFRESH_SECONDS, _HeartbeatWriter  # noqa: E402
from arbx.pairs.registry import load_pairs  # noqa: E402

MODES = ("rest_concurrent", "streaming")


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(len(sorted_values) * pct / 100))
    return sorted_values[index]


def _load_capture_config(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except OSError:
        return {}


async def _until_deadline(source, deadline: float):
    """Yield source items only until an absolute monotonic deadline.

    The prior loop checked time only after a successful source yield. A total
    venue outage therefore made ``--duration`` ineffective because no item ever
    reached that check. ``wait_for`` bounds the pending ``anext`` itself and its
    cancellation closes network clients through the source generator's
    ``finally`` block.
    """
    iterator = aiter(source)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                item = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except (TimeoutError, StopAsyncIteration):
                return
            yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


async def _run(args: argparse.Namespace) -> dict:
    config = _load_capture_config(args.config)
    mode = args.mode or str(config.get("mode", "rest_concurrent"))
    if mode not in MODES:
        raise SystemExit(f"unknown mode {mode!r}; expected one of {MODES}")
    interval_s = float(config.get("rest_interval_s", 5))
    stale_after_s = float(config.get("ws_stale_after_s", 30))
    ntp_server = str(config.get("ntp_server", "time.google.com"))

    pairs = load_pairs(args.pairs)
    run_id = f"capture_{mode}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    data_dir = Path(args.data_dir)

    ntp = measure_ntp_offset_ms(server=ntp_server)
    sink = ObservationSink(data_dir, run_id=run_id, ntp_offset_ms=ntp)
    heartbeat = _HeartbeatWriter(data_dir)
    heartbeat.write_event(
        "recorder_start", run_id, universe_size=len(pairs) * 2,
        interval_seconds=interval_s, ntp_offset_ms=ntp, capture_mode=mode,
    )

    if mode == "streaming":
        source = StreamingSource(
            pairs,
            stale_after_s=stale_after_s,
            kalshi_stream=KalshiRestPollStream(interval_s=interval_s),
            heartbeat=heartbeat,
            run_id=run_id,
        )
    else:
        source = ConcurrentRestSource(pairs, interval_s=interval_s)

    skews: list[float] = []
    paired_count = 0
    last_ntp_at = time.monotonic()
    deadline = time.monotonic() + args.duration
    print(f"[run_capture] mode={mode} run_id={run_id} pairs={len(pairs)} "
          f"duration={args.duration:.0f}s ntp_offset_ms={ntp}")
    try:
        async for paired in _until_deadline(source.subscribe(pairs), deadline):
            sink.write_paired(paired)
            skews.append(paired.skew_ms)
            paired_count += 1
            if time.monotonic() - last_ntp_at >= NTP_REFRESH_SECONDS:
                last_ntp_at = time.monotonic()
                refreshed = measure_ntp_offset_ms(server=ntp_server)
                if refreshed is not None:
                    sink.ntp_offset_ms = refreshed
    finally:
        sink.close()

    abs_skews = sorted(abs(s) for s in skews)
    summary = {
        "run_id": run_id,
        "mode": mode,
        "duration_s": args.duration,
        "pairs": len(pairs),
        "paired_snapshots": paired_count,
        "book_rows": paired_count * 2,
        "per_venue_updates": (
            dict(source.updates) if isinstance(source, StreamingSource)
            else {"kalshi": paired_count, "polymarket": paired_count}
        ),
        "updates_per_min": round(paired_count / max(args.duration / 60.0, 1e-9), 1),
        "skew_ms": {
            "p50": _percentile(abs_skews, 50),
            "p95": _percentile(abs_skews, 95),
            "p99": _percentile(abs_skews, 99),
            "max": abs_skews[-1] if abs_skews else None,
        },
        "gap_count": source.gap_count if isinstance(source, StreamingSource) else 0,
        "ntp_offset_ms_last": sink.ntp_offset_ms,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    heartbeat.write_event(
        "recorder_stop", run_id, cycles=paired_count, last_seq=paired_count * 2,
    )
    heartbeat.close()
    summary_path = data_dir / "capture_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[run_capture] paired={paired_count} |skew_ms| p50={summary['skew_ms']['p50']} "
          f"p95={summary['skew_ms']['p95']} gaps={summary['gap_count']}")
    print(f"[run_capture] summary written: {summary_path}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a capture session")
    parser.add_argument("--mode", choices=MODES, default=None,
                        help="Capture mode (default: configs/capture.yaml)")
    parser.add_argument("--pairs", type=Path, default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "capture.yaml")
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
