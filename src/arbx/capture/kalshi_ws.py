# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Kalshi WebSocket order-book stream (subscribe-only, market data).
"""KalshiBookStream: local order books from the Kalshi WS orderbook_delta channel.

Verified against the official docs (2026-07-03,
``docs.kalshi.com/websockets/orderbook-updates`` and
``getting_started/quick_start_websockets``):

- Endpoint: ``wss://api.elections.kalshi.com/trade-api/ws/v2``.
- **The connection handshake requires authentication** (``KALSHI-ACCESS-KEY``
  / ``KALSHI-ACCESS-SIGNATURE`` / ``KALSHI-ACCESS-TIMESTAMP``, RSA-signed
  ``timestamp + "GET" + "/trade-api/ws/v2"``) — there is no unauthenticated
  market-data WS. This module is credential-parameterized (``auth_headers``)
  and fails with a clear error
  without them. See docs/capture_notes.md.
- Subscribe: ``{"id": n, "cmd": "subscribe", "params": {"channels":
  ["orderbook_delta"], "market_tickers": [...]}}``.
- ``orderbook_snapshot``: full book per market, ``yes_dollars_fp`` /
  ``no_dollars_fp`` bid ladders as ``[price_dollars, count]`` string pairs
  (legacy cents-integer ``yes``/``no`` also accepted and converted).
- ``orderbook_delta``: ``price_dollars`` (or cents ``price``), ``delta_fp``
  (or ``delta``), ``side`` in {yes, no}, optional ``ts_ms``.
- ``seq`` is "sequential ... if you want to guarantee you received all the
  messages": any gap invalidates the local book → drop state and
  resubscribe.

Subscribe-only: this client sends nothing but subscribe commands. No order
capability exists here.
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

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
CONNECTOR_SOURCE = "streaming"

_BACKOFF_INITIAL_S = 1.0
_BACKOFF_CAP_S = 60.0


class KalshiWsAuthError(RuntimeError):
    """Raised when the auth-gated Kalshi WS is used without credentials."""


class _SeqGap(Exception):
    """Internal: a seq discontinuity invalidated the local books."""


class _SideBook:
    """One market's bid ladders, mutated by snapshot/delta messages."""

    def __init__(self) -> None:
        self.bids: dict[str, dict[float, float]] = {"yes": {}, "no": {}}

    def load_snapshot(self, msg: dict[str, Any]) -> None:
        self.bids = {
            "yes": _ladder(msg, "yes_dollars_fp", "yes"),
            "no": _ladder(msg, "no_dollars_fp", "no"),
        }

    def apply_delta(self, msg: dict[str, Any]) -> None:
        side = str(msg.get("side", ""))
        if side not in self.bids:
            return
        price = _price(msg.get("price_dollars", msg.get("price")))
        if price is None:
            return
        delta = float(msg.get("delta_fp", msg.get("delta", 0)) or 0)
        book = self.bids[side]
        quantity = book.get(price, 0.0) + delta
        if quantity > 0:
            book[price] = quantity
        else:
            book.pop(price, None)


def _price(raw: Any) -> float | None:
    if raw is None:
        return None
    value = float(raw)
    if value > 1:  # legacy cents payloads
        value = value / 100.0
    return value if 0 < value < 1 else None


def _ladder(msg: dict[str, Any], dollars_key: str, cents_key: str) -> dict[float, float]:
    raw = msg.get(dollars_key)
    if raw is None:
        raw = msg.get(cents_key, [])
    ladder: dict[float, float] = {}
    for level in raw or []:
        price = _price(level[0])
        size = float(level[1])
        if price is not None and size > 0:
            ladder[price] = size
    return ladder


