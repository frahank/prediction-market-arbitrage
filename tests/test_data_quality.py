# Unit tests for the data-quality report.
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbx.data.quality import analyze


def _write_book(data_dir: Path, venue: str, date: str, rows: list[dict]) -> None:
    d = data_dir / "raw" / "book" / f"venue={venue}"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{date}.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_latency(data_dir: Path, date: str, rows: list[dict]) -> None:
    d = data_dir / "raw" / "latency"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{date}.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _book_row(seq: int, ts: datetime, *, venue="kalshi", market="M", book_ts=None, fetch=50.0):
    return {
        "venue": venue, "market_id": market, "capture_seq": seq,
        "capture_ts_utc": ts.isoformat(),
        "venue_book_ts": (book_ts or ts).isoformat(),
        "recv_monotonic_ns": seq * 1_000_000,
        "best_bid": 0.5, "best_ask": 0.55, "fetch_elapsed_ms": fetch,
    }


def test_counts_rows_and_markets(tmp_path: Path):
    base = datetime(2026, 6, 28, 0, 0, tzinfo=timezone.utc)
    rows = [_book_row(i, base + timedelta(seconds=30 * i)) for i in range(1, 4)]
    _write_book(tmp_path, "kalshi", "2026-06-28", rows)
    report = analyze(tmp_path, nominal_interval_s=30)
    assert report.total_book_rows == 3
    assert len(report.markets) == 1
    assert report.markets[0].rows == 3


