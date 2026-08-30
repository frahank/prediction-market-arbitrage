# Derived edge rows carry every edge_observations non-probe column.
from __future__ import annotations

from datetime import datetime, timezone

from arbx.analysis.edges import EdgePair, _edge_rows_for_capture
from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel
from arbx.data.recorder import book_to_observation

# §2.2 edge_observations columns. `public_probe` and `probe_source` are stamped
# only by the public refetch probe runner, so they are excluded from the set a
# normal (non-probe) derived row must carry.
_CONTRACT_NON_PROBE_COLUMNS = [
    "pair_key",
    "capture_ts_utc",
    "recv_monotonic_ns",
    "kalshi_capture_seq",
    "polymarket_capture_seq",
    "source_capture_seq",
    "direction",
    "raw_edge",
    "fee_adj_edge",
    "depth_adj_edge",
    "target_size",
    "depth_fillable_size",
    "depth_liquidity_complete",
    "vwap",
    "kalshi_vwap",
    "polymarket_vwap",
    "slippage",
    "max_profitable_size",
    "capture_skew_ms",
    "kalshi_freshness_status",
    "polymarket_freshness_status",
    "max_staleness_seconds",
    "books_fresh",
    "benchmark_ms",
    "survived",
    "survived_edge",
    "survived_through_ms",
    "survival_tier",
    "include_in_strategy_metrics",
    "run_id",
]


def _book(venue: str, market_id: str, yes_bid: float, no_bid: float) -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id=market_id,
        yes_levels=(OrderBookLevel(yes_bid, 100.0),),
        no_levels=(OrderBookLevel(no_bid, 100.0),),
        timestamp=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        connector_source=ConnectorSource.LIVE_PUBLIC,
        reportable=True,
    )


def _row(book: OrderBook, capture_seq: int, recv_ns: int) -> dict:
    return book_to_observation(
        book,
        capture_seq=capture_seq,
        recv_monotonic_ns=recv_ns,
        capture_ts_utc=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        fetch_elapsed_ms=10.0,
        run_id="edges-schema",
    )


def test_edge_row_columns_match_contract():
    pair = EdgePair(
        pair_key="KX-A/POLY-A",
        kalshi_market_id="KX-A",
        polymarket_market_id="POLY-A",
        include_in_strategy_metrics=True,
    )
    k_row = _row(_book("kalshi", "KX-A", yes_bid=0.60, no_bid=0.35), 1, 1_000)
    p_row = _row(_book("polymarket", "POLY-A", yes_bid=0.62, no_bid=0.33), 2, 2_000)

    rows = _edge_rows_for_capture(pair, k_row, p_row, fee_round_trip=0.02, target_size=1.0)

    assert rows, "expected at least one derived (non-probe) edge row"
    row = rows[0]
    for column in _CONTRACT_NON_PROBE_COLUMNS:
        assert column in row, f"missing §2.2 non-probe column: {column}"
