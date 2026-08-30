#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# CLI: record a pair decision; archive moves the entry.
#
# Appends an append-only decision_log entry ({at, decision, rationale, auditor})
# to the pair in configs/pairs.approved.yaml and re-hashes the sha256 sidecar.
# `--decision archive` additionally moves the entry (full history) to
# configs/pairs.archived.yaml. load_pairs() enforces the consequence: only
# verified-equivalent pairs whose latest decision is `approve` keep
# include_in_strategy_metrics.
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.pairs.equivalence import (  # noqa: E402
    VALID_DECISIONS,
    archive_pair,
    record_decision,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a pair approve/reject decision")
    parser.add_argument("--pair", required=True,
                        help="pair_key or kalshi_market_id")
    parser.add_argument("--decision", required=True, choices=sorted(VALID_DECISIONS))
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--auditor", default="operator")
    parser.add_argument("--registry", type=Path,
                        default=ROOT / "configs" / "pairs.approved.yaml")
    args = parser.parse_args(argv)

    try:
        record_decision(args.registry, args.pair, args.decision,
                        args.rationale, args.auditor)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{args.pair}: recorded '{args.decision}' ({args.auditor})")

    if args.decision == "archive":
        archived = archive_pair(args.registry, args.pair)
        print(f"{args.pair}: moved to {archived.name} with full history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
