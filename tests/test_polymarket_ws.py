"""PolymarketBookStream: book/price_change handling, reconnect, token orientation."""

import asyncio
import json

import pytest

from arbx.capture.polymarket_ws import PolymarketBookStream

TOKEN = "1075058827677314893583499125139453995603934829696"


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
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.connections = []

    def __call__(self, url):
        factory = self

        class _Ctx:
            async def __aenter__(self_inner):
                if not factory._sessions:
                    raise asyncio.CancelledError
                ws = _FakeWs(factory._sessions.pop(0))
                factory.connections.append(ws)
                return ws

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _book_msg(token=TOKEN):
    return {
        "event_type": "book",
        "asset_id": token,
        "market": "0xcondition",
        "bids": [{"price": "0.06", "size": "50"}, {"price": "0.01", "size": "100"}],
        "asks": [{"price": "0.07", "size": "40"}, {"price": "0.99", "size": "100"}],
        "timestamp": "1782828000000",
        "hash": "0x0",
    }


def _price_change_msg(token=TOKEN, *, price="0.065", size="25", side="BUY"):
    return {
        "event_type": "price_change",
        "market": "0xcondition",
        "price_changes": [
            {"asset_id": token, "price": price, "size": size, "side": side,
             "best_bid": price, "best_ask": "0.07"},
        ],
        "timestamp": "1782828001000",
    }


async def _no_sleep(_s):
    return None


def _run(stream, books, tokens=(TOKEN,)):
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream.run(list(tokens), books.append))


def test_book_message_builds_snapshot():
    connect = _FakeConnect([[_book_msg()]])
    stream = PolymarketBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    [snap] = books
    assert snap.venue == "polymarket"
    assert snap.bids == ((0.06, 50.0), (0.01, 100.0))  # best (highest bid) first
    assert snap.asks == ((0.07, 40.0), (0.99, 100.0))  # cheapest ask first
    assert snap.connector_source == "streaming"
    assert snap.venue_book_ts is not None and snap.venue_book_ts.year == 2026
    # Documented subscribe shape.
    assert connect.connections[0].sent[0] == {"assets_ids": [TOKEN], "type": "market"}


def test_price_change_updates_level():
    connect = _FakeConnect([[
        _book_msg(),
        _price_change_msg(price="0.065", size="25", side="BUY"),   # new best bid
        _price_change_msg(price="0.07", size="0", side="SELL"),    # removes best ask
    ]])
    stream = PolymarketBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    assert len(books) == 3
    assert books[1].bids[0] == (0.065, 25.0)
    assert books[2].asks == ((0.99, 100.0),), "size 0 must remove the level"


def test_reconnect_on_close():
    connect = _FakeConnect([[_book_msg()], [_book_msg()]])
    stream = PolymarketBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    assert len(connect.connections) == 2, "closed stream must reconnect"
    assert connect.connections[1].sent[0]["type"] == "market", "must resubscribe"
    assert stream.reconnects >= 1
    assert len(books) == 2


def test_token_orientation_matches_registry():
    # Subscribed by the YES token id; snapshots carry that token as market_id,
    # and events for unsubscribed tokens are ignored.
    other = "9999"
    connect = _FakeConnect([[_book_msg(), _book_msg(token=other),
                             _price_change_msg(token=other)]])
    stream = PolymarketBookStream(connect=connect, sleeper=_no_sleep)
    books = []
    _run(stream, books)

    assert [b.market_id for b in books] == [TOKEN]
    assert connect.connections[0].sent[0]["assets_ids"] == [TOKEN]
