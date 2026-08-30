# Unit tests for the shared freshness definition.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arbx.data.freshness import (
    FRESH,
    MISSING_BOOK,
    MISSING_VENUE_TIMESTAMP,
    STALE,
    compute_freshness,
    is_modeling_viable,
)

BASE = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


def test_fresh_within_threshold():
    staleness, status = compute_freshness(BASE, BASE - timedelta(seconds=10), best_bid=0.5, best_ask=0.55)
    assert status == FRESH
    assert staleness == 10.0
    assert is_modeling_viable(status)


def test_stale_beyond_threshold():
    staleness, status = compute_freshness(BASE, BASE - timedelta(seconds=120), best_bid=0.5, best_ask=0.55)
    assert status == STALE
    assert staleness == 120.0
    assert not is_modeling_viable(status)


def test_missing_book_takes_precedence_over_staleness():
    staleness, status = compute_freshness(BASE, BASE - timedelta(seconds=120), best_bid=None, best_ask=None)
    assert status == MISSING_BOOK
    assert staleness == 120.0  # still reported for diagnostics


def test_missing_venue_timestamp():
    staleness, status = compute_freshness(BASE, None, best_bid=0.5, best_ask=0.55)
    assert status == MISSING_VENUE_TIMESTAMP
    assert staleness is None


def test_accepts_iso_strings():
    staleness, status = compute_freshness(BASE.isoformat(), (BASE - timedelta(seconds=5)).isoformat(),
                                          best_bid=0.5, best_ask=0.55)
    assert status == FRESH
    assert staleness == 5.0


def test_custom_threshold():
    _, status = compute_freshness(BASE, BASE - timedelta(seconds=20), best_bid=0.5, best_ask=0.55,
                                  threshold_seconds=10.0)
    assert status == STALE
