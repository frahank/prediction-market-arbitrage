# Scope: BOT_RUNTIME — StreamingSource: unify venue streams behind MarketDataSource.
"""StreamingSource: one ``MarketDataSource`` over both venue book streams.

Holds the latest ``BookSnapshot`` per leg; whenever either leg updates, emits
a ``PairedSnapshot`` pairing the update with the other leg's latest book.
``skew_ms`` is the age difference of the two books' receive times, so
downstream freshness logic (``books_fresh``) stays honest. A leg with no
update for ``stale_after_s`` suppresses pairs (nothing is emitted) and the
gap is logged once per episode into the continuity log via the recorder's
``_HeartbeatWriter``.

Without Kalshi WebSocket credentials (docs/capture_notes.md), the Kalshi leg
defaults to ``KalshiRestPollStream`` — a REST-poll adapter behind
the same stream interface — while the polymarket leg is true WS push.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from arbx.capture.polymarket_ws import PolymarketBookStream
from arbx.capture.types import BookSnapshot, PairedSnapshot, paired_snapshot
from arbx.pairs.registry import PairSpec

logger = logging.getLogger(__name__)


class KalshiRestPollStream:
    """Stopgap kalshi leg: REST polling behind the book-stream interface.

    Same ``run(market_ids, on_book)`` seam as ``KalshiBookStream`` so
    ``StreamingSource`` can swap to the authenticated WS stream without any
    other change. Snapshots are stamped ``connector_source="live_public"``
    (they are REST fetches, not push updates).
    """

    def __init__(self, interval_s: float = 5.0, client: Any = None) -> None:
        self._interval_s = interval_s
        self._client = client

    async def run(self, market_ids: list[str], on_book) -> None:
        import httpx

        from arbx.capture.rest_concurrent import ConcurrentRestSource

        fetcher = ConcurrentRestSource([], interval_s=self._interval_s)
        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns = self._client is None
        try:
            next_cycle_at = time.monotonic()
            while True:
                spacing_s = self._interval_s / max(1, len(market_ids))
                snaps = await asyncio.gather(*(
                    fetcher._after(i * spacing_s, fetcher._fetch_kalshi(client, mid))
                    for i, mid in enumerate(market_ids)
                ))
                for snap in snaps:
                    if snap is not None:
                        on_book(snap)
                next_cycle_at += self._interval_s
                delay = next_cycle_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_cycle_at = time.monotonic()
        finally:
            if owns:
                await client.aclose()


class StreamingSource:
    """``MarketDataSource`` combining the two venue book streams."""

    def __init__(
        self,
        pairs: list[PairSpec],
        *,
        stale_after_s: float = 30.0,
        kalshi_stream: Any = None,
        polymarket_stream: Any = None,
        heartbeat: Any = None,  # recorder._HeartbeatWriter | None
        run_id: str = "streaming",
    ) -> None:
        self.pairs = pairs
        self.stale_after_s = stale_after_s
        self._kalshi_stream = kalshi_stream or KalshiRestPollStream()
        self._polymarket_stream = polymarket_stream or PolymarketBookStream()
        self._heartbeat = heartbeat
        self._run_id = run_id
        # Observability for capture_summary.json.
        self.updates: dict[str, int] = {"kalshi": 0, "polymarket": 0}
        self.gap_count = 0
        self._gap_open: set[tuple[str, str]] = set()

    async def subscribe(self, pairs: list[PairSpec]) -> AsyncIterator[PairedSnapshot]:
        pair_by_kalshi = {p.kalshi_market_id: p for p in pairs}
        pair_by_poly = {
            (p.polymarket_yes_token_id or p.polymarket_condition_id): p for p in pairs
        }
        latest: dict[tuple[str, str], BookSnapshot] = {}
        queue: asyncio.Queue[BookSnapshot] = asyncio.Queue()

        loop = asyncio.get_running_loop()

        def on_book(snapshot: BookSnapshot) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, snapshot)

        tasks = [
            asyncio.create_task(
                self._kalshi_stream.run(list(pair_by_kalshi), on_book),
                name="kalshi-book-stream",
            ),
            asyncio.create_task(
                self._polymarket_stream.run(list(pair_by_poly), on_book),
                name="polymarket-book-stream",
            ),
        ]
        try:
            while True:
                snapshot = await queue.get()
                self.updates[snapshot.venue] = self.updates.get(snapshot.venue, 0) + 1
                latest[(snapshot.venue, snapshot.market_id)] = snapshot

                if snapshot.venue == "kalshi":
                    pair = pair_by_kalshi.get(snapshot.market_id)
                    if pair is None:
                        continue
                    other_key = (
                        "polymarket",
                        pair.polymarket_yes_token_id or pair.polymarket_condition_id,
                    )
                else:
                    pair = pair_by_poly.get(snapshot.market_id)
                    if pair is None:
                        continue
                    other_key = ("kalshi", pair.kalshi_market_id)

                # A buffered/replayed update whose receive time is already
                # older than the staleness bound must not pair as "current".
                if self._leg_stale(snapshot):
                    continue
                self._gap_open.discard((snapshot.venue, snapshot.market_id))
                other = latest.get(other_key)
                if other is None or self._leg_stale(other):
                    continue
                if snapshot.venue == "kalshi":
                    yield paired_snapshot(pair.pair_key, snapshot, other)
                else:
                    yield paired_snapshot(pair.pair_key, other, snapshot)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _leg_stale(self, leg: BookSnapshot) -> bool:
        age_s = (time.monotonic_ns() - leg.recv_monotonic_ns) / 1e9
        if age_s <= self.stale_after_s:
            return False
        key = (leg.venue, leg.market_id)
        if key not in self._gap_open:  # log once per staleness episode
            self._gap_open.add(key)
            self.gap_count += 1
            logger.warning(
                "stream leg stale: %s/%s age=%.1fs > %.0fs — suppressing pairs",
                leg.venue, leg.market_id, age_s, self.stale_after_s,
            )
            if self._heartbeat is not None:
                self._heartbeat.write_event(
                    "stream_gap",
                    self._run_id,
                    venue=leg.venue,
                    market_id=leg.market_id,
                    age_s=age_s,
                    stale_after_s=self.stale_after_s,
                )
        return True
