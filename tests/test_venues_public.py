# Scope: TEST — Public venue adapters normalize raw payloads without mocks/fixtures.
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import arbx.venues.kalshi_public as kalshi_mod
import arbx.venues.polymarket_public as poly_mod
from arbx.core.models import OrderBook, VenueHealth
from arbx.venues.http import HttpResult
from arbx.venues.kalshi_public import KalshiAdapter
from arbx.venues.polymarket_public import PolymarketAdapter


class _StubProvider:
    """Minimal provider standing in for the real API provider in tests."""

    def __init__(self, payload: dict, *, venue: str) -> None:
        self._payload = payload
        self._venue = venue

    def fetch_orderbook_json(self, market_id: str) -> HttpResult:
        return HttpResult(
            payload=self._payload,
            status_code=200,
            attempts=1,
            health=VenueHealth(
                venue=self._venue,
                is_healthy=True,
                last_checked=datetime.now(timezone.utc),
                reason="ok",
            ),
        )


# Recorded-shape Kalshi orderbook payload (public /orderbook response), exercising
# _to_fixture_shape + the inlined normalize_kalshi_book path.
KALSHI_PAYLOAD = {
    "market_id": "KXTEST",
    "timestamp": "2026-06-28T12:00:00+00:00",
    "orderbook": {
        "yes": [{"price": 0.61, "size": 20}, {"price": 0.59, "size": 10}],
        "no": [{"price": 0.35, "size": 18}, {"price": 0.30, "size": 5}],
    },
}

# Recorded-shape Polymarket CLOB /book payload with epoch-millis timestamp,
# exercising _to_fixture_shape (complement + sort) + inlined normalize_poly_book.
POLY_PAYLOAD = {
    "market": "0xcondition",
    "asset_id": "yes-token",
    "timestamp": "1782828000000",
    "bids": [
        {"price": "0.01", "size": "10"},
        {"price": "0.37", "size": "5"},
    ],
    "asks": [
        {"price": "0.99", "size": "10"},
        {"price": "0.39", "size": "5"},
    ],
}


def test_kalshi_payload_normalizes_to_orderbook():
    adapter = KalshiAdapter(provider=_StubProvider(KALSHI_PAYLOAD, venue="kalshi"))

    book = adapter.fetch_orderbook("KXTEST")

    assert isinstance(book, OrderBook)
    assert book.venue == "kalshi"
    assert book.market_id == "KXTEST"
    # best of book + level ordering preserved as supplied
    assert book.yes_levels[0].price == 0.61
    assert book.no_levels[0].price == 0.35
    assert [level.price for level in book.yes_levels] == [0.61, 0.59]
    assert [level.price for level in book.no_levels] == [0.35, 0.30]
    # timestamp parsing (ISO-8601 -> tz-aware UTC)
    assert book.timestamp == datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    assert adapter.health().is_healthy is True


def test_polymarket_payload_normalizes_to_orderbook():
    adapter = PolymarketAdapter(provider=_StubProvider(POLY_PAYLOAD, venue="polymarket"))

    book = adapter.fetch_orderbook("yes-token")

    assert isinstance(book, OrderBook)
    assert book.venue == "polymarket"
    # YES bids from payload bids (best/highest first); NO bids from asks
    # complemented (a YES ask at p is a NO bid at 1 - p), best first.
    assert book.yes_levels[0].price == 0.37
    assert [level.price for level in book.yes_levels] == [0.37, 0.01]
    assert book.no_levels[0].price == 0.61
    assert [round(level.price, 10) for level in book.no_levels] == [0.61, 0.01]
    # epoch-millis timestamp parsed to tz-aware UTC
    assert book.timestamp.year == 2026
    assert book.timestamp.tzinfo is not None
    assert adapter.health().is_healthy is True


def _import_lines(module) -> list[str]:
    lines = Path(module.__file__).read_text().splitlines()
    return [line for line in lines if line.startswith(("import ", "from "))]


def test_no_mock_imports():
    for module in (kalshi_mod, poly_mod):
        for line in _import_lines(module):
            assert "mock" not in line, f"{module.__name__} imports a mock: {line!r}"
