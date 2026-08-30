# Scope: TEST — book_to_observation emits every dataset_schema.md §2.1 column.
from __future__ import annotations

from datetime import datetime, timezone

from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel
from arbx.data.recorder import book_to_observation


def _synthetic_book() -> OrderBook:
    return OrderBook(
        venue="kalshi",
        market_id="KX-CONTRACT",
        yes_levels=(OrderBookLevel(0.60, 100.0), OrderBookLevel(0.55, 200.0)),
        no_levels=(OrderBookLevel(0.35, 150.0), OrderBookLevel(0.30, 250.0)),
        timestamp=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 6, 28, 12, 0, 0, 50000, tzinfo=timezone.utc),
        connector_source=ConnectorSource.LIVE_PUBLIC,
        reportable=True,
    )


# Column contract from docs/dataset_schema.md §2.1 (verbatim per the Phase 1
# module-contract block), with the top-5 ladders expanded.
_CONTRACT_COLUMNS = [
    "venue",
    "market_id",
    "capture_seq",
    "capture_ts_utc",
    "recv_monotonic_ns",
    "venue_book_ts",
    "best_bid",
    "best_ask",
    "mid",
    "spread",
    "staleness_seconds",
    "freshness_status",
    "freshness_threshold_seconds",
    *[f"bid_px_{i}" for i in range(1, 6)],
    *[f"bid_sz_{i}" for i in range(1, 6)],
    *[f"ask_px_{i}" for i in range(1, 6)],
    *[f"ask_sz_{i}" for i in range(1, 6)],
    "book_json",
    "connector_source",
    "reportable",
    "fetch_elapsed_ms",
    "ntp_offset_ms",
    "run_id",
]


def test_observation_row_has_contract_columns():
    row = book_to_observation(
        _synthetic_book(),
        capture_seq=1,
        recv_monotonic_ns=12345678,
        capture_ts_utc=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetch_elapsed_ms=42.5,
        run_id="schema-run",
        ntp_offset_ms=1.5,
    )
    for column in _CONTRACT_COLUMNS:
        assert column in row, f"missing §2.1 column: {column}"
