# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Polymarket CLOB WebSocket book stream (public market channel).
"""PolymarketBookStream: local books from the CLOB WS market channel.

Verified against the official docs (2026-07-03,
``docs.polymarket.com/developers/CLOB/websocket/market-channel``):

- Endpoint: ``wss://ws-subscriptions-clob.polymarket.com/ws/market`` —
  public, **no authentication**.
- Subscribe: ``{"assets_ids": [<token_id>, ...], "type": "market"}``.
- ``book``: full book per ``asset_id`` with ``bids``/``asks`` lists of
  ``{"price": str, "size": str}`` in YES-token dollars and an epoch-millis
  ``timestamp``.
- ``price_change``: ``price_changes`` list of per-asset level updates
  (``price``, ``size``, ``side`` in {BUY, SELL}); ``size == "0"`` removes
  the level. Messages may arrive as single objects or JSON arrays.

Books are kept directly in YES-price space (bids = resting buys, asks =
resting sells) — the same orientation the fixed REST normalizer produces
(docs/book_semantics_fix.md). Subscribe-only; no order capability here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from arbx.capture.types import BookSnapshot

logger = logging.getLogger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CONNECTOR_SOURCE = "streaming"

_BACKOFF_INITIAL_S = 1.0
_BACKOFF_CAP_S = 60.0


class _TokenBook:
    """One token's book: YES bids and YES asks as price -> size dicts."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def load_book(self, msg: dict[str, Any]) -> None:
        self.bids = _levels(msg.get("bids"))
        self.asks = _levels(msg.get("asks"))

    def apply_change(self, change: dict[str, Any]) -> None:
        price = _num(change.get("price"))
        size = _num(change.get("size"))
        side = str(change.get("side", "")).upper()
        if price is None or size is None or not 0 < price < 1:
            return
        book = self.bids if side == "BUY" else self.asks if side == "SELL" else None
        if book is None:
            return
        if size > 0:
            book[price] = size
        else:
            book.pop(price, None)  # size "0" removes the level


def _num(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _levels(raw: Any) -> dict[float, float]:
    out: dict[float, float] = {}
    for level in raw or []:
        price, size = _num(level.get("price")), _num(level.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            out[price] = size
    return out


class PolymarketBookStream:
    def __init__(
        self,
        *,
        url: str = POLYMARKET_WS_URL,
        connect: Callable[..., Any] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_reconnects: int | None = None,
    ) -> None:
        """``connect``/``sleeper``/``max_reconnects`` are test seams —
        ``connect(url)`` must return an async context manager whose value has
        ``send``/``recv`` coroutines."""
        self._url = url
        self._connect = connect
        self._sleeper = sleeper
        self._max_reconnects = max_reconnects
        self.reconnects = 0

    async def run(
        self,
        market_ids: list[str],
        on_book: Callable[[BookSnapshot], None],
    ) -> None:
        """``market_ids`` are YES CLOB token ids (PairSpec.polymarket_yes_token_id)."""
        connect = self._connect or self._default_connect
        backoff_s = _BACKOFF_INITIAL_S
        attempts = 0
        while self._max_reconnects is None or attempts <= self._max_reconnects:
            try:
                async with connect(self._url) as ws:
                    backoff_s = _BACKOFF_INITIAL_S
                    await self._session(ws, market_ids, on_book)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any transport error
                logger.warning("polymarket ws disconnected (%s); retrying in %.0fs", exc, backoff_s)
            attempts += 1
            self.reconnects += 1
            await self._sleeper(backoff_s)
            backoff_s = min(backoff_s * 2, _BACKOFF_CAP_S)

    def _default_connect(self, url: str):
        import websockets

        return websockets.connect(url)

    async def _session(
        self,
        ws: Any,
        market_ids: list[str],
        on_book: Callable[[BookSnapshot], None],
    ) -> None:
        await ws.send(json.dumps({"assets_ids": list(market_ids), "type": "market"}))
        books: dict[str, _TokenBook] = {}
        wanted = set(market_ids)
        while True:
            raw = await ws.recv()
            payload = json.loads(raw)
            events = payload if isinstance(payload, list) else [payload]
            for event in events:
                if not isinstance(event, dict):
                    continue
                self._handle_event(event, books, wanted, on_book)

    def _handle_event(
        self,
        event: dict[str, Any],
        books: dict[str, _TokenBook],
        wanted: set[str],
        on_book: Callable[[BookSnapshot], None],
    ) -> None:
        event_type = event.get("event_type")
        ts = _num(event.get("timestamp"))
        if event_type == "book":
            token = str(event.get("asset_id", ""))
            if token not in wanted:
                return
            book = books.setdefault(token, _TokenBook())
            book.load_book(event)
            on_book(self._snapshot(token, book, ts))
        elif event_type == "price_change":
            touched: set[str] = set()
            for change in event.get("price_changes", []) or []:
                token = str(change.get("asset_id", ""))
                if token not in wanted or token not in books:
                    continue  # never apply deltas before the initial book
                books[token].apply_change(change)
                touched.add(token)
            for token in touched:
                on_book(self._snapshot(token, books[token], ts))

    @staticmethod
    def _snapshot(token: str, book: _TokenBook, ts_epoch: float | None) -> BookSnapshot:
        venue_book_ts = None
        if ts_epoch is not None and ts_epoch > 0:
            seconds = ts_epoch / 1000.0 if ts_epoch > 10_000_000_000 else ts_epoch
            venue_book_ts = datetime.fromtimestamp(seconds, timezone.utc)
        return BookSnapshot(
            venue="polymarket",
            market_id=token,
            recv_monotonic_ns=time.monotonic_ns(),
            capture_ts_utc=datetime.now(timezone.utc),
            venue_book_ts=venue_book_ts,
            bids=tuple(sorted(book.bids.items(), key=lambda lv: lv[0], reverse=True)),
            asks=tuple(sorted(book.asks.items(), key=lambda lv: lv[0])),
            fetch_elapsed_ms=None,
            connector_source=CONNECTOR_SOURCE,
        )
