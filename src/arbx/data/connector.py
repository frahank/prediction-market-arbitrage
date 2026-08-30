# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Minimal public market-data connector for the recorder.
"""Minimal public market-data connector.

Contains only the pieces the recorder needs: the
``PublicMarketDataConnector`` protocol, the
``AdapterMarketDataConnector`` used by the recorder, and
``capture_connector_snapshot`` (with its ``ConnectorSnapshot`` return type and
the ``orderbook_to_record`` helper) required by the edge-survival probe. Nothing
from a paper-simulation layer is included.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

from arbx.core.models import ConnectorSource, OrderBook, VenueHealth
from arbx.venues.base import AdapterContract


@dataclass(frozen=True)
class ConnectorSnapshot:
    books: dict[str, OrderBook]
    request_started_at: dict[str, datetime]
    response_received_at: dict[str, datetime]
    fetch_elapsed_ms: dict[str, float]
    capture_skew_ms: float
    connector_sources: dict[str, ConnectorSource]

    def to_record(self) -> dict[str, Any]:
        return {
            "books": {
                venue: orderbook_to_record(book)
                for venue, book in self.books.items()
            },
            "request_started_at": {
                venue: value.isoformat()
                for venue, value in self.request_started_at.items()
            },
            "response_received_at": {
                venue: value.isoformat()
                for venue, value in self.response_received_at.items()
            },
            "fetch_elapsed_ms": dict(self.fetch_elapsed_ms),
            "capture_skew_ms": self.capture_skew_ms,
            "connector_sources": {
                venue: source.value
                for venue, source in self.connector_sources.items()
            },
        }


@runtime_checkable
class PublicMarketDataConnector(Protocol):
    venue: str
    connector_source: ConnectorSource

    def fetch_orderbook(self, market_id: str) -> OrderBook: ...

    def fetch_market_metadata(self, market_id: str) -> dict[str, Any] | None: ...

    def health(self) -> VenueHealth: ...


class AdapterMarketDataConnector(AdapterContract):
    def __init__(
        self,
        *,
        venue: str,
        adapter: AdapterContract,
        provider: Any | None,
        connector_source: ConnectorSource,
        source_reference: str,
        reportable: bool = True,
    ) -> None:
        self.venue = venue
        self.adapter = adapter
        self.provider = provider
        self.connector_source = connector_source
        self.source_reference = source_reference
        self.reportable = reportable

    def fetch_orderbook(self, market_id: str) -> OrderBook:
        book = self.adapter.fetch_orderbook(market_id)
        return replace(
            book,
            connector_source=self.connector_source,
            source_reference=self.source_reference,
            reportable=self.reportable,
        )

    def fetch_market_metadata(self, market_id: str) -> dict[str, Any] | None:
        fetcher = getattr(self.provider, "fetch_market_json", None)
        if not callable(fetcher):
            return None
        result = fetcher(market_id)
        return result.payload if result.health.is_healthy else None

    def health(self) -> VenueHealth:
        health = self.adapter.health()
        return replace(health, connector_source=self.connector_source)


def capture_connector_snapshot(
    connectors: Mapping[str, PublicMarketDataConnector],
    market_ids: Mapping[str, str],
) -> ConnectorSnapshot:
    started_at: dict[str, datetime] = {}
    finished_at: dict[str, datetime] = {}
    elapsed_ms: dict[str, float] = {}

    def fetch(venue: str) -> tuple[str, OrderBook]:
        started_at[venue] = datetime.now(timezone.utc)
        start = time.monotonic()
        book = connectors[venue].fetch_orderbook(market_ids[venue])
        elapsed_ms[venue] = max(0.0, (time.monotonic() - start) * 1000)
        finished_at[venue] = datetime.now(timezone.utc)
        return venue, book

    with ThreadPoolExecutor(max_workers=2) as executor:
        books = dict(executor.map(fetch, ("kalshi", "polymarket")))
    capture_skew = abs(
        (finished_at["kalshi"] - finished_at["polymarket"]).total_seconds()
        * 1000
    )
    return ConnectorSnapshot(
        books=books,
        request_started_at=started_at,
        response_received_at=finished_at,
        fetch_elapsed_ms=elapsed_ms,
        capture_skew_ms=capture_skew,
        connector_sources={
            venue: getattr(
                connectors[venue],
                "connector_source",
                books[venue].connector_source,
            )
            for venue in ("kalshi", "polymarket")
        },
    )


def orderbook_to_record(book: OrderBook) -> dict[str, Any]:
    return {
        "venue": book.venue,
        "market_id": book.market_id,
        "yes_levels": [asdict(level) for level in book.yes_levels],
        "no_levels": [asdict(level) for level in book.no_levels],
        "timestamp": book.timestamp.isoformat(),
        "fetched_at": None if book.fetched_at is None else book.fetched_at.isoformat(),
        "connector_source": book.connector_source.value,
        "source_reference": book.source_reference,
        "reportable": book.reportable,
    }