def test_detects_duplicate_capture_seq(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    rows = [_book_row(1, base), _book_row(1, base + timedelta(seconds=30))]  # dup seq
    _write_book(tmp_path, "kalshi", "2026-06-28", rows)
    report = analyze(tmp_path)
    assert report.duplicate_seq == 1


def test_detects_gap(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    # 3rd row is 5 min after 2nd -> a gap at 30s nominal (>2x)
    times = [base, base + timedelta(seconds=30), base + timedelta(seconds=330)]
    rows = [_book_row(i + 1, t) for i, t in enumerate(times)]
    _write_book(tmp_path, "kalshi", "2026-06-28", rows)
    report = analyze(tmp_path, nominal_interval_s=30)
    mq = report.markets[0]
    assert mq.gap_intervals == 1
    assert mq.gap_rate > 0


def test_detects_staleness(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    fresh = _book_row(1, base, book_ts=base)
    stale = _book_row(2, base + timedelta(seconds=30), book_ts=base - timedelta(seconds=120))
    _write_book(tmp_path, "kalshi", "2026-06-28", [fresh, stale])
    report = analyze(tmp_path)
    assert report.markets[0].stale_rows == 1


def test_fetch_latency_percentiles(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    rows = [_book_row(i, base + timedelta(seconds=30 * i), fetch=float(i * 10)) for i in range(1, 11)]
    _write_book(tmp_path, "kalshi", "2026-06-28", rows)
    report = analyze(tmp_path)
    assert report.fetch_ms_p50 is not None
    assert report.fetch_ms_p99 >= report.fetch_ms_p50


def test_reads_continuity_log(tmp_path: Path):
    _write_latency(tmp_path, "2026-06-28", [
        {"kind": "recorder_start", "run_id": "r1", "observed_at": "2026-06-28T00:00:00+00:00",
         "resumed_from_seq": 0, "ntp_offset_ms": None},
        {"kind": "recorder_heartbeat", "observed_at": "2026-06-28T00:00:30+00:00"},
        {"kind": "recorder_gap", "reason": "restart"},
        {"kind": "recorder_gap", "reason": "late_cycle"},
        {"kind": "universe_change"},
    ])
    report = analyze(tmp_path)
    assert report.heartbeats == 1
    assert report.restart_gaps == 1
    assert report.late_gaps == 1
    assert report.universe_changes == 1
    assert len(report.runs) == 1


def test_thresholds_pass_on_clean_data(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    rows = [_book_row(i, base + timedelta(seconds=30 * i)) for i in range(1, MIN_ROWS + 1)]
    _write_book(tmp_path, "kalshi", "2026-06-28", rows)
    report = analyze(tmp_path, nominal_interval_s=30)
    res = report.threshold_results()
    assert res["rows_per_market_day"][0] is True
    assert res["gap_rate"][0] is True
    assert res["duplicate_seq"][0] is True
    assert report.passed()


def test_flags_bad_ntp_offset(tmp_path: Path):
    _write_latency(tmp_path, "2026-06-28", [
        {"kind": "recorder_start", "run_id": "r", "observed_at": "2026-06-28T00:00:00+00:00",
         "ntp_offset_ms": 250.0},
    ])
    report = analyze(tmp_path)
    assert report.threshold_results()["ntp_offset"][0] is False


def test_staleness_is_not_part_of_the_acceptance_gate(tmp_path: Path):
    # A market that is healthy on continuity but has a stale row should still
    # pass the recorder-health gate; freshness is reported separately.
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    rows = [_book_row(i, base + timedelta(seconds=30 * i), venue="polymarket") for i in range(1, MIN_ROWS + 1)]
    rows.append(_book_row(MIN_ROWS + 1, base + timedelta(seconds=30 * (MIN_ROWS + 1)),
                          venue="polymarket", book_ts=base - timedelta(seconds=600)))  # very stale
    _write_book(tmp_path, "polymarket", "2026-06-28", rows)
    report = analyze(tmp_path, nominal_interval_s=30)
    assert report.markets[0].stale_rows == 1
    assert report.recorder_health_passed() is True
    assert report.passed() is True  # gate == recorder health
    fv = report.freshness_by_venue()["polymarket"]
    assert fv["stale"] == 1
    assert fv["fresh"] == MIN_ROWS


def test_freshness_by_venue_counts_missing_book(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    empty = _book_row(1, base)
    empty["best_bid"] = None
    empty["best_ask"] = None
    _write_book(tmp_path, "kalshi", "2026-06-28", [empty])
    report = analyze(tmp_path)
    assert report.freshness_by_venue()["kalshi"]["missing_book"] == 1


def test_uses_stored_freshness_status_when_present(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    row = _book_row(1, base)  # timestamps say fresh...
    row["freshness_status"] = "stale"  # ...but the recorder stamped stale
    _write_book(tmp_path, "kalshi", "2026-06-28", [row])
    report = analyze(tmp_path)
    assert report.markets[0].stale_rows == 1


def test_cache_matches_uncached_and_is_reused(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    rows = [_book_row(i, base + timedelta(seconds=30 * i), fetch=float(i)) for i in range(1, 25)]
    rows.append(_book_row(7, base))  # a duplicate seq for good measure
    _write_book(tmp_path, "kalshi", "2026-06-28", rows)
    cache = tmp_path / "cache"

    fresh = analyze(tmp_path, nominal_interval_s=30).to_dict()
    cached_cold = analyze(tmp_path, nominal_interval_s=30, cache_dir=cache).to_dict()
    cached_warm = analyze(tmp_path, nominal_interval_s=30, cache_dir=cache).to_dict()

    assert cached_cold == fresh
    assert cached_warm == fresh
    # A cache file was written for the source day-file.
    assert any(cache.glob("*.json"))


def test_cache_invalidates_when_file_changes(tmp_path: Path):
    base = datetime(2026, 6, 28, tzinfo=timezone.utc)
    _write_book(tmp_path, "kalshi", "2026-06-28", [_book_row(1, base)])
    cache = tmp_path / "cache"
    first = analyze(tmp_path, cache_dir=cache).to_dict()
    assert first["total_book_rows"] == 1
    # Append a new row -> size/mtime change -> cache must re-read.
    _write_book(tmp_path, "kalshi", "2026-06-28", [_book_row(2, base + timedelta(seconds=30))])
    second = analyze(tmp_path, cache_dir=cache).to_dict()
    assert second["total_book_rows"] == 2


MIN_ROWS = 1_440
