# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Kalshi public-data adapter + provider; no account access.
"""Kalshi public order-book adapter.

Merges the former ``adapters/kalshi.py`` (normalization + ``KalshiAdapter``) and
``adapters/kalshi_provider.py`` (the real public-data provider) into one module,
with ``normalize_kalshi_book`` inlined so there is no dependency on the paper
simulation layer. The provider hits only the public read-only order-book
endpoint; no order-mutation endpoint is ever constructed here.
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
from arbx.venues.http import HttpResult, MockableHttpClient, RetryConfig

PLACEHOLDER_KALSHI_ACCESS_KEY = "REPLACE_WITH_KALSHI_ACCESS_KEY"
PLACEHOLDER_KALSHI_PRIVATE_KEY = "REPLACE_WITH_KALSHI_PRIVATE_KEY"
PLACEHOLDER_KALSHI_ACCESS_SIGNATURE = "REPLACE_WITH_KALSHI_ACCESS_SIGNATURE"
PLACEHOLDER_KALSHI_ACCESS_TIMESTAMP = "REPLACE_WITH_KALSHI_ACCESS_TIMESTAMP"

PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "arbx-kalshi-public-data/0.1",
}
KALSHI_PUBLIC_RETRY_CONFIG = RetryConfig(max_retries=3, backoff_base_ms=250, jitter=True)


class _NormalizationError(ValueError):
    """Raised when a raw Kalshi payload cannot be normalized to an OrderBook."""


class KalshiApiProvider:
    """Real Kalshi public-data provider; hits only read-only public endpoints."""

    def __init__(
        self,
        *,
        http_client: MockableHttpClient | None = None,
        base_url: str = "https://external-api.kalshi.com/trade-api/v2",
        fixture_path: str | None = None,
    ) -> None:
        self.http_client = http_client or MockableHttpClient(retry_config=KALSHI_PUBLIC_RETRY_CONFIG)
        self.base_url = base_url.rstrip("/")
        self.fixture_path = fixture_path

    def fetch_orderbook_json(self, market_id: str) -> HttpResult:
        url = f"{self.base_url}/markets/{quote(market_id, safe='')}/orderbook"
        return self.http_client.get_json(
            url,
            venue="kalshi",
            headers=PUBLIC_HEADERS,
        )

    def fetch_market_json(self, market_id: str) -> HttpResult:
        url = f"{self.base_url}/markets/{quote(market_id, safe='')}"
        return self.http_client.get_json(
            url,
            venue="kalshi",
            headers=PUBLIC_HEADERS,
        )

    def is_configured(self) -> bool:
        return True

    def _placeholder_headers(self) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": PLACEHOLDER_KALSHI_ACCESS_KEY,
            "KALSHI-ACCESS-SIGNATURE": PLACEHOLDER_KALSHI_ACCESS_SIGNATURE,
            "KALSHI-ACCESS-TIMESTAMP": PLACEHOLDER_KALSHI_ACCESS_TIMESTAMP,
        }


class KalshiAdapter(AdapterContract):
    def __init__(
        self,
        provider: KalshiApiProvider | None = None,
        *,
        connector_source: ConnectorSource = ConnectorSource.MOCK,
    ) -> None:
        self.provider = provider or KalshiApiProvider()
        self.connector_source = connector_source
        self._last_book: OrderBook | None = None
        self._health = _venue_health("kalshi", True, "not_checked")

    def fetch_orderbook(self, market_id: str) -> OrderBook:
        result = self.provider.fetch_orderbook_json(market_id)
        self._health = result.health
        if not result.health.is_healthy or result.payload is None:
            return self._fallback_book(market_id)

        try:
            book = _book_from_payload(result.payload, market_id)
        except ValueError:
            self._health = _venue_health("kalshi", False, "degraded: invalid_payload")
            return self._fallback_book(market_id)

        book = replace(
            book,
            fetched_at=datetime.now(timezone.utc),
            connector_source=self.connector_source,
        )
        self._last_book = book
        self._health = _venue_health("kalshi", True, f"ok: status={result.status_code}; attempts={result.attempts}")
        return book

    def health(self) -> VenueHealth:
        return replace(self._health, connector_source=self.connector_source)

    def _fallback_book(self, market_id: str) -> OrderBook:
        if self._last_book is not None:
            return self._last_book
        return OrderBook(
            venue="kalshi",
            market_id=market_id,
            yes_levels=(),
            no_levels=(),
            timestamp=datetime.now(timezone.utc),
            connector_source=self.connector_source,
            reportable=False,
        )


def normalize_kalshi_book(raw: dict[str, Any]) -> OrderBook:
    """Normalize a fixture-shaped Kalshi payload into an OrderBook."""
    return _build_normalized_book(
        venue=str(raw.get("venue", "kalshi")),
        market_id=str(raw.get("market_id", "")),
        yes_levels=_levels_from_raw(raw.get("yes", []), "yes_levels"),
        no_levels=_levels_from_raw(raw.get("no", []), "no_levels"),
        timestamp=_parse_iso_timestamp(str(raw.get("timestamp", ""))),
    )


def _book_from_payload(payload: dict[str, Any], market_id: str) -> OrderBook:
    if "orderbook_fp" in payload:
        return _book_from_orderbook_fp(payload, market_id)
    if not (
        ("yes" in payload and "no" in payload)
        or isinstance(payload.get("orderbook"), dict)
    ):
        raise ValueError("unrecognized Kalshi orderbook payload")
    return normalize_kalshi_book(_to_fixture_shape(payload, market_id))


def _book_from_orderbook_fp(payload: dict[str, Any], market_id: str) -> OrderBook:
    # Semantics fix (see docs/book_semantics_fix.md): ``yes_dollars`` /
    # ``no_dollars`` are resting BIDS on each side. ``yes_levels`` /
    # ``no_levels`` carry those bid ladders best-first — exactly what the
    # recorder's row mapping assumes (best_bid = yes_levels[0].price,
    # best_ask = 1 - no_levels[0].price). The previous port complemented the
    # opposite side into ``yes_levels``, which swapped bid/ask in every row.
    orderbook = payload.get("orderbook_fp", {})
    if not isinstance(orderbook, dict):
        raise ValueError("orderbook_fp must be an object")

    yes_levels = bid_levels_from_raw(orderbook.get("yes_dollars", []))
    no_levels = bid_levels_from_raw(orderbook.get("no_dollars", []))
    return OrderBook(
        venue="kalshi",
        market_id=str(payload.get("market_id", payload.get("ticker", market_id))),
        yes_levels=yes_levels,
        no_levels=no_levels,
        timestamp=_timestamp_from_payload(payload),
    )


def _to_fixture_shape(payload: dict[str, Any], market_id: str) -> dict[str, Any]:
    if "yes" in payload and "no" in payload:
        return {
            "venue": "kalshi",
            "market_id": str(payload.get("market_id", market_id)),
            "timestamp": str(payload.get("timestamp", _now_iso())),
            "yes": payload["yes"],
            "no": payload["no"],
        }

    orderbook = payload.get("orderbook", {})
    if not isinstance(orderbook, dict):
        orderbook = {}

    return {
        "venue": "kalshi",
        "market_id": str(payload.get("market_id", market_id)),
        "timestamp": str(payload.get("timestamp", _now_iso())),
        "yes": _levels_from_kalshi(orderbook.get("yes", [])),
        "no": _levels_from_kalshi(orderbook.get("no", [])),
    }


def _levels_from_kalshi(raw_levels: Any) -> list[dict[str, float]]:
    levels = []
    if not isinstance(raw_levels, list):
        return levels
    for raw_level in raw_levels:
        if isinstance(raw_level, dict):
            price = float(raw_level["price"])
            size = float(raw_level["size"])
        else:
            price = float(raw_level[0])
            size = float(raw_level[1])
        if price > 1:
            price = price / 100
        levels.append({"price": price, "size": size})
    # Bid ladders are best (highest) first; the live API returns ascending.
    return sorted(levels, key=lambda level: level["price"], reverse=True)


def bid_levels_from_raw(raw_levels: Any) -> tuple[OrderBookLevel, ...]:
    """Parse a raw bid ladder (``[[price, size], ...]`` or dicts) best-first.

    Prices are dollars in (0, 1); zero-size and out-of-range levels are
    dropped. Shared by the REST ``orderbook_fp`` path and the WebSocket
    ``orderbook_snapshot`` path (``yes_dollars_fp`` / ``no_dollars_fp``).
    """
    levels = []
    if not isinstance(raw_levels, list):
        return ()
    for raw_level in raw_levels:
        if isinstance(raw_level, dict):
            price = float(raw_level["price"])
            size = float(raw_level["size"])
        else:
            price = float(raw_level[0])
            size = float(raw_level[1])
        if 0 < price < 1 and size > 0:
            levels.append(OrderBookLevel(price=price, size=size))
    return tuple(sorted(levels, key=lambda level: level.price, reverse=True))


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


def _timestamp_from_payload(payload: dict[str, Any]) -> datetime:
    for field_name in ("timestamp", "updated_time", "server_time"):
        raw_timestamp = payload.get(field_name)
        if raw_timestamp:
            return _parse_timestamp(raw_timestamp)
    return datetime.now(timezone.utc)


def _parse_timestamp(raw_timestamp: Any) -> datetime:
    if isinstance(raw_timestamp, int | float):
        return _epoch_to_datetime(float(raw_timestamp))
    timestamp = str(raw_timestamp)
    if timestamp.isdigit():
        return _epoch_to_datetime(float(timestamp))
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _epoch_to_datetime(raw_timestamp: float) -> datetime:
    seconds = raw_timestamp / 1000 if raw_timestamp > 10_000_000_000 else raw_timestamp
    return datetime.fromtimestamp(seconds, timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _venue_health(venue: str, is_healthy: bool, reason: str) -> VenueHealth:
    return VenueHealth(
        venue=venue,
        is_healthy=is_healthy,
        last_checked=datetime.now(timezone.utc),
        reason=reason,
    )
