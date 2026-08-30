# Scope: TEST — Unit tests for the Phase 1 market-data recorder.
"""
Tests for market_recorder.py.

All tests are offline — no real HTTP calls. The recorder's connector is
injected via the connectors parameter so live venue adapters are never used.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel
from arbx.data.recorder import (
    _book_to_observation,
    _DailyWriter,
    _last_heartbeat_at,
    _resume_seq,
    load_universe_from_discovery,
    load_universe_from_list,
    run_recorder,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_book(venue: str = "kalshi", market_id: str = "TEST-MKT") -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id=market_id,
        yes_levels=(OrderBookLevel(0.60, 100.0), OrderBookLevel(0.55, 200.0)),
        no_levels=(OrderBookLevel(0.35, 150.0), OrderBookLevel(0.30, 250.0)),
        timestamp=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 6, 28, 12, 0, 0, 50000, tzinfo=timezone.utc),
        connector_source=ConnectorSource.LIVE_PUBLIC,
        reportable=True,
    )


def _mock_connector(book: OrderBook) -> MagicMock:
    conn = MagicMock()
    conn.fetch_orderbook.return_value = book
    return conn


# ---------------------------------------------------------------------------
# _book_to_observation
# ---------------------------------------------------------------------------

def test_observation_has_required_schema_fields():
    book = _make_book()
    row = _book_to_observation(
        book,
        capture_seq=1,
        recv_monotonic_ns=12345678,
        capture_ts_utc=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetch_elapsed_ms=42.5,
        run_id="test-run",
    )
    required = [
        "venue", "market_id", "capture_seq", "capture_ts_utc", "recv_monotonic_ns",
        "venue_book_ts", "best_bid", "best_ask", "mid", "spread",
        "fetch_elapsed_ms", "connector_source", "reportable", "run_id",
        "bid_px_1", "bid_sz_1", "ask_px_1", "ask_sz_1", "book_json",
    ]
    for field in required:
        assert field in row, f"missing field: {field}"


def test_observation_best_bid_ask():
    book = _make_book()
    row = _book_to_observation(
        book,
        capture_seq=1,
        recv_monotonic_ns=0,
        capture_ts_utc=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetch_elapsed_ms=10.0,
        run_id="r",
    )
    # best_bid = best yes level price
    assert row["best_bid"] == pytest.approx(0.60)
    # best_ask = 1 - best no level price (0.35 NO bid => 0.65 YES ask)
    assert row["best_ask"] == pytest.approx(0.65)
    assert row["mid"] == pytest.approx(0.625)
    assert row["spread"] == pytest.approx(0.05)


def test_observation_top5_levels():
    book = _make_book()
    row = _book_to_observation(
        book,
        capture_seq=1,
        recv_monotonic_ns=0,
        capture_ts_utc=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetch_elapsed_ms=0.0,
        run_id="r",
    )
    assert row["bid_px_1"] == pytest.approx(0.60)
    assert row["bid_sz_1"] == pytest.approx(100.0)
    assert row["bid_px_2"] == pytest.approx(0.55)
    assert row["bid_px_3"] is None  # only 2 levels in fixture


def test_observation_empty_book():
    book = OrderBook(
        venue="kalshi",
        market_id="EMPTY",
        yes_levels=(),
        no_levels=(),
        timestamp=datetime(2026, 6, 28, tzinfo=timezone.utc),
        connector_source=ConnectorSource.LIVE_PUBLIC,
    )
    row = _book_to_observation(
        book,
        capture_seq=5,
        recv_monotonic_ns=0,
        capture_ts_utc=datetime(2026, 6, 28, tzinfo=timezone.utc),
        fetch_elapsed_ms=0.0,
        run_id="r",
    )
    assert row["best_bid"] is None
    assert row["best_ask"] is None
    assert row["mid"] is None
    assert row["spread"] is None


def test_observation_book_json_round_trips():
    book = _make_book()
    row = _book_to_observation(
        book,
        capture_seq=1,
        recv_monotonic_ns=0,
        capture_ts_utc=datetime(2026, 6, 28, tzinfo=timezone.utc),
        fetch_elapsed_ms=0.0,
        run_id="r",
    )
    parsed = json.loads(row["book_json"])
    assert len(parsed["yes_levels"]) == 2
    assert len(parsed["no_levels"]) == 2
    assert parsed["yes_levels"][0]["price"] == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# _DailyWriter
# ---------------------------------------------------------------------------

def test_daily_writer_creates_file_and_appends(tmp_path: Path):
    writer = _DailyWriter(tmp_path, "kalshi")
    writer.write({"venue": "kalshi", "capture_seq": 1, "market_id": "T"})
    writer.write({"venue": "kalshi", "capture_seq": 2, "market_id": "T"})
    writer.close()

    files = list((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["capture_seq"] == 1
    assert lines[1]["capture_seq"] == 2


def test_daily_writer_separate_venues(tmp_path: Path):
    wk = _DailyWriter(tmp_path, "kalshi")
    wp = _DailyWriter(tmp_path, "polymarket")
    wk.write({"venue": "kalshi"})
    wp.write({"venue": "polymarket"})
    wk.close()
    wp.close()

    assert (tmp_path / "raw" / "book" / "venue=kalshi").exists()
    assert (tmp_path / "raw" / "book" / "venue=polymarket").exists()


# ---------------------------------------------------------------------------
# _resume_seq
# ---------------------------------------------------------------------------

def test_resume_seq_empty_dir(tmp_path: Path):
    assert _resume_seq(tmp_path) == 0


def test_resume_seq_picks_up_existing(tmp_path: Path):
    d = tmp_path / "raw" / "book" / "venue=kalshi"
    d.mkdir(parents=True)
    (d / "2026-06-28.jsonl").write_text(
        json.dumps({"capture_seq": 42}) + "\n" +
        json.dumps({"capture_seq": 99}) + "\n"
    )
    assert _resume_seq(tmp_path) == 99


# ---------------------------------------------------------------------------
# load_universe_from_list
# ---------------------------------------------------------------------------

def test_load_universe_from_list():
    items = [("kalshi", "MKT-1"), ("polymarket", "0xabc")]
    assert load_universe_from_list(items) == items


# ---------------------------------------------------------------------------
# load_universe_from_registry — Polymarket fetch id (YES token, not condition)
# ---------------------------------------------------------------------------

def test_registry_universe_prefers_yes_token_id(tmp_path: Path):
    from arbx.data.recorder import load_universe_from_registry
    yaml_path = tmp_path / "pairs.yaml"
    yaml_path.write_text(
        "pairs:\n"
        "  - kalshi_market_id: KX-A\n"
        "    polymarket_market_id: 0xcond\n"
        "    polymarket_identifiers:\n"
        "      condition_id: 0xcond\n"
        "      yes_token_id: '12345'\n"
        "      no_token_id: '67890'\n"
    )
    uni = load_universe_from_registry(yaml_path)
    assert ("kalshi", "KX-A") in uni
    # the YES token id, not the condition id, is what the CLOB book fetch needs
    assert ("polymarket", "12345") in uni
    assert ("polymarket", "0xcond") not in uni


def test_registry_universe_falls_back_to_condition_id(tmp_path: Path):
    from arbx.data.recorder import load_universe_from_registry
    yaml_path = tmp_path / "pairs.yaml"
    yaml_path.write_text(
        "pairs:\n"
        "  - kalshi_market_id: KX-B\n"
        "    polymarket_market_id: 0xonly\n"
        "    polymarket_identifiers:\n"
        "      condition_id: 0xonly\n"
        "      yes_token_id: ''\n"
    )
    uni = load_universe_from_registry(yaml_path)
    assert ("polymarket", "0xonly") in uni


# ---------------------------------------------------------------------------
# load_universe_from_discovery (Phase 2 — coverage)
# ---------------------------------------------------------------------------

def _write_discovery(path: Path, venue: str, markets: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"venue": venue, "markets": markets}))


def test_discovery_universe_ranks_and_dedups(tmp_path: Path):
    k = tmp_path / "kalshi.json"
    p = tmp_path / "poly.json"
    _write_discovery(k, "kalshi", [
        {"venue": "kalshi", "market_id": "K-LOW", "activity_score": 1.0},
        {"venue": "kalshi", "market_id": "K-HIGH", "activity_score": 100.0},
        {"venue": "kalshi", "market_id": "K-HIGH", "activity_score": 100.0},  # dup
    ])
    _write_discovery(p, "polymarket", [
        {"venue": "polymarket", "market_id": "P-MID", "activity_score": 50.0},
    ])
    universe = load_universe_from_discovery([k, p])
    # ranked by activity desc, deduped
    assert universe == [("kalshi", "K-HIGH"), ("polymarket", "P-MID"), ("kalshi", "K-LOW")]


def test_discovery_universe_respects_max_markets(tmp_path: Path):
    k = tmp_path / "kalshi.json"
    _write_discovery(k, "kalshi", [
        {"venue": "kalshi", "market_id": f"K-{i}", "activity_score": float(i)}
        for i in range(10)
    ])
    universe = load_universe_from_discovery([k], max_markets=3)
    assert len(universe) == 3
    assert universe[0] == ("kalshi", "K-9")  # most active first


def test_discovery_universe_skips_missing_and_bad_files(tmp_path: Path):
    good = tmp_path / "good.json"
    _write_discovery(good, "kalshi", [{"venue": "kalshi", "market_id": "K-1", "activity_score": 5.0}])
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    missing = tmp_path / "missing.json"
    universe = load_universe_from_discovery([missing, bad, good])
    assert universe == [("kalshi", "K-1")]


def test_discovery_universe_min_activity_filter(tmp_path: Path):
    k = tmp_path / "kalshi.json"
    _write_discovery(k, "kalshi", [
        {"venue": "kalshi", "market_id": "K-DEAD", "activity_score": 0.0},
        {"venue": "kalshi", "market_id": "K-LIVE", "activity_score": 9.0},
    ])
    universe = load_universe_from_discovery([k], min_activity=1.0)
    assert universe == [("kalshi", "K-LIVE")]


# ---------------------------------------------------------------------------
# run_recorder (integration, offline)
# ---------------------------------------------------------------------------

def test_run_recorder_writes_rows(tmp_path: Path):
    kalshi_book = _make_book("kalshi", "KALSHI-MKT")
    poly_book = _make_book("polymarket", "0xcondition")

    connectors = {
        "kalshi": _mock_connector(kalshi_book),
        "polymarket": _mock_connector(poly_book),
    }
    universe = [("kalshi", "KALSHI-MKT"), ("polymarket", "0xcondition")]

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="test-run-001",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=2,
    )

    kalshi_files = list((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
    poly_files = list((tmp_path / "raw" / "book" / "venue=polymarket").glob("*.jsonl"))
    assert kalshi_files, "no kalshi JSONL produced"
    assert poly_files, "no polymarket JSONL produced"

    k_rows = [json.loads(line) for line in kalshi_files[0].read_text().splitlines() if line.strip()]
    p_rows = [json.loads(line) for line in poly_files[0].read_text().splitlines() if line.strip()]
    assert len(k_rows) == 2  # 2 cycles
    assert len(p_rows) == 2
    assert k_rows[0]["market_id"] == "KALSHI-MKT"
    assert p_rows[0]["market_id"] == "0xcondition"
    assert k_rows[0]["run_id"] == "test-run-001"


def test_run_recorder_capture_seq_monotonic(tmp_path: Path):
    book = _make_book()
    connectors = {"kalshi": _mock_connector(book)}
    universe = [("kalshi", "MKT")]

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="seq-test",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=3,
    )

    files = list((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
    rows = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    seqs = [r["capture_seq"] for r in rows]
    assert seqs == sorted(seqs), "capture_seq not monotonically increasing"
    assert len(set(seqs)) == len(seqs), "capture_seq has duplicates"


def test_run_recorder_fetch_error_does_not_crash(tmp_path: Path):
    connector = MagicMock()
    connector.fetch_orderbook.side_effect = RuntimeError("network failure")
    universe = [("kalshi", "FAIL-MKT")]

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="err-test",
        interval_seconds=0.01,
        connectors={"kalshi": connector},
        max_cycles=2,
    )
    # Should complete without raising


def test_run_recorder_on_cycle_callback(tmp_path: Path):
    book = _make_book()
    connectors = {"kalshi": _mock_connector(book)}
    universe = [("kalshi", "MKT")]
    seen_cycles = []

    def on_cycle(cycle: int, rows: list) -> None:
        seen_cycles.append((cycle, len(rows)))

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="cb-test",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=3,
        on_cycle=on_cycle,
    )
    assert len(seen_cycles) == 3
    assert all(n == 1 for _, n in seen_cycles)


def test_run_recorder_resume_seq(tmp_path: Path):
    # Pre-seed existing data with seq up to 50
    d = tmp_path / "raw" / "book" / "venue=kalshi"
    d.mkdir(parents=True)
    (d / "2026-06-27.jsonl").write_text(json.dumps({"capture_seq": 50}) + "\n")

    book = _make_book()
    connectors = {"kalshi": _mock_connector(book)}
    universe = [("kalshi", "MKT")]

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="resume-test",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=1,
    )

    files = sorted((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
    all_rows = []
    for f in files:
        all_rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    seqs = [r["capture_seq"] for r in all_rows]
    assert max(seqs) > 50, "resume should continue from seq > 50"


def test_recorder_heartbeat_file_created(tmp_path: Path):
    book = _make_book()
    connectors = {"kalshi": _mock_connector(book)}
    universe = [("kalshi", "MKT")]

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="hb-test",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=2,
    )
    latency_files = list((tmp_path / "raw" / "latency").glob("*.jsonl"))
    assert latency_files, "no heartbeat JSONL produced"
    hb_rows = [json.loads(line) for line in latency_files[0].read_text().splitlines() if line.strip()]
    kinds = {r["kind"] for r in hb_rows}
    assert "recorder_heartbeat" in kinds


# ---------------------------------------------------------------------------
# Continuity: lifecycle events, refresh, restart-gap (Phase 2)
# ---------------------------------------------------------------------------

def _latency_rows(tmp_path: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "raw" / "latency").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def test_recorder_emits_start_and_stop_events(tmp_path: Path):
    book = _make_book()
    run_recorder(
        [("kalshi", "MKT")],
        data_dir=tmp_path,
        run_id="lifecycle",
        interval_seconds=0.01,
        connectors={"kalshi": _mock_connector(book)},
        max_cycles=1,
    )
    kinds = [r["kind"] for r in _latency_rows(tmp_path)]
    assert "recorder_start" in kinds
    assert "recorder_stop" in kinds


def test_recorder_universe_refresh_adds_and_retires(tmp_path: Path):
    connectors = {
        "kalshi": _mock_connector(_make_book("kalshi", "K")),
        "polymarket": _mock_connector(_make_book("polymarket", "P")),
    }
    # provider flips the universe from [K] to [P] after first call
    calls = {"n": 0}

    def provider() -> list[tuple[str, str]]:
        calls["n"] += 1
        return [("polymarket", "P")] if calls["n"] > 1 else [("kalshi", "K")]

    run_recorder(
        [("kalshi", "K")],
        data_dir=tmp_path,
        run_id="refresh",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=4,
        universe_provider=provider,
        universe_refresh_seconds=0.0,  # refresh every cycle
    )
    changes = [r for r in _latency_rows(tmp_path) if r["kind"] == "universe_change"]
    assert changes, "expected a universe_change event"
    change = changes[0]
    assert ["polymarket", "P"] in change["added"]
    assert ["kalshi", "K"] in change["retired"]


def test_recorder_refresh_failure_keeps_universe(tmp_path: Path):
    connectors = {"kalshi": _mock_connector(_make_book("kalshi", "K"))}

    def bad_provider() -> list[tuple[str, str]]:
        raise RuntimeError("discovery down")

    run_recorder(
        [("kalshi", "K")],
        data_dir=tmp_path,
        run_id="badrefresh",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=3,
        universe_provider=bad_provider,
        universe_refresh_seconds=0.0,
    )
    # Still recorded K despite provider raising every cycle
    k_files = list((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
    rows = [json.loads(line) for line in k_files[0].read_text().splitlines() if line.strip()]
    assert len(rows) == 3


def test_recorder_empty_refresh_keeps_universe(tmp_path: Path):
    connectors = {"kalshi": _mock_connector(_make_book("kalshi", "K"))}
    run_recorder(
        [("kalshi", "K")],
        data_dir=tmp_path,
        run_id="emptyrefresh",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=3,
        universe_provider=lambda: [],
        universe_refresh_seconds=0.0,
    )
    k_files = list((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
    rows = [json.loads(line) for line in k_files[0].read_text().splitlines() if line.strip()]
    assert len(rows) == 3


def test_last_heartbeat_at_reads_latest(tmp_path: Path):
    d = tmp_path / "raw" / "latency"
    d.mkdir(parents=True)
    (d / "2026-06-27.jsonl").write_text(
        json.dumps({"kind": "recorder_heartbeat", "observed_at": "2026-06-27T10:00:00+00:00"}) + "\n" +
        json.dumps({"kind": "recorder_heartbeat", "observed_at": "2026-06-27T11:00:00+00:00"}) + "\n" +
        json.dumps({"kind": "recorder_gap", "observed_at": "2026-06-27T23:00:00+00:00"}) + "\n"
    )
    latest = _last_heartbeat_at(tmp_path)
    assert latest == datetime(2026, 6, 27, 11, 0, 0, tzinfo=timezone.utc)


def test_last_heartbeat_at_empty(tmp_path: Path):
    assert _last_heartbeat_at(tmp_path) is None


def test_recorder_annotates_restart_gap(tmp_path: Path):
    # Seed a stale heartbeat far in the past so startup flags a restart gap.
    d = tmp_path / "raw" / "latency"
    d.mkdir(parents=True)
    (d / "old.jsonl").write_text(
        json.dumps({"kind": "recorder_heartbeat", "observed_at": "2020-01-01T00:00:00+00:00"}) + "\n"
    )
    run_recorder(
        [("kalshi", "K")],
        data_dir=tmp_path,
        run_id="restart",
        interval_seconds=0.01,
        connectors={"kalshi": _mock_connector(_make_book("kalshi", "K"))},
        max_cycles=1,
        restart_gap_threshold_seconds=1.0,
    )
    gaps = [r for r in _latency_rows(tmp_path) if r["kind"] == "recorder_gap" and r.get("reason") == "restart"]
    assert gaps, "expected a restart gap annotation"
    assert gaps[0]["gap_ms"] > 0


def test_recorder_no_restart_gap_on_fresh_start(tmp_path: Path):
    run_recorder(
        [("kalshi", "K")],
        data_dir=tmp_path,
        run_id="fresh",
        interval_seconds=0.01,
        connectors={"kalshi": _mock_connector(_make_book("kalshi", "K"))},
        max_cycles=1,
    )
    restart_gaps = [r for r in _latency_rows(tmp_path)
                    if r["kind"] == "recorder_gap" and r.get("reason") == "restart"]
    assert not restart_gaps


def test_recorder_external_stop_event(tmp_path: Path):
    import threading
    stop = threading.Event()
    stop.set()  # already stopped → loop body never runs
    run_recorder(
        [("kalshi", "K")],
        data_dir=tmp_path,
        run_id="extstop",
        interval_seconds=0.01,
        connectors={"kalshi": _mock_connector(_make_book("kalshi", "K"))},
        stop_event=stop,
    )
    # No book rows because the loop exited immediately
    assert not list((tmp_path / "raw" / "book" / "venue=kalshi").glob("*.jsonl"))
