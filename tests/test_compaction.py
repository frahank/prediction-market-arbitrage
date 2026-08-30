# Unit tests for Parquet/DuckDB compaction (optional analytics extra).
from __future__ import annotations

import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb", reason="requires the optional 'analytics' extra")

from arbx.data.compaction import compact  # noqa: E402


def _book(data_dir: Path, venue: str, date: str, rows: list[dict]) -> None:
    d = data_dir / "raw" / "book" / f"venue={venue}"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{date}.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(seq: int, *, venue="kalshi", market="M", fetch=50.0):
    return {
        "venue": venue, "market_id": market, "capture_seq": seq,
        "capture_ts_utc": "2026-06-27T01:00:00+00:00", "venue_book_ts": "2026-06-27T01:00:00+00:00",
        "recv_monotonic_ns": seq * 1_000_000, "best_bid": 0.5, "best_ask": 0.55,
        "fetch_elapsed_ms": fetch, "run_id": "r", "book_json": "{}",
    }


def test_compacts_closed_day_to_parquet(tmp_path: Path):
    _book(tmp_path, "kalshi", "2026-06-27", [_row(1), _row(2)])
    result = compact(tmp_path, watermark="2026-06-28")
    assert result.book_partitions
    out = tmp_path / "parquet" / "book_observations" / "venue=kalshi" / "date=2026-06-27" / "part-0.parquet"
    assert out.exists()
    assert result.rows_written == 2


def test_dedupes_capture_seq(tmp_path: Path):
    _book(tmp_path, "kalshi", "2026-06-27", [_row(1), _row(1), _row(2)])  # dup seq 1
    result = compact(tmp_path, watermark="2026-06-28")
    assert result.rows_written == 2  # deduped


def test_holds_back_open_current_day(tmp_path: Path):
    _book(tmp_path, "kalshi", "2026-06-27", [_row(1)])
    _book(tmp_path, "kalshi", "2026-06-28", [_row(2)])  # >= watermark, held back
    result = compact(tmp_path, watermark="2026-06-28")
    assert any("2026-06-28" in s for s in result.skipped_open_days)
    assert result.rows_written == 1


def test_idempotent_rowcount(tmp_path: Path):
    _book(tmp_path, "kalshi", "2026-06-27", [_row(1), _row(2)])
    r1 = compact(tmp_path, watermark="2026-06-28")
    r2 = compact(tmp_path, watermark="2026-06-28")
    assert r1.rows_written == r2.rows_written == 2


def test_warehouse_views_queryable(tmp_path: Path):
    _book(tmp_path, "kalshi", "2026-06-27", [_row(1), _row(2)])
    result = compact(tmp_path, watermark="2026-06-28")
    assert result.warehouse_path is not None
    con = duckdb.connect(str(result.warehouse_path))
    try:
        n = con.execute("SELECT count(*) FROM book_observations").fetchone()[0]
        assert n == 2
        # latency_observations view derives data_fetch rows
        kinds = con.execute("SELECT DISTINCT kind, source FROM latency_observations").fetchall()
        assert ("data_fetch", "public") in kinds
        observed_at, hour_utc = con.execute(
            "SELECT observed_at, hour_utc FROM latency_observations LIMIT 1"
        ).fetchone()
        assert observed_at.isoformat() == "2026-06-27T01:00:00"
        assert hour_utc == 1
    finally:
        con.close()