class KalshiBookStream:
    def __init__(
        self,
        *,
        url: str = KALSHI_WS_URL,
        auth_headers: Callable[[], dict[str, str]] | None = None,
        connect: Callable[..., Any] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_reconnects: int | None = None,
    ) -> None:
        """``auth_headers`` builds the signed handshake headers per attempt
        for every attempt. ``connect``/``sleeper``/``max_reconnects`` are test seams —
        ``connect(url, headers)`` must return an async context manager whose
        value has ``send``/``recv`` coroutines."""
        self._url = url
        self._auth_headers = auth_headers
        self._connect = connect
        self._sleeper = sleeper
        self._max_reconnects = max_reconnects
        self.reconnects = 0
        self.seq_gaps = 0

    async def run(
        self,
        market_ids: list[str],
        on_book: Callable[[BookSnapshot], None],
    ) -> None:
        if self._connect is None and self._auth_headers is None:
            raise KalshiWsAuthError(
                "Kalshi WebSocket market data requires an authenticated handshake "
                "(KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP; see "
                "docs/capture_notes.md). Pass "
                "auth_headers to enable, or use ConcurrentRestSource for the "
                "kalshi leg meanwhile."
            )
        connect = self._connect or self._default_connect
        backoff_s = _BACKOFF_INITIAL_S
        attempts = 0
        while self._max_reconnects is None or attempts <= self._max_reconnects:
            try:
                async with connect(self._url, self._headers()) as ws:
                    backoff_s = _BACKOFF_INITIAL_S  # healthy connection
                    await self._session(ws, market_ids, on_book)
            except _SeqGap:
                self.seq_gaps += 1
                logger.warning("kalshi ws seq gap — resubscribing with fresh books")
                continue  # immediate resubscribe, no backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any transport error
                logger.warning("kalshi ws disconnected (%s); retrying in %.0fs", exc, backoff_s)
            attempts += 1
            self.reconnects += 1
            await self._sleeper(backoff_s)
            backoff_s = min(backoff_s * 2, _BACKOFF_CAP_S)

    def _headers(self) -> dict[str, str]:
        return self._auth_headers() if self._auth_headers is not None else {}

    def _default_connect(self, url: str, headers: dict[str, str]):
        import websockets

        # websockets pings every 20s by default, satisfying keepalive.
        return websockets.connect(url, additional_headers=headers)

    async def _session(
        self,
        ws: Any,
        market_ids: list[str],
        on_book: Callable[[BookSnapshot], None],
    ) -> None:
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": list(market_ids)},
        }))
        books: dict[str, _SideBook] = {}
        last_seq: int | None = None
        while True:
            message = json.loads(await ws.recv())
            msg_type = message.get("type")
            if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
                continue  # subscribed acks, errors, other channels
            seq = message.get("seq")
            if isinstance(seq, int):
                if last_seq is not None and seq != last_seq + 1:
                    raise _SeqGap(f"seq {last_seq} -> {seq}")
                last_seq = seq
            msg = message.get("msg", {})
            ticker = str(msg.get("market_ticker", ""))
            if not ticker:
                continue
            book = books.setdefault(ticker, _SideBook())
            if msg_type == "orderbook_snapshot":
                book.load_snapshot(msg)
            else:
                book.apply_delta(msg)
            on_book(self._snapshot(ticker, book, msg))

    @staticmethod
    def _snapshot(ticker: str, book: _SideBook, msg: dict[str, Any]) -> BookSnapshot:
        ts_ms = msg.get("ts_ms")
        venue_book_ts = (
            datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc)
            if isinstance(ts_ms, (int, float))
            else None
        )
        yes_bids = sorted(book.bids["yes"].items(), key=lambda lv: lv[0], reverse=True)
        # A NO bid at p prices an executable YES ask at 1 − p; best (cheapest
        # YES ask) first — identical to the REST normalizer's row mapping.
        no_bids = sorted(book.bids["no"].items(), key=lambda lv: lv[0], reverse=True)
        asks = tuple((round(1.0 - px, 10), sz) for px, sz in no_bids)
        return BookSnapshot(
            venue="kalshi",
            market_id=ticker,
            recv_monotonic_ns=time.monotonic_ns(),
            capture_ts_utc=datetime.now(timezone.utc),
            venue_book_ts=venue_book_ts,
            bids=tuple(yes_bids),
            asks=asks,
            fetch_elapsed_ms=None,
            connector_source=CONNECTOR_SOURCE,
        )
