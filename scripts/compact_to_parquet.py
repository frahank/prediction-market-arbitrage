#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — CLI for Tier-2 Parquet/DuckDB compaction (Phase 4).
"""
Compact the recorder's raw JSONL landing into Parquet + a DuckDB warehouse.

Requires the optional analytics extra:
    pip install -e '.[analytics]'

Usage:
    python scripts/compact_to_parquet.py                       # compact closed days under data/
    python scripts/compact_to_parquet.py --watermark 2026-06-30
    python scripts/compact_to_parquet.py --no-warehouse

After compaction, query the modeling dataset with DuckDB:
    duckdb data/warehouse.duckdb "SELECT venue, count(*) FROM book_observations GROUP BY venue;"
"""
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
    parser = argparse.ArgumentParser(description="Compact raw JSONL landing to Parquet/DuckDB")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--watermark", type=str, default=None,
                        help="Compact day-files strictly before this UTC date (default: today)")
    parser.add_argument("--no-warehouse", action="store_true", help="Skip building warehouse.duckdb")
    args = parser.parse_args(argv)

    from arbx.data.compaction import compact

    try:
        result = compact(
            args.data_dir.resolve(),
            watermark=args.watermark,
            build_warehouse=not args.no_warehouse,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"book partitions written: {len(result.book_partitions)}")
    print(f"edge partitions written: {len(result.edge_partitions)}")
    print(f"rows written: {result.rows_written}")
    if result.skipped_open_days:
        print(f"held back (open/current day): {', '.join(result.skipped_open_days)}")
    if result.warehouse_path:
        print(f"warehouse: {result.warehouse_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
