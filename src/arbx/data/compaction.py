# Scope: BOT_RUNTIME — Tier-2 compaction of raw JSONL landing into Parquet/DuckDB (Phase 4).
"""
Compact the recorder's append-only raw JSONL landing (Tier 1) into a columnar
Parquet store (Tier 2) and build a DuckDB warehouse of views over it.

Per docs/dataset_schema.md §3:
  - Only *closed* day-files (date strictly before the watermark, default today
    UTC) are compacted, so this never races the live writer.
  - Dedupe on ``capture_seq`` so a crash/restart that re-appended rows produces
    exactly one row per capture.
  - zstd compression, partitioned ``venue=/date=`` (Hive layout).
  - Idempotent: re-running over the same input yields the same logical content
    (row counts and keys), so it is safe to schedule repeatedly.

DuckDB is an *optional* dependency (`pip install -e '.[analytics]'`); this module
imports it lazily so importing the package never requires it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class CompactionResult:
    book_partitions: list[str] = field(default_factory=list)
    edge_partitions: list[str] = field(default_factory=list)
    rows_written: int = 0
    skipped_open_days: list[str] = field(default_factory=list)
    warehouse_path: Path | None = None


def _sql_str(path: Path | str) -> str:
    """Single-quoted SQL string literal for a filesystem path."""
    return "'" + str(path).replace("'", "''") + "'"


def _duckdb() -> Any:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "compaction requires the optional 'analytics' extra: "
            "pip install -e '.[analytics]'"
        ) from exc
    return duckdb


def _closed_day_files(base: Path, watermark: str) -> list[Path]:
    """Return <date>.jsonl files whose date is strictly before the watermark."""
    out: list[Path] = []
    for f in sorted(base.glob("*.jsonl")):
        date_str = f.stem
        if len(date_str) == 10 and date_str < watermark:
            out.append(f)
    return out


def compact(
    data_dir: Path,
    *,
    watermark: str | None = None,
    build_warehouse: bool = True,
) -> CompactionResult:
    """
    Compact closed raw day-files under ``data_dir`` into ``data_dir/parquet``.

    ``watermark`` is an inclusive-exclusive UTC date string (YYYY-MM-DD); only
    day-files strictly before it are compacted. Defaults to today (UTC), so all
    fully-elapsed days are compacted and the current day is left to the writer.
    """
    duckdb = _duckdb()
    if watermark is None:
        watermark = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    result = CompactionResult()
    parquet_root = data_dir / "parquet"
    con = duckdb.connect()
    try:
        # ---- book_observations: partitioned by venue=/date= ----
        book_base = data_dir / "raw" / "book"
        if book_base.exists():
            for venue_dir in sorted(book_base.glob("venue=*")):
                venue = venue_dir.name.split("=", 1)[1]
                # note open (current-day) files so the caller can see what was held back
                for f in sorted(venue_dir.glob("*.jsonl")):
                    if not (len(f.stem) == 10 and f.stem < watermark):
                        result.skipped_open_days.append(f"book/{venue}/{f.stem}")
                for f in _closed_day_files(venue_dir, watermark):
                    date_str = f.stem
                    out_dir = parquet_root / "book_observations" / f"venue={venue}" / f"date={date_str}"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_file = out_dir / "part-0.parquet"
                    con.execute(
                        f"""
                        COPY (
                            SELECT * EXCLUDE (rn) FROM (
                                SELECT *, row_number() OVER (
                                    PARTITION BY capture_seq ORDER BY recv_monotonic_ns
                                ) AS rn
                                FROM read_json_auto({_sql_str(f)}, format='newline_delimited', union_by_name=true)
                            ) WHERE rn = 1
                        ) TO {_sql_str(out_file)} (FORMAT PARQUET, COMPRESSION ZSTD)
                        """
                    )
                    cnt = con.execute(
                        f"SELECT count(*) FROM read_parquet({_sql_str(out_file)})"
                    ).fetchone()[0]
                    result.book_partitions.append(str(out_file))
                    result.rows_written += int(cnt)

        # ---- edge_observations (if the Phase 5 layer wrote any) ----
        edge_base = data_dir / "raw" / "edge"
        if edge_base.exists():
            for f in sorted(edge_base.glob("*.jsonl")):
                if not (len(f.stem) == 10 and f.stem < watermark):
                    result.skipped_open_days.append(f"edge/{f.stem}")
                    continue
                date_str = f.stem
                out_dir = parquet_root / "edge_observations" / f"date={date_str}"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file = out_dir / "part-0.parquet"
                con.execute(
                    f"""
                    COPY (
                        SELECT * FROM read_json_auto({_sql_str(f)}, format='newline_delimited', union_by_name=true)
                    ) TO {_sql_str(out_file)} (FORMAT PARQUET, COMPRESSION ZSTD)
                    """
                )
                result.edge_partitions.append(str(out_file))

        if build_warehouse:
            result.warehouse_path = _build_warehouse(duckdb, data_dir, parquet_root)
    finally:
        con.close()

    return result


def _build_warehouse(duckdb: Any, data_dir: Path, parquet_root: Path) -> Path | None:
    """Create data/warehouse.duckdb with views over the Parquet globs."""
    book_glob = parquet_root / "book_observations" / "**" / "*.parquet"
    edge_glob = parquet_root / "edge_observations" / "**" / "*.parquet"
    has_book = any((parquet_root / "book_observations").rglob("*.parquet")) if (parquet_root / "book_observations").exists() else False
    if not has_book:
        return None
    has_edge = (parquet_root / "edge_observations").exists() and any(
        (parquet_root / "edge_observations").rglob("*.parquet")
    )

    wh_path = data_dir / "warehouse.duckdb"
    con = duckdb.connect(str(wh_path))
    try:
        con.execute(
            f"""
            CREATE OR REPLACE VIEW book_observations AS
            SELECT * FROM read_parquet({_sql_str(book_glob)}, hive_partitioning=true);
            """
        )
        # latency_observations: Tier-0 data-fetch latency derived from book rows.
        con.execute(
            """
            CREATE OR REPLACE VIEW latency_observations AS
            SELECT
                venue,
                CAST(capture_ts_utc AS TIMESTAMP) AS observed_at,
                recv_monotonic_ns,
                CAST(
                    extract('hour' FROM CAST(capture_ts_utc AS TIMESTAMP))
                    AS INTEGER
                ) AS hour_utc,
                'data_fetch'        AS kind,
                fetch_elapsed_ms    AS latency_ms,
                'public'            AS source,
                run_id
            FROM book_observations
            WHERE fetch_elapsed_ms IS NOT NULL;
            """
        )
        if has_edge:
            con.execute(
                f"""
                CREATE OR REPLACE VIEW edge_observations AS
                SELECT * FROM read_parquet({_sql_str(edge_glob)}, hive_partitioning=true);
                """
            )
    finally:
        con.close()
    return wh_path
