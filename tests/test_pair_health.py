"""Offline coverage for the public pair-health evaluator."""
from __future__ import annotations

import asyncio

from tests.test_capture_rest import _pair

from arbx.pairs.health import evaluate_pairs


def test_pair_health_is_bounded_and_isolates_failures():
    pairs = [
        _pair(key="healthy"),
        _pair(key="unavailable", kalshi="KXDOWN"),
        _pair(key="error", kalshi="KXERROR"),
        _pair(key="slow", kalshi="KXSLOW"),
    ]
    active = 0
    peak = 0

    async def fetch(pair):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if pair.pair_key == "slow":
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0.01)
            if pair.pair_key == "error":
                raise RuntimeError("simulated fetch failure")
            if pair.pair_key == "unavailable":
                return None
            return type("Snapshot", (), {"skew_ms": 4.25})()
        finally:
            active -= 1

    results = asyncio.run(
        evaluate_pairs(pairs, fetch, max_concurrency=1, pair_timeout_s=0.02)
    )

    assert peak == 1
    assert results[0]["healthy"] is True
    assert results[0]["skew_ms"] == 4.25
    assert results[1]["healthy"] is False
    assert results[1]["status"] == "unavailable"
    assert results[1]["kalshi_market_id"] == "KXDOWN"
    assert results[2]["status"] == "error:RuntimeError"
    assert results[3]["status"] == "timeout"
