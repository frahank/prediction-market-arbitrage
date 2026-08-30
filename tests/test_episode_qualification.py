# qualifies() predicate unchanged after the port.
from __future__ import annotations

from arbx.analysis.episodes import qualifies


def _qualifying_row() -> dict:
    return {
        "books_fresh": True,
        "capture_skew_ms": 10,
        "fee_adj_edge": 0.03,
        "depth_adj_edge": 0.03,
        "max_profitable_size": 100,
    }


def test_qualifies_predicate_unchanged():
    assert qualifies(_qualifying_row()) is True

    # Each gate, failed one at a time, must flip the verdict to False.
    stale = {**_qualifying_row(), "books_fresh": False}
    assert qualifies(stale) is False

    skewed = {**_qualifying_row(), "capture_skew_ms": 300}  # >= MAX_SKEW_MS (250)
    assert qualifies(skewed) is False

    thin_fee = {**_qualifying_row(), "fee_adj_edge": 0.005}  # < MIN_EDGE (0.01)
    assert qualifies(thin_fee) is False

    thin_depth = {**_qualifying_row(), "depth_adj_edge": 0.005}  # < MIN_EDGE
    assert qualifies(thin_depth) is False

    illiquid = {**_qualifying_row(), "max_profitable_size": 0.5}  # < MIN_FILL_SIZE (1.0)
    assert qualifies(illiquid) is False
