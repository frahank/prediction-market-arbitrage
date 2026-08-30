#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — CLI for the recorder data-quality report (Phase 4).
"""
Data-quality report for the market-data recorder dataset.

Usage:
    python scripts/data_quality.py                       # report over data/
    python scripts/data_quality.py --data-dir data --interval 30
    python scripts/data_quality.py --json                # machine-readable
    python scripts/data_quality.py --strict              # exit 1 if thresholds fail

The report is the acceptance gate for an unattended soak (docs/dataset_schema.md §5).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recorder data-quality report")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Nominal recorder cadence in seconds (for gap detection)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any threshold fails")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Parse source JSONL without using <data-dir>/cache/dq partials",
    )
    args = parser.parse_args(argv)

    from arbx.data.quality import analyze

    data_dir = args.data_dir.resolve()
    cache_dir = None if args.no_cache else data_dir / "cache" / "dq"
    report = analyze(data_dir, nominal_interval_s=args.interval, cache_dir=cache_dir)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(report.render_text())

    if args.strict and not report.passed():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
