# Scope: BOT_RUNTIME — Capture-layer types: the one seam between capture, decision, and replay.
"""Capture-layer snapshot types (Phase 3 contract).

``PairedSnapshot`` is the ONE seam between capture, decision, and replay:
Phase 6's replay source and Phase 7's decision engine both code against
``MarketDataSource``. ``BookSnapshot.to_observation_row`` emits rows
dict-compatible with ``book_observations`` (docs/dataset_schema.md §2.1),
matching ``arbx.data.recorder.book_to_observation`` column-for-column.

Price convention (post docs/book_semantics_fix.md): ``bids``/``asks`` are in
YES-price space, best first — ``bids[0]`` is the highest resting YES bid,
``asks[0]`` the cheapest executable YES ask. At least the top 5 levels are
retained (more is fine; ``book_json`` preserves whatever is carried).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Protocol

from arbx.core.models import OrderBook
from arbx.data.freshness import (
    DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
    compute_freshness,
)
from arbx.pairs.registry import PairSpec

_TOP_N = 5


@dataclass(frozen=True)
class BookSnapshot:
    # dict-compatible with book_observations schema §2.1
    venue: str
    market_id: str
    recv_monotonic_ns: int
    capture_ts_utc: datetime
    venue_book_ts: datetime | None
    bids: tuple[tuple[float, float], ...]  # (price, size), best first, ≥ top-5 retained
    asks: tuple[tuple[float, float], ...]
    fetch_elapsed_ms: float | None
    connector_source: str  # "live_public" | "streaming"

    @classmethod
    def from_order_book(
        cls,
        book: OrderBook,
        *,
        recv_monotonic_ns: int,
        capture_ts_utc: datetime,
        fetch_elapsed_ms: float | None,
        connector_source: str,
    ) -> BookSnapshot:
        """Convert a normalized ``OrderBook`` (bid ladders per side, best
        first) into YES-price bids/asks — the same mapping the recorder uses
        (ask price = 1 − NO bid)."""
        return cls(
            venue=book.venue,
            market_id=book.market_id,
            recv_monotonic_ns=recv_monotonic_ns,
            capture_ts_utc=capture_ts_utc,
            venue_book_ts=book.timestamp,
            bids=tuple((lv.price, lv.size) for lv in book.yes_levels),
            asks=tuple((round(1.0 - lv.price, 10), lv.size) for lv in book.no_levels),
            fetch_elapsed_ms=fetch_elapsed_ms,
            connector_source=connector_source,
        )

    def to_observation_row(
        self, *, run_id: str, capture_seq: int, ntp_offset_ms: float | None
    ) -> dict[str, Any]:
        best_bid = self.bids[0][0] if self.bids else None
        best_ask = self.asks[0][0] if self.asks else None
        both = best_bid is not None and best_ask is not None
        staleness_seconds, freshness_status = compute_freshness(
            self.capture_ts_utc,
            self.venue_book_ts,
            best_bid=best_bid,
            best_ask=best_ask,
            threshold_seconds=DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
        )
        row: dict[str, Any] = {
            "venue": self.venue,
            "market_id": self.market_id,
            "capture_seq": capture_seq,
            "capture_ts_utc": self.capture_ts_utc.isoformat(),
            "recv_monotonic_ns": self.recv_monotonic_ns,
            "venue_book_ts": self.venue_book_ts.isoformat() if self.venue_book_ts else None,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": ((best_bid + best_ask) / 2.0) if both else None,
            "spread": (best_ask - best_bid) if both else None,
            "staleness_seconds": staleness_seconds,
            "freshness_status": freshness_status,
            "freshness_threshold_seconds": DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
            "fetch_elapsed_ms": self.fetch_elapsed_ms,
            "connector_source": self.connector_source,
            "reportable": True,
            "run_id": run_id,
            "ntp_offset_ms": ntp_offset_ms,
        }
        for i in range(1, _TOP_N + 1):
            bid = self.bids[i - 1] if i <= len(self.bids) else None
            row[f"bid_px_{i}"] = bid[0] if bid else None
            row[f"bid_sz_{i}"] = bid[1] if bid else None
        for i in range(1, _TOP_N + 1):
            ask = self.asks[i - 1] if i <= len(self.asks) else None
            row[f"ask_px_{i}"] = ask[0] if ask else None
            row[f"ask_sz_{i}"] = ask[1] if ask else None
        # Same fidelity payload the recorder stores: bid ladders per side
        # (the NO bid pricing an ask at p sits at 1 − p).
        row["book_json"] = json.dumps({
            "yes_levels": [{"price": px, "size": sz} for px, sz in self.bids],
            "no_levels": [{"price": round(1.0 - px, 10), "size": sz} for px, sz in self.asks],
        })
        return row


@dataclass(frozen=True)
class PairedSnapshot:
    pair_key: str
    kalshi: BookSnapshot
    polymarket: BookSnapshot
    skew_ms: float  # kalshi.recv_ns − poly.recv_ns, in ms
    recv_monotonic_ns: int  # max of the two


class MarketDataSource(Protocol):
    def subscribe(self, pairs: list[PairSpec]) -> AsyncIterator[PairedSnapshot]: ...


def paired_snapshot(pair_key: str, kalshi: BookSnapshot, polymarket: BookSnapshot) -> PairedSnapshot:
    """Build a ``PairedSnapshot`` with the contract's skew/recv semantics."""
    return PairedSnapshot(
        pair_key=pair_key,
        kalshi=kalshi,
        polymarket=polymarket,
        skew_ms=(kalshi.recv_monotonic_ns - polymarket.recv_monotonic_ns) / 1e6,
        recv_monotonic_ns=max(kalshi.recv_monotonic_ns, polymarket.recv_monotonic_ns),
    )
