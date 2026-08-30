"""Bid/ask orientation of normalized books, pinned against live venue quotes.

Regression guard for the inversion fixed in docs/book_semantics_fix.md: the
ported normalizers fed ask-side ladders into ``yes_levels``, so every recorded
row had ``best_bid``/``best_ask`` (and the px/sz ladders) swapped and every
derived edge was inflated by both venues' spreads.

The payloads below are trimmed from live responses captured 2026-07-02 for
KXALIENS-27 and its paired Polymarket token, alongside the venues' own quotes
at the same moment:

- Kalshi ``GET /markets/KXALIENS-27``: ``yes_bid_dollars=0.0800``,
  ``yes_ask_dollars=0.0810``.
- Polymarket ``GET /midpoint?token_id=...``: ``mid=0.065`` (book 0.06/0.07).

The normalized row must reproduce the venue's own bid/ask, uncrossed.
"""

import time
from datetime import datetime, timezone

from arbx.data.recorder import book_to_observation
from arbx.venues.kalshi_public import _book_from_payload
from arbx.venues.polymarket_public import _to_fixture_shape, normalize_poly_book

KALSHI_FP_PAYLOAD = {
    "market_id": "KXALIENS-27",
    "orderbook_fp": {
        # Resting YES bids (dollars), ascending as the live API returns them.
        "yes_dollars": [["0.0010", "12240.51"], ["0.0730", "144.24"], ["0.0800", "8940.76"]],
        # Resting NO bids.
        "no_dollars": [["0.0010", "1002000.00"], ["0.9160", "492.32"], ["0.9190", "14608.67"]],
    },
}

POLY_BOOK_PAYLOAD = {
    "market": "0xcondition",
    "asset_id": "token",
    "timestamp": "1782828000000",
    # Resting YES bids / YES asks, best last as the live CLOB returns them.
    "bids": [{"price": "0.01", "size": "100"}, {"price": "0.06", "size": "50"}],
    "asks": [{"price": "0.99", "size": "100"}, {"price": "0.07", "size": "40"}],
}


def _row(book):
    return book_to_observation(
        book,
        capture_seq=1,
        recv_monotonic_ns=time.monotonic_ns(),
        capture_ts_utc=datetime.now(timezone.utc),
        fetch_elapsed_ms=1.0,
        run_id="semantics-test",
    )


def test_kalshi_row_matches_venue_quote():
    row = _row(_book_from_payload(KALSHI_FP_PAYLOAD, "KXALIENS-27"))
    # Venue's own quote at capture: yes_bid 0.080, yes_ask 0.081.
    assert row["best_bid"] == 0.08
    assert round(row["best_ask"], 10) == 0.081
    assert row["spread"] > 0, "book must not be crossed"
    # Bid ladder best-first (descending), ask ladder cheapest-first (ascending).
    assert row["bid_px_1"] == 0.08 and row["bid_px_2"] == 0.073
    assert round(row["ask_px_1"], 10) == 0.081 and round(row["ask_px_2"], 10) == 0.084
    # Ask sizes come from the NO-bid ladder that prices them.
    assert row["ask_sz_1"] == 14608.67


def test_polymarket_row_matches_venue_quote():
    shaped = _to_fixture_shape(POLY_BOOK_PAYLOAD, "token")
    shaped.pop("_empty_book", None)
    row = _row(normalize_poly_book(shaped))
    # Venue midpoint at capture was 0.065 on a 0.06/0.07 book.
    assert row["best_bid"] == 0.06
    assert round(row["best_ask"], 10) == 0.07
    assert row["spread"] > 0, "book must not be crossed"
    assert round((row["best_bid"] + row["best_ask"]) / 2, 10) == 0.065
    assert row["bid_px_1"] == 0.06 and row["bid_px_2"] == 0.01
    assert round(row["ask_px_1"], 10) == 0.07 and round(row["ask_px_2"], 10) == 0.99
    assert row["ask_sz_1"] == 40.0
