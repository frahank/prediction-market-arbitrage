# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Polymarket public-data adapter + provider; no account access.
"""Polymarket public order-book adapter.

Merges the former ``adapters/polymarket.py`` (normalization + ``PolymarketAdapter``)
and ``adapters/polymarket_provider.py`` (the real public-data provider) into one
module, with ``normalize_poly_book`` inlined so there is no dependency on the
paper simulation layer. The provider hits only the public read-only CLOB book
endpoint; no order-mutation endpoint is ever constructed here. ``market_id`` is
the CLOB token id.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from arbx.core.models import (
    ConnectorSource,
    ModelValidationError,
    OrderBook,
    OrderBookLevel,
    VenueHealth,
)
from arbx.venues.base import AdapterContract
from arbx.venues.http import HttpResult, MockableHttpClient

PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "arbx-public-data/0.1",
}


class _NormalizationError(ValueError):
    """Raised when a raw Polymarket payload cannot be normalized to an OrderBook."""


class PolymarketApiProvider:
    """Real Polymarket public-data provider; hits only read-only public endpoints."""

    def __init__(
        self,
        *,
        http_client: MockableHttpClient | None = None,
        base_url: str = "https://clob.polymarket.com",
        fixture_path: str | None = None,
    ) -> None:
        self.http_client = http_client or MockableHttpClient()
        self.base_url = base_url.rstrip("/")
        self.fixture_path = fixture_path

    def fetch_orderbook_json(self, market_id: str) -> HttpResult:
        url = f"{self.base_url}/book?token_id={quote(market_id, safe='')}"
        return self.http_client.get_json(url, venue="polymarket", headers=PUBLIC_HEADERS)

    def fetch_market_json(self, condition_id: str) -> HttpResult:
        url = f"{self.base_url}/markets/{quote(condition_id, safe='')}"
        return self.http_client.get_json(
            url,
            venue="polymarket",
            headers=PUBLIC_HEADERS,
        )

    def fetch_market_info_json(self, condition_id: str) -> HttpResult:
        url = f"{self.base_url}/clob-markets/{quote(condition_id, safe='')}"
        return self.http_client.get_json(
            url,
            venue="polymarket",
            headers=PUBLIC_HEADERS,
        )

    def fetch_fee_rate_json(self, token_id: str) -> HttpResult:
        url = f"{self.base_url}/fee-rate?token_id={quote(token_id, safe='')}"
        return self.http_client.get_json(
            url,
            venue="polymarket",
            headers=PUBLIC_HEADERS,
        )

    def is_configured(self) -> bool:
        return True


class PolymarketAdapter(AdapterContract):
    def __init__(
        self,
        provider: PolymarketApiProvider | None = None,
        *,
        connector_source: ConnectorSource = ConnectorSource.MOCK,
    ) -> None:
        self.provider = provider or PolymarketApiProvider()
        self.connector_source = connector_source
        self._last_book: OrderBook | None = None
        self._health = _venue_health("polymarket", True, "not_checked")

    def fetch_orderbook(self, market_id: str) -> OrderBook:
        result = self.provider.fetch_orderbook_json(market_id)
        self._health = result.health
        if not result.health.is_healthy or result.payload is None:
            return self._fallback_book(market_id)

        try:
            normalized = _to_fixture_shape(result.payload, market_id)
            if normalized.pop("_empty_book", False):
                book = OrderBook(
                    venue=str(normalized["venue"]),
                    market_id=str(normalized["market_id"]),
                    yes_levels=(),
                    no_levels=(),
                    timestamp=datetime.fromisoformat(
                        str(normalized["timestamp"]).replace("Z", "+00:00")
                    ),
                )
            else:
                book = normalize_poly_book(normalized)
        except ValueError:
            self._health = _venue_health("polymarket", False, "degraded: invalid_payload")
            return self._fallback_book(market_id)

        book = replace(
            book,
            fetched_at=datetime.now(timezone.utc),
            connector_source=self.connector_source,
        )
        self._last_book = book
        self._health = _venue_health("polymarket", True, f"ok: status={result.status_code}; attempts={result.attempts}")
        return book

    def health(self) -> VenueHealth:
        return replace(self._health, connector_source=self.connector_source)

    def _fallback_book(self, market_id: str) -> OrderBook:
        if self._last_book is not None:
            return self._last_book
        return OrderBook(
            venue="polymarket",
            market_id=market_id,
            yes_levels=(),
            no_levels=(),
            timestamp=datetime.now(timezone.utc),
            connector_source=self.connector_source,
            reportable=False,
        )


def normalize_poly_book(raw: dict[str, Any]) -> OrderBook:
    """Normalize a fixture-shaped Polymarket payload into an OrderBook."""
    bids = raw.get("bids", {})
    if not isinstance(bids, dict):
        raise _NormalizationError("polymarket bids must be an object")

    return _build_normalized_book(
        venue=str(raw.get("venue", "polymarket")),
        market_id=str(raw.get("market_id", "")),
        yes_levels=_levels_from_raw(bids.get("YES", []), "yes_levels"),
        no_levels=_levels_from_raw(bids.get("NO", []), "no_levels"),
        timestamp=_parse_iso_timestamp(str(raw.get("timestamp", ""))),
    )


def _to_fixture_shape(payload: dict[str, Any], market_id: str) -> dict[str, Any]:
    bids = payload.get("bids", {})
    if isinstance(bids, dict) and "YES" in bids and "NO" in bids:
        return {
            "venue": "polymarket",
            "market_id": str(payload.get("market_id", market_id)),
            "timestamp": _timestamp_to_iso(payload.get("timestamp", _now_iso())),
            "bids": bids,
        }

    asks = payload.get("asks")
    raw_bids = payload.get("bids")
    if not isinstance(asks, list) or not isinstance(raw_bids, list):
        raise ValueError("unrecognized Polymarket orderbook payload")
    if not asks and not raw_bids:
        return {
            "venue": "polymarket",
            "market_id": str(payload.get("market_id", market_id)),
            "timestamp": _timestamp_to_iso(payload.get("timestamp", _now_iso())),
            "bids": {
                "YES": [],
                "NO": [],
            },
            "_empty_book": True,
        }
    # Semantics fix (see docs/book_semantics_fix.md): ``bids`` are resting
    # YES bids and become ``yes_levels``; ``asks`` are resting YES asks and
    # become ``no_levels`` as their NO-bid complement (a YES ask at p is a NO
    # bid at 1-p) — matching the recorder's row mapping (best_bid =
    # yes_levels[0].price, best_ask = 1 - no_levels[0].price). The previous
    # port fed asks into ``yes_levels``, which swapped bid/ask in every row.
    yes_levels = _levels_from_poly(raw_bids, complement=False)
    no_levels = _levels_from_poly(asks, complement=True)
    return {
        "venue": "polymarket",
        "market_id": str(payload.get("market_id", market_id)),
        "timestamp": _timestamp_to_iso(payload.get("timestamp", _now_iso())),
        "bids": {
            "YES": yes_levels,
            "NO": no_levels,
        },
    }


def _levels_from_poly(raw_levels: Any, *, complement: bool) -> list[dict[str, float]]:
    levels = []
    if not isinstance(raw_levels, list):
        return levels
    for raw_level in raw_levels:
        price = float(raw_level["price"])
        size = float(raw_level["size"])
        if complement:
            price = 1 - price
        if 0 < price < 1 and size > 0:
            levels.append({"price": price, "size": size})
    # Bid ladders are best (highest) first.
    return sorted(levels, key=lambda level: level["price"], reverse=True)


def _build_normalized_book(
    *,
    venue: str,
    market_id: str,
    yes_levels: tuple[OrderBookLevel, ...],
    no_levels: tuple[OrderBookLevel, ...],
    timestamp: datetime,
) -> OrderBook:
    if not yes_levels:
        raise _NormalizationError("yes_levels cannot be empty")
    if not no_levels:
        raise _NormalizationError("no_levels cannot be empty")

    try:
        return OrderBook(
            venue=venue,
            market_id=market_id,
            yes_levels=yes_levels,
            no_levels=no_levels,
            timestamp=timestamp,
        )
    except ModelValidationError as exc:
        raise _NormalizationError(str(exc)) from exc


def _levels_from_raw(raw_levels: Any, field_name: str) -> tuple[OrderBookLevel, ...]:
    if not isinstance(raw_levels, list):
        raise _NormalizationError(f"{field_name} must be a list")

    try:
        return tuple(
            OrderBookLevel(
                price=float(level["price"]),
                size=float(level["size"]),
            )
            for level in raw_levels
        )
    except (KeyError, TypeError, ValueError, ModelValidationError) as exc:
        raise _NormalizationError(f"{field_name} contains an invalid level") from exc


def _parse_iso_timestamp(value: str) -> datetime:
    if not value:
        raise _NormalizationError("timestamp is required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _NormalizationError("timestamp must be ISO-8601") from exc


def _timestamp_to_iso(raw_timestamp: Any) -> str:
    if isinstance(raw_timestamp, int | float):
        return _epoch_to_iso(float(raw_timestamp))

    timestamp = str(raw_timestamp)
    if timestamp.isdigit():
        return _epoch_to_iso(float(timestamp))
    return timestamp


def _epoch_to_iso(raw_timestamp: float) -> str:
    seconds = raw_timestamp / 1000 if raw_timestamp > 10_000_000_000 else raw_timestamp
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _venue_health(venue: str, is_healthy: bool, reason: str) -> VenueHealth:
    return VenueHealth(
        venue=venue,
        is_healthy=is_healthy,
        last_checked=datetime.now(timezone.utc),
        reason=reason,
    )
