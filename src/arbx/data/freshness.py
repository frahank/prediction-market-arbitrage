# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Single source of truth for per-row market-data freshness.
"""
Freshness / staleness for recorded book observations.

A book row is "stale" when the venue's own book timestamp (``venue_book_ts``)
is older than ``threshold`` seconds at the moment we captured it
(``capture_ts_utc``)::

    staleness_seconds = capture_ts_utc - venue_book_ts

Both the recorder (which stamps each row) and the data-quality report (which
scores a soak) import from here so the definition never drifts between the
column we store and the metric we grade.

Important semantics caveat: ``venue_book_ts`` is venue-side and its meaning is
venue-specific. For a venue whose timestamp means "last time the book changed",
an illiquid-but-valid market will legitimately read as stale without any
recorder fault. Treat staleness as a *market-activity / freshness* signal, not
as a recorder-health failure (see ``data_quality`` for the health/freshness
split).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Default freshness threshold. Kept here so the recorder column and the DQ
# acceptance metric share one constant. Tune per-venue later.
DEFAULT_FRESHNESS_THRESHOLD_SECONDS = 60.0

# freshness_status values
FRESH = "fresh"
STALE = "stale"
MISSING_VENUE_TIMESTAMP = "missing_venue_timestamp"
MISSING_BOOK = "missing_book"


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or pass through a datetime; None on failure.

    Naive datetimes are assumed UTC so subtraction is always well-defined.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def compute_freshness(
    capture_ts_utc: Any,
    venue_book_ts: Any,
    *,
    best_bid: Any = None,
    best_ask: Any = None,
    threshold_seconds: float = DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
) -> tuple[float | None, str]:
    """Return ``(staleness_seconds, freshness_status)`` for one observation.

    ``staleness_seconds`` is ``capture_ts_utc - venue_book_ts`` in seconds when
    both timestamps are present, else ``None``. Status precedence:

    1. ``missing_book``            — no best bid and no best ask
    2. ``missing_venue_timestamp`` — book present but no parseable venue ts
    3. ``stale`` / ``fresh``       — by the threshold
    """
    cap = parse_ts(capture_ts_utc)
    vbt = parse_ts(venue_book_ts)
    staleness = (cap - vbt).total_seconds() if (cap is not None and vbt is not None) else None

    if best_bid is None and best_ask is None:
        return staleness, MISSING_BOOK
    if vbt is None or cap is None:
        return staleness, MISSING_VENUE_TIMESTAMP
    return staleness, (STALE if staleness is not None and staleness > threshold_seconds else FRESH)


def is_modeling_viable(freshness_status: str) -> bool:
    """A single-row gate for the fresh modeling slice: fresh rows only."""
    return freshness_status == FRESH
