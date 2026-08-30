"""NTP offset stamping in the recorder (P3-T2): rows carry it, big offsets warn."""

import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import arbx.data.recorder as recorder_mod
from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel
from arbx.data.recorder import run_recorder


def _book() -> OrderBook:
    return OrderBook(
        venue="kalshi",
        market_id="TEST-MKT",
        yes_levels=(OrderBookLevel(0.60, 100.0),),
        no_levels=(OrderBookLevel(0.35, 150.0),),
        timestamp=datetime.now(timezone.utc),
        connector_source=ConnectorSource.LIVE_PUBLIC,
    )


def _connectors():
    conn = MagicMock()
    conn.fetch_orderbook.return_value = _book()
    return {"kalshi": conn}


def _run(tmp_path):
    run_recorder(
        [("kalshi", "TEST-MKT")],
        data_dir=tmp_path,
        run_id="ntp-test",
        interval_seconds=0.01,
        connectors=_connectors(),
        max_cycles=1,
    )


def _book_rows(tmp_path):
    rows = []
    for f in (tmp_path / "raw" / "book").rglob("*.jsonl"):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def test_rows_carry_ntp_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_mod, "measure_ntp_offset_ms", lambda: 42.5)
    _run(tmp_path)
    rows = _book_rows(tmp_path)
    assert rows and all(r["ntp_offset_ms"] == 42.5 for r in rows)
    # The continuity log's recorder_start event carries it too.
    starts = [
        json.loads(line)
        for f in (tmp_path / "raw" / "latency").glob("*.jsonl")
        for line in f.read_text().splitlines()
        if json.loads(line).get("kind") == "recorder_start"
    ]
    assert starts and starts[0]["ntp_offset_ms"] == 42.5


def test_warning_on_large_offset(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(recorder_mod, "measure_ntp_offset_ms", lambda: 400.0)
    with caplog.at_level(logging.WARNING, logger="arbx.data.recorder"):
        _run(tmp_path)
    assert any(
        "250" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )
    rows = _book_rows(tmp_path)
    assert rows and all(r["ntp_offset_ms"] == 400.0 for r in rows)


def test_pinned_offset_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_mod, "measure_ntp_offset_ms", lambda: 42.5)
    run_recorder(
        [("kalshi", "TEST-MKT")],
        data_dir=tmp_path,
        run_id="ntp-pinned",
        interval_seconds=0.01,
        connectors=_connectors(),
        max_cycles=1,
        ntp_offset_ms=7.0,
    )
    rows = _book_rows(tmp_path)
    assert rows and all(r["ntp_offset_ms"] == 7.0 for r in rows)
