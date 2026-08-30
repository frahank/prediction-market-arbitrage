#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Quick live-glance monitor for an in-flight recorder soak.
"""
Quick status of a running (or finished) recorder soak.

Usage:
    python scripts/recorder_status.py                 # one-shot glance over data/
    python scripts/recorder_status.py --watch         # refresh every 10s
    python scripts/recorder_status.py --watch --every 30

Reads only the continuity log + today's book files, so it is safe to run
against a live recorder without disturbing it. Pure stdlib.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _parse_ts(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def snapshot(data_dir: Path) -> str:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    lines: list[str] = [f"Recorder status @ {now.strftime('%Y-%m-%d %H:%M:%S')}Z — {data_dir}"]

    # book rows per venue (today)
    book_base = data_dir / "raw" / "book"
    if book_base.exists():
        for venue_dir in sorted(book_base.glob("venue=*")):
            venue = venue_dir.name.split("=", 1)[1]
            f = venue_dir / f"{today}.jsonl"
            n = sum(1 for _ in _iter_jsonl(f)) if f.exists() else 0
            lines.append(f"  {venue:11s} rows today: {n}")
    else:
        lines.append("  (no book data yet)")

    # continuity
    last_hb = None
    last_cycle = None
    last_run = None
    recent_events: list[str] = []
    latency_base = data_dir / "raw" / "latency"
    if latency_base.exists():
        for f in sorted(latency_base.glob("*.jsonl")):
            for row in _iter_jsonl(f):
                kind = row.get("kind")
                if kind == "recorder_heartbeat":
                    last_hb = _parse_ts(row.get("observed_at")) or last_hb
                    last_cycle = row.get("cycle", last_cycle)
                    last_run = row.get("run_id", last_run)
                if kind in ("recorder_gap", "recorder_start", "recorder_stop", "universe_change"):
                    recent_events.append(
                        f"    {row.get('observed_at') or row.get('actual_at','')} {kind} "
                        f"{row.get('reason','')} {('+'+str(len(row['added']))+'/-'+str(len(row['retired']))) if kind=='universe_change' else ''}".rstrip()
                    )

    if last_hb is not None:
        age = (now - last_hb).total_seconds()
        health = "ALIVE" if age < 120 else "STALE"
        lines.append(f"  last heartbeat: {age:.0f}s ago [{health}]  run={last_run} cycle={last_cycle}")
    else:
        lines.append("  last heartbeat: none")

    if recent_events:
        lines.append("  recent events:")
        lines.extend(recent_events[-8:])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live recorder status monitor")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--watch", action="store_true", help="Refresh continuously")
    parser.add_argument("--every", type=float, default=10.0, help="Refresh interval seconds (--watch)")
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()
    if not args.watch:
        print(snapshot(data_dir))
        return 0
    try:
        while True:
            print("\033[2J\033[H", end="")  # clear screen
            print(snapshot(data_dir))
            time.sleep(args.every)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
