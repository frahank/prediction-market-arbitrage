# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Concurrent-REST paired capture source (kills sequential skew).
"""ConcurrentRestSource: fire both venue GETs concurrently per pair.

Replaces the sequential fetch-kalshi-then-fetch-polymarket pattern (which
bakes a ~110ms skew floor into every paired observation) with
``asyncio.gather`` per pair over one ``httpx.AsyncClient``. Pairing is by
completion within a cycle; ``skew_ms`` is measured, not assumed.

Simultaneous *firing* still leaves the structural per-venue RTT asymmetry in
the receive times (measured 2026-07-03 from this host: Polymarket ~232ms p50
vs Kalshi ~128ms → ~105ms skew). Each venue's round-trip is therefore
tracked as an EWMA and the slower venue is fired earlier by the RTT
difference, aligning receive times; ``skew_ms`` remains the honestly
measured residual. Read-only public GETs — the same URLs and normalizers as
the sync adapters; no order-mutation endpoint exists here.

Run a live smoke::

    python -m arbx.capture.rest_concurrent --pairs configs/pairs.approved.yaml --duration 120
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from arbx.capture.types import BookSnapshot, PairedSnapshot, paired_snapshot
from arbx.pairs.registry import PairSpec
from arbx.venues.kalshi_public import PUBLIC_HEADERS as KALSHI_HEADERS
from arbx.venues.kalshi_public import _book_from_payload
from arbx.venues.polymarket_public import PUBLIC_HEADERS as POLY_HEADERS
from arbx.venues.polymarket_public import _to_fixture_shape, normalize_poly_book

KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
POLYMARKET_BASE_URL = "https://clob.polymarket.com"

CONNECTOR_SOURCE = "live_public"

# RTT-compensation EWMA weight for the newest sample, and a safety cap on how
# far ahead the slower venue may be fired.
_EWMA_ALPHA = 0.2
_MAX_HEAD_START_S = 0.5


def _poly_market_id(pair: PairSpec) -> str:
    # Same fetch id the recorder/edge layer key on: the YES CLOB token id,
    # condition id only as a last resort.
    return pair.polymarket_yes_token_id or pair.polymarket_condition_id


class ConcurrentRestSource:
    """``MarketDataSource`` over concurrent public REST fetches."""

    def __init__(
        self,
        pairs: list[PairSpec],
        interval_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.pairs = pairs
        self.interval_s = interval_s
        self._client = client
        self._owns_client = client is None
        self._rtt_ewma_ms: dict[str, float] = {}

    async def subscribe(self, pairs: list[PairSpec]) -> AsyncIterator[PairedSnapshot]:
        client = self._client or httpx.AsyncClient(timeout=10.0)
        self._client = client
        try:
            next_cycle_at = time.monotonic()
            while True:
                # Stagger pairs evenly across the cycle instead of bursting
                # 2×len(pairs) sockets at once: the burst itself was measured
                # to double per-request latency and add ~100ms of jitter,
                # swamping the skew the concurrency is meant to remove.
                spacing_s = self.interval_s / max(1, len(pairs))
                results = await asyncio.gather(*(
                    self._after(i * spacing_s, self._fetch_pair(client, pair))
                    for i, pair in enumerate(pairs)
                ))
                for paired in results:
                    if paired is not None:
                        yield paired
                next_cycle_at += self.interval_s
                delay = next_cycle_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_cycle_at = time.monotonic()
        finally:
            if self._owns_client:
                await client.aclose()
                self._client = None

    async def fetch_pair(
        self, client: httpx.AsyncClient, pair: PairSpec
    ) -> PairedSnapshot | None:
        """Public seam: one pair's concurrent, RTT-compensated paired fetch.

        Lets a caller that owns the batching/cadence (the live scanner) reuse
        the exact fetch + normalize + skew-measurement path without the cycle
        loop in :meth:`subscribe`.
        """
        return await self._fetch_pair(client, pair)

    async def _fetch_pair(
        self, client: httpx.AsyncClient, pair: PairSpec
    ) -> PairedSnapshot | None:
        # RTT compensation: fire the slower venue first by the EWMA round-trip
        # difference so receive times line up (zero until both EWMAs warm up).
        lead_ms = self._rtt_ewma_ms.get("kalshi", 0.0) - self._rtt_ewma_ms.get(
            "polymarket", 0.0
        )
        kalshi_delay = min(max(-lead_ms, 0.0) / 1000.0, _MAX_HEAD_START_S)
        poly_delay = min(max(lead_ms, 0.0) / 1000.0, _MAX_HEAD_START_S)
        kalshi, poly = await asyncio.gather(
            self._after(kalshi_delay, self._fetch_kalshi(client, pair.kalshi_market_id)),
            self._after(poly_delay, self._fetch_polymarket(client, _poly_market_id(pair))),
        )
        # One venue failing skips this pair for the cycle, never the cycle.
        if kalshi is None or poly is None:
            return None
        return paired_snapshot(pair.pair_key, kalshi, poly)

    @staticmethod
    async def _after(delay_s: float, coro):
        try:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
        except BaseException:  # cancelled during the delay: close, don't leak
            coro.close()
            raise
        return await coro

    def _update_rtt(self, venue: str, elapsed_ms: float) -> None:
        previous = self._rtt_ewma_ms.get(venue)
        self._rtt_ewma_ms[venue] = (
            elapsed_ms
            if previous is None
            else (1 - _EWMA_ALPHA) * previous + _EWMA_ALPHA * elapsed_ms
        )

    async def _fetch_kalshi(
        self, client: httpx.AsyncClient, market_id: str
    ) -> BookSnapshot | None:
        url = f"{KALSHI_BASE_URL}/markets/{quote(market_id, safe='')}/orderbook"
        payload, timing = await self._get_json(client, url, headers=KALSHI_HEADERS)
        if payload is None:
            return None
        self._update_rtt("kalshi", timing[2])
        try:
            book = _book_from_payload(payload, market_id)
        except ValueError:
            return None
        return self._snapshot(book, timing)

    async def _fetch_polymarket(
        self, client: httpx.AsyncClient, market_id: str
    ) -> BookSnapshot | None:
        url = f"{POLYMARKET_BASE_URL}/book?token_id={quote(market_id, safe='')}"
        payload, timing = await self._get_json(client, url, headers=POLY_HEADERS)
        if payload is None:
            return None
        self._update_rtt("polymarket", timing[2])
        try:
            shaped = _to_fixture_shape(payload, market_id)
            shaped.pop("_empty_book", None)
            book = normalize_poly_book(shaped)
        except ValueError:
            return None
        return self._snapshot(book, timing)

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, *, headers: dict[str, str]
    ) -> tuple[dict[str, Any] | None, tuple[int, datetime, float]]:
        started = time.monotonic()
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            return None, (0, datetime.now(timezone.utc), 0.0)
        recv_ns = time.monotonic_ns()
        capture_ts = datetime.now(timezone.utc)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if response.status_code != 200:
            return None, (recv_ns, capture_ts, elapsed_ms)
        try:
            payload = response.json()
        except ValueError:
            return None, (recv_ns, capture_ts, elapsed_ms)
        if not isinstance(payload, dict):
            return None, (recv_ns, capture_ts, elapsed_ms)
        return payload, (recv_ns, capture_ts, elapsed_ms)

    @staticmethod
    def _snapshot(book, timing: tuple[int, datetime, float]) -> BookSnapshot:
        recv_ns, capture_ts, elapsed_ms = timing
        return BookSnapshot.from_order_book(
            book,
            recv_monotonic_ns=recv_ns,
            capture_ts_utc=capture_ts,
            fetch_elapsed_ms=elapsed_ms,
            connector_source=CONNECTOR_SOURCE,
        )


async def _smoke(pairs_path: str, data_dir: str, duration_s: float, interval_s: float) -> None:
    from pathlib import Path

    from arbx.capture.clock import measure_ntp_offset_ms
    from arbx.capture.sink import ObservationSink
    from arbx.pairs.registry import load_pairs

    pairs = load_pairs(Path(pairs_path))
    ntp = measure_ntp_offset_ms()
    run_id = f"rest_concurrent_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    sink = ObservationSink(Path(data_dir), run_id=run_id, ntp_offset_ms=ntp)
    source = ConcurrentRestSource(pairs, interval_s=interval_s)
    skews: list[float] = []
    deadline = time.monotonic() + duration_s
    print(f"[rest_concurrent] run_id={run_id} pairs={len(pairs)} "
          f"interval={interval_s}s ntp_offset_ms={ntp}")
    try:
        async for paired in source.subscribe(pairs):
            sink.write_paired(paired)
            skews.append(abs(paired.skew_ms))
            if time.monotonic() >= deadline:
                break
    finally:
        sink.close()
    if skews:
        skews.sort()
        p50 = skews[len(skews) // 2]
        p95 = skews[min(len(skews) - 1, int(len(skews) * 0.95))]
        print(f"[rest_concurrent] paired={len(skews)} |skew_ms| p50={p50:.1f} "
              f"p95={p95:.1f} max={skews[-1]:.1f}")
    else:
        print("[rest_concurrent] no paired snapshots captured")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Concurrent-REST paired capture smoke")
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--data-dir", default="data_capture_smoke")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    asyncio.run(_smoke(args.pairs, args.data_dir, args.duration, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
