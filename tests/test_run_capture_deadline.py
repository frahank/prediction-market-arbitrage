"""Regression coverage for capture duration during a total source outage."""
from __future__ import annotations

import asyncio
import time

from scripts.run_capture import _until_deadline


def test_deadline_stops_source_that_never_yields():
    closed = False

    async def stalled_source():
        nonlocal closed
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed = True

    async def collect():
        deadline = time.monotonic() + 0.03
        return [item async for item in _until_deadline(stalled_source(), deadline)]

    started = time.monotonic()
    assert asyncio.run(collect()) == []
    assert time.monotonic() - started < 0.5
    assert closed is True
