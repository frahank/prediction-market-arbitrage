#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — P4-T4 CLI: targeted single-pair soak + evidence pack.
#
# Runs one approved pair through the hybrid streaming capture for --hours and
# writes the full evidence pack (dq_summary, episodes, survival_summary,
# liquidity_profile, strike_map, manifest) under evidence/<market>/<date>/.
# Self-terminating: exits on its own when the deadline passes. Public data only.
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Targeted per-pair streaming soak + evidence pack")
    parser.add_argument("--pair", required=True,
                        help="Kalshi market id (e.g. KXWCCONTINENT-26-NA) or pair_key")
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Capture landing dir (default: evidence/<market>/<date>/data)")
    parser.add_argument("--pairs", type=Path,
                        default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--rest-interval-s", type=float, default=5.0)
    parser.add_argument("--stale-after-s", type=float, default=30.0)
    parser.add_argument("--poly-bps", type=int, default=None,
                        help="Pin Polymarket bps for deterministic offline analysis")
    args = parser.parse_args(argv)

    from scripts.analyze_edge_episodes import build_fee_engine

    from arbx.pairs.registry import load_pairs
    from arbx.pairs.targeted_soak import run_targeted_soak

    pairs = load_pairs(args.pairs)
    pair = next((p for p in pairs
                 if p.kalshi_market_id == args.pair or p.pair_key == args.pair), None)
    if pair is None:
        print(f"pair {args.pair!r} not found in {args.pairs}", file=sys.stderr)
        return 1

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = args.data_dir or (
        ROOT / "evidence" / pair.kalshi_market_id / date / "data")
    evidence_dir = run_targeted_soak(
        pair, args.hours, data_dir,
        fee_engine=build_fee_engine(args.poly_bps),
        rest_interval_s=args.rest_interval_s,
        stale_after_s=args.stale_after_s,
    )
    print(f"Targeted soak complete — evidence pack at {evidence_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
