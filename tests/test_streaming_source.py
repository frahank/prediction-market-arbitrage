"""StreamingSource: pairing on either leg's update, staleness suppression, skew."""

import asyncio
import time
from datetime import datetime, timezone

from arbx.capture.engine import StreamingSource
from arbx.capture.types import BookSnapshot
from arbx.pairs.registry import PairSpec

TOKEN = "tok-yes-a"


def _pair() -> PairSpec:
    return PairSpec(
        pair_key="pair-a",
        kalshi_market_id="KXTEST-26",
        polymarket_condition_id="0xcond",
        polymarket_yes_token_id=TOKEN,
        polymarket_no_token_id="tok-no",
        orientation="yes_yes",
        status="approved",
        include_in_strategy_metrics=True,
        raw={},
    )


def _snap(venue, market_id, *, age_s=0.0) -> BookSnapshot:
    return BookSnapshot(
        venue=venue,
        market_id=market_id,
        recv_monotonic_ns=time.monotonic_ns() - int(age_s * 1e9),
        capture_ts_utc=datetime.now(timezone.utc),
        venue_book_ts=datetime.now(timezone.utc),
        bids=((0.40, 10.0),),
        asks=((0.44, 8.0),),
        fetch_elapsed_ms=None,
        connector_source="streaming",
    )


class _ScriptedStream:
    """Pushes scripted snapshots via on_book, then idles forever."""

    def __init__(self, snapshots):
        self._snapshots = snapshots

    async def run(self, market_ids, on_book):
        for snap in self._snapshots:
            on_book(snap)
            await asyncio.sleep(0)
        await asyncio.Event().wait()


async def _collect(source, pairs, n, timeout_s=2.0):
    out = []

    async def consume():
        async for paired in source.subscribe(pairs):
            out.append(paired)
            if len(out) >= n:
                break

    try:
        await asyncio.wait_for(consume(), timeout=timeout_s)
    except asyncio.TimeoutError:
        pass
    return out


def test_pair_emitted_on_either_leg_update():
    k1, k2 = _snap("kalshi", "KXTEST-26"), _snap("kalshi", "KXTEST-26")
    p1 = _snap("polymarket", TOKEN)
    source = StreamingSource(
        [_pair()],
        kalshi_stream=_ScriptedStream([k1, k2]),
        polymarket_stream=_ScriptedStream([p1]),
    )
    emitted = asyncio.run(_collect(source, [_pair()], n=3, timeout_s=0.5))
    # First update (whichever leg) has no counterpart yet; each later update
    # from EITHER leg pairs with the other leg's latest book.
    assert len(emitted) == 2
    assert all(e.pair_key == "pair-a" for e in emitted)
    assert source.updates["kalshi"] == 2 and source.updates["polymarket"] == 1


def test_stale_leg_suppresses_pairs():
    stale_poly = _snap("polymarket", TOKEN, age_s=120.0)
    k1 = _snap("kalshi", "KXTEST-26")
    source = StreamingSource(
        [_pair()],
        stale_after_s=30.0,
        kalshi_stream=_ScriptedStream([k1]),
        polymarket_stream=_ScriptedStream([stale_poly]),
    )
    emitted = asyncio.run(_collect(source, [_pair()], n=1, timeout_s=0.5))
    assert emitted == []
    assert source.gap_count == 1


def test_sink_dedupes_unchanged_leg(tmp_path):
    import json

    from arbx.capture.sink import ObservationSink
    from arbx.capture.types import paired_snapshot

    poly = _snap("polymarket", TOKEN)
    k1, k2 = _snap("kalshi", "KXTEST-26"), _snap("kalshi", "KXTEST-26")
    sink = ObservationSink(tmp_path, run_id="dedupe-test")
    # Two kalshi updates pairing against the same unchanged poly book.
    sink.write_paired(paired_snapshot("pair-a", k1, poly))
    sink.write_paired(paired_snapshot("pair-a", k2, poly))
    sink.close()

    rows = [
        json.loads(line)
        for f in (tmp_path / "raw" / "book").rglob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    by_venue = {"kalshi": 0, "polymarket": 0}
    for row in rows:
        by_venue[row["venue"]] += 1
    assert by_venue == {"kalshi": 2, "polymarket": 1}, "unchanged leg written once"
    assert len({r["capture_seq"] for r in rows}) == 3


def test_skew_is_age_difference():
    poly = _snap("polymarket", TOKEN, age_s=0.5)  # 500ms older than the kalshi leg
    kalshi = _snap("kalshi", "KXTEST-26")
    source = StreamingSource(
        [_pair()],
        kalshi_stream=_ScriptedStream([kalshi]),
        polymarket_stream=_ScriptedStream([poly]),
    )
    [paired] = asyncio.run(_collect(source, [_pair()], n=1, timeout_s=0.5))
    expected_ms = (kalshi.recv_monotonic_ns - poly.recv_monotonic_ns) / 1e6
    assert paired.skew_ms == expected_ms
    assert 400.0 < paired.skew_ms < 700.0
    assert paired.recv_monotonic_ns == kalshi.recv_monotonic_ns
