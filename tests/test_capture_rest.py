"""ConcurrentRestSource: measured skew, schema-compatible rows, per-pair failure isolation."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from arbx.capture.rest_concurrent import ConcurrentRestSource
from arbx.capture.sink import ObservationSink
from arbx.core.models import OrderBook, OrderBookLevel
from arbx.data.recorder import book_to_observation
from arbx.pairs.registry import PairSpec


def _pair(key="pair-a", kalshi="KXTEST-26", token="tok-yes-a") -> PairSpec:
    return PairSpec(
        pair_key=key,
        kalshi_market_id=kalshi,
        polymarket_condition_id="0xcond",
        polymarket_yes_token_id=token,
        polymarket_no_token_id="tok-no",
        orientation="yes_yes",
        status="approved",
        include_in_strategy_metrics=True,
        raw={},
    )


KALSHI_BODY = {
    "orderbook_fp": {
        "yes_dollars": [["0.4000", "10.00"], ["0.4100", "5.00"]],
        "no_dollars": [["0.5700", "8.00"], ["0.5500", "3.00"]],
    },
}
POLY_BODY = {
    "asset_id": "tok-yes-a",
    "timestamp": "1782828000000",
    "bids": [{"price": "0.40", "size": "12"}],
    "asks": [{"price": "0.44", "size": "9"}],
}


def _transport(*, kalshi_delay_s=0.0, poly_delay_s=0.0, fail_kalshi_market=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if "kalshi" in request.url.host:
            if fail_kalshi_market and fail_kalshi_market in str(request.url.path):
                return httpx.Response(500, json={"error": "boom"})
            await asyncio.sleep(kalshi_delay_s)
            return httpx.Response(200, json=KALSHI_BODY)
        await asyncio.sleep(poly_delay_s)
        return httpx.Response(200, json=POLY_BODY)

    return httpx.MockTransport(handler)


async def _first_cycle(source, pairs, n=1):
    out = []
    async for paired in source.subscribe(pairs):
        out.append(paired)
        if len(out) >= n:
            break
    return out


def test_paired_snapshot_skew_measured():
    client = httpx.AsyncClient(
        transport=_transport(kalshi_delay_s=0.040, poly_delay_s=0.010)
    )
    source = ConcurrentRestSource([_pair()], interval_s=0.05, client=client)
    [paired] = asyncio.run(_first_cycle(source, [_pair()]))
    # Kalshi completed ~30ms after Polymarket; both fired simultaneously.
    assert 5.0 < paired.skew_ms < 90.0
    assert paired.recv_monotonic_ns == paired.kalshi.recv_monotonic_ns
    assert paired.kalshi.fetch_elapsed_ms >= 40.0
    assert paired.polymarket.fetch_elapsed_ms >= 10.0


def test_rows_schema_compatible(tmp_path):
    client = httpx.AsyncClient(transport=_transport())
    source = ConcurrentRestSource([_pair()], interval_s=0.05, client=client)
    [paired] = asyncio.run(_first_cycle(source, [_pair()]))

    sink = ObservationSink(tmp_path, run_id="test-run", ntp_offset_ms=1.5)
    k_row, p_row = sink.write_paired(paired)
    sink.close()

    # Column parity with the recorder's book_to_observation.
    reference = book_to_observation(
        OrderBook(
            venue="kalshi",
            market_id="KXTEST-26",
            yes_levels=(OrderBookLevel(price=0.41, size=5.0),),
            no_levels=(OrderBookLevel(price=0.57, size=8.0),),
            timestamp=datetime.now(timezone.utc),
        ),
        capture_seq=1,
        recv_monotonic_ns=time.monotonic_ns(),
        capture_ts_utc=datetime.now(timezone.utc),
        fetch_elapsed_ms=1.0,
        run_id="ref",
        ntp_offset_ms=1.5,
    )
    assert set(k_row) == set(reference)
    assert k_row["connector_source"] == "live_public"
    assert p_row["connector_source"] == "live_public"
    assert k_row["ntp_offset_ms"] == 1.5
    # Correct orientation: best bid below best ask on both venues.
    assert k_row["best_bid"] == 0.41 and round(k_row["best_ask"], 10) == 0.43
    assert p_row["best_bid"] == 0.40 and round(p_row["best_ask"], 10) == 0.44
    # Rows landed in the recorder's layout and parse back.
    files = list((tmp_path / "raw" / "book").rglob("*.jsonl"))
    assert len(files) == 2
    for f in files:
        for line in f.read_text().splitlines():
            assert json.loads(line)["run_id"] == "test-run"


def test_rtt_compensation_shrinks_skew():
    # Kalshi consistently 60ms slower; after the first cycle warms the EWMAs,
    # the kalshi leg is fired early and the measured skew collapses.
    client = httpx.AsyncClient(
        transport=_transport(kalshi_delay_s=0.070, poly_delay_s=0.010)
    )
    source = ConcurrentRestSource([_pair()], interval_s=0.01, client=client)
    first, later = asyncio.run(_first_cycle(source, [_pair()], n=2))
    assert first.skew_ms > 40.0  # uncompensated
    assert abs(later.skew_ms) < first.skew_ms / 2  # compensated


def test_one_venue_failure_skips_pair_not_cycle():
    pairs = [_pair(), _pair(key="pair-b", kalshi="KXBROKEN-26", token="tok-yes-b")]
    client = httpx.AsyncClient(transport=_transport(fail_kalshi_market="KXBROKEN-26"))
    source = ConcurrentRestSource(pairs, interval_s=0.05, client=client)
    snapshots = asyncio.run(_first_cycle(source, pairs, n=1))
    assert [s.pair_key for s in snapshots] == ["pair-a"]
