"""KalshiBookStream: snapshot+delta book building, seq gaps, backoff, cents conversion."""

import asyncio
import json

import pytest

from arbx.capture.kalshi_ws import KalshiBookStream, KalshiWsAuthError


class _FakeWs:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if not self._messages:
            raise ConnectionError("stream closed")
        return json.dumps(self._messages.pop(0))


class _FakeConnect:
    """connect(url, headers) factory serving scripted sessions in order."""

    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.connections = []

    def __call__(self, url, headers):
        factory = self

        class _Ctx:
            async def __aenter__(self_inner):
                if not factory._sessions:
                    raise asyncio.CancelledError  # script exhausted: stop the stream
                ws = _FakeWs(factory._sessions.pop(0))
                factory.connections.append(ws)
                return ws

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _snapshot_msg(seq=1, ticker="KXTEST-26"):
    return {
        "type": "orderbook_snapshot", "sid": 1, "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.4000", "10.00"], ["0.3900", "4.00"]],
            "no_dollars_fp": [["0.5700", "8.00"], ["0.5500", "2.00"]],
        },
    }


def _delta_msg(seq, *, side="yes", price="0.4100", delta="5.00", ticker="KXTEST-26"):
    return {
        "type": "orderbook_delta", "sid": 1, "seq": seq,
        "msg": {
            "market_ticker": ticker, "price_dollars": price,
            "delta_fp": delta, "side": side, "ts_ms": 1782828000000,
        },
    }


async def _no_sleep(_s):
    return None


def _run(stream, books):
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream.run(["KXTEST-26"], books.append))


def test_snapshot_then_delta_builds_book():
    connect = _FakeConnect([[_snapshot_msg(), _delta_msg(2)]])
    stream = KalshiBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    assert len(books) == 2
    first, second = books
    # Snapshot: YES bids best-first; asks are 1 − NO bids, cheapest first.
    assert first.bids == ((0.40, 10.0), (0.39, 4.0))
    assert first.asks == ((0.43, 8.0), (0.45, 2.0))
    assert first.connector_source == "streaming"
    # Delta added a new best YES bid at 0.41.
    assert second.bids[0] == (0.41, 5.0)
    assert second.venue_book_ts is not None
    # Subscribe command matched the documented shape.
    sub = connect.connections[0].sent[0]
    assert sub["cmd"] == "subscribe"
    assert sub["params"]["channels"] == ["orderbook_delta"]
    assert sub["params"]["market_tickers"] == ["KXTEST-26"]


def test_seq_gap_triggers_resubscribe():
    connect = _FakeConnect([
        [_snapshot_msg(seq=1), _delta_msg(3)],  # gap: 1 -> 3
        [_snapshot_msg(seq=7), _delta_msg(8)],  # fresh session, new numbering ok
    ])
    stream = KalshiBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    assert stream.seq_gaps == 1
    assert len(connect.connections) == 2, "gap must reconnect + resubscribe"
    assert connect.connections[1].sent[0]["cmd"] == "subscribe"
    # The gapped delta was NOT applied: after the gap only the fresh
    # snapshot+delta emitted (1 snapshot emit from session one).
    assert len(books) == 3


def test_reconnect_backoff_caps():
    delays = []

    async def record_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 9:
            raise asyncio.CancelledError

    class _AlwaysFail:
        def __call__(self, url, headers):
            raise ConnectionError("refused")

    stream = KalshiBookStream(connect=_AlwaysFail(), sleeper=record_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream.run(["KXTEST-26"], lambda b: None))

    assert delays[0] == 1.0
    assert delays == sorted(delays), "backoff must be non-decreasing"
    assert max(delays) == 60.0, "backoff must cap at 60s"


def test_cents_to_probability_conversion():
    cents_snapshot = {
        "type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {
            "market_ticker": "KXTEST-26",
            "yes": [[8, 100]],   # legacy cents-integer ladders
            "no": [[91, 50]],
        },
    }
    cents_delta = {
        "type": "orderbook_delta", "sid": 1, "seq": 2,
        "msg": {"market_ticker": "KXTEST-26", "price": 9, "delta": 25, "side": "yes"},
    }
    connect = _FakeConnect([[cents_snapshot, cents_delta]])
    stream = KalshiBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    first, second = books
    assert first.bids == ((0.08, 100.0),)
    assert first.asks == ((0.09, 50.0),)  # 1 − 0.91
    assert second.bids[0] == (0.09, 25.0)


def test_unauthenticated_default_fails_clearly():
    stream = KalshiBookStream()  # no connect override, no auth_headers
    with pytest.raises(KalshiWsAuthError, match="authenticated handshake"):
        asyncio.run(stream.run(["KXTEST-26"], lambda b: None))
