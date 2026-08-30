#!/usr/bin/env python3
# Scope: BOT_RUNTIME — CLI entry point for the market-data recorder (Phase 1–2).
"""
Run the market-data recorder.

Universe sources (Phase 2 — coverage):
    --universe-source registry   derive the universe from configs/pairs.approved.yaml
    --universe-source discovery  derive it from discovery JSON dumps (a broad universe)

Continuity (Phase 2 — continuity):
    The recorder runs until stopped (Ctrl-C / SIGTERM); restarts resume cleanly
    by continuing capture_seq and annotating the downtime as a restart gap.
    --supervise auto-restarts the recorder process loop on an unexpected crash.
    --refresh-interval re-resolves the universe periodically (re-running discovery
    when --refresh-discovery is set) so markets are added/retired without a restart.

Usage:
    # Broad discovery universe, refreshed hourly, supervised, run forever:
    python scripts/run_recorder.py --universe-source discovery --refresh-discovery \
        --refresh-interval 3600 --max-markets 300 --supervise

    # Registry universe, 30s cadence (legacy default):
    python scripts/run_recorder.py

    python scripts/run_recorder.py --max-cycles 10   # smoke test, stop after 10 cycles
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def _default_discovery_paths() -> list[Path]:
    return [
        ROOT / "logs" / "kalshi_discovered_markets.json",
        ROOT / "logs" / "polymarket_discovered_markets.json",
    ]


def _run_discovery_refresh(max_markets: int, discovery_paths: list[Path]) -> None:
    """Re-run the discovery scripts so the JSON dumps reflect the live venues."""
    jobs = [
        ("kalshi", ROOT / "scripts" / "discover_kalshi_public_markets.py", discovery_paths[0]),
        ("polymarket", ROOT / "scripts" / "discover_polymarket_public_markets.py", discovery_paths[1]),
    ]
    for venue, script, out_path in jobs:
        cmd = [
            sys.executable, str(script),
            "--output", str(out_path),
            "--max-markets", str(max_markets),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=600)
            print(f"[recorder] discovery refresh ok: {venue} -> {out_path}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            # Never let a discovery failure interrupt collection; the universe
            # provider falls back to the last good JSON dumps.
            print(f"[recorder] discovery refresh failed for {venue}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prediction-market data recorder")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between cycles (default 30)")
    parser.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (default: run forever)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_data_dir(ROOT),
        help="Root data directory (default: data_strategy_30/)",
    )
    parser.add_argument("--universe-source", choices=("registry", "discovery"), default="registry",
                        help="Where to derive the (venue, market_id) universe from")
    parser.add_argument("--pairs-yaml", type=Path, default=ROOT / "configs" / "pairs.approved.yaml",
                        help="Registry YAML to derive universe from (registry source)")
    parser.add_argument("--discovery-path", type=Path, action="append", default=None,
                        help="Discovery JSON dump(s) (discovery source); repeatable. "
                             "Defaults to logs/{kalshi,polymarket}_discovered_markets.json")
    parser.add_argument("--max-markets", type=int, default=300,
                        help="Cap the discovery universe to this many most-active markets (default 300)")
    parser.add_argument("--refresh-interval", type=float, default=None,
                        help="Re-resolve the universe every N seconds (default: never refresh)")
    parser.add_argument("--refresh-discovery", action="store_true",
                        help="On each refresh, re-run the discovery scripts before re-loading (discovery source)")
    parser.add_argument("--supervise", action="store_true",
                        help="Auto-restart the recorder loop on an unexpected crash (run until stopped)")
    parser.add_argument("--restart-backoff", type=float, default=10.0,
                        help="Seconds to wait before restarting after a crash (with --supervise)")
    parser.add_argument("--run-id", type=str, default=None, help="Session ID (auto-generated if omitted)")
    args = parser.parse_args()

    from arbx.data.recorder import (
        build_live_public_connectors,
        load_universe_from_discovery,
        load_universe_from_registry,
        run_recorder,
    )

    run_id = args.run_id or f"rec_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    data_dir = args.output_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    discovery_paths = args.discovery_path or _default_discovery_paths()

    # universe_provider re-resolves the universe on the refresh cadence.
    if args.universe_source == "discovery":
        def universe_provider() -> list[tuple[str, str]]:
            if args.refresh_discovery:
                _run_discovery_refresh(args.max_markets, discovery_paths)
            return load_universe_from_discovery(discovery_paths, max_markets=args.max_markets)
    else:
        def universe_provider() -> list[tuple[str, str]]:
            return load_universe_from_registry(args.pairs_yaml)

    universe = universe_provider()
    print(f"Universe: {len(universe)} markets from {args.universe_source}")
    for venue, mid in universe[:5]:
        print(f"  {venue}: {mid}")
    if len(universe) > 5:
        print(f"  ... and {len(universe) - 5} more")
    if not universe:
        print("[recorder] WARNING: empty universe; nothing to record. "
              "Run discovery first or check --universe-source.")

    connectors = build_live_public_connectors()

    def on_cycle(cycle: int, rows: list) -> None:
        ok = sum(1 for r in rows if r.get("best_bid") is not None or r.get("best_ask") is not None)
        print(f"  cycle {cycle:4d}: {len(rows)} rows, {ok} with quotes")

    # Supervisor owns the stop flag and signals so a crashed inner loop can be
    # restarted without losing the ability to Ctrl-C the whole process.
    import signal as _signal

    stop_event = threading.Event()

    def _handle_signal(sig: int, frame: object) -> None:
        print(f"\n[recorder] received signal {sig}, stopping…")
        stop_event.set()

    _signal.signal(_signal.SIGINT, _handle_signal)
    _signal.signal(_signal.SIGTERM, _handle_signal)

    def _run_once() -> None:
        run_recorder(
            universe,
            data_dir=data_dir,
            run_id=run_id,
            interval_seconds=args.interval,
            connectors=connectors,
            max_cycles=args.max_cycles,
            on_cycle=on_cycle,
            universe_provider=universe_provider,
            universe_refresh_seconds=args.refresh_interval,
            stop_event=stop_event,
        )

    if not args.supervise:
        _run_once()
        return

    while not stop_event.is_set():
        try:
            _run_once()
        except Exception as exc:  # noqa: BLE001 — supervisor restarts on any crash
            print(f"[recorder] crashed: {exc!r}")
        if stop_event.is_set() or args.max_cycles is not None:
            break
        print(f"[recorder] supervisor restarting in {args.restart_backoff}s…")
        stop_event.wait(timeout=args.restart_backoff)
    print("[recorder] supervisor exited")


if __name__ == "__main__":
    main()
