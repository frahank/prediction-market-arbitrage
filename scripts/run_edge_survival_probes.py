#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Public edge-survival probe runner for verified pairs.
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run public-data edge survival probes for verified strategy pairs"
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--pairs-yaml", type=Path, default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--fee-round-trip", type=float, default=0.02)
    parser.add_argument("--target-size", type=float, default=1.0,
                        help="Visible two-leg depth target in outcome units/contracts")
    parser.add_argument("--max-pairs", type=int, default=1,
                        help="Limit pairs probed in one command (default: 1)")
    parser.add_argument("--pair-key", action="append", default=None,
                        help="Probe only pairs whose pair_key contains this substring "
                             "(repeatable). Use to target transient candidates, e.g. "
                             "--pair-key KXALIENS-27 --pair-key KXPRESNOMD-28-AOC. "
                             "When set, --max-pairs is ignored.")
    parser.add_argument("--delay-ms", type=int, action="append", default=None,
                        help="Probe delay in ms; repeatable. Default: 50,100,250,500,1000")
    parser.add_argument("--dry-run", action="store_true", help="Do not append edge rows")
    args = parser.parse_args(argv)

    from arbx.analysis.edges import load_edge_pairs, write_edges_jsonl
    from arbx.analysis.survival import (
        DEFAULT_PROBE_DELAYS_MS,
        run_public_edge_survival_probe,
    )
    from arbx.data.recorder import build_live_public_connectors

    delays = tuple(args.delay_ms) if args.delay_ms else DEFAULT_PROBE_DELAYS_MS
    strategy_pairs = [
        pair for pair in load_edge_pairs(args.pairs_yaml)
        if pair.include_in_strategy_metrics
    ]
    if args.pair_key:
        wanted = tuple(args.pair_key)
        pairs = [p for p in strategy_pairs if any(sel in p.pair_key for sel in wanted)]
        if not pairs:
            print(f"No strategy pairs matched --pair-key {wanted}.", file=sys.stderr)
            return 1
    else:
        pairs = strategy_pairs[: max(1, args.max_pairs)]
    if not pairs:
        print("No strategy pairs available to probe.", file=sys.stderr)
        return 1

    connectors = build_live_public_connectors()
    rows = []
    for pair in pairs:
        pair_rows = run_public_edge_survival_probe(
            pair,
            connectors,
            fee_round_trip=args.fee_round_trip,
            target_size=args.target_size,
            delays_ms=delays,
        )
        rows.extend(pair_rows)
        survived = sum(1 for row in pair_rows if row.get("benchmark_ms") and row.get("survived"))
        tiers = sorted({row.get("survival_tier") for row in pair_rows if row.get("survival_tier")})
        tier_label = ", ".join(tiers) if tiers else "none"
        print(f"{pair.pair_key}: {len(pair_rows)} rows, {survived} delayed survived rows, tier={tier_label}")

    if args.dry_run:
        print(f"Dry run: would write {len(rows)} edge rows")
        return 0
    out = write_edges_jsonl(args.data_dir.resolve(), rows)
    print(f"Wrote {len(rows)} public probe edge rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
