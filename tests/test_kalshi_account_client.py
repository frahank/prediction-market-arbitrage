# Scope: BOT_RUNTIME — KalshiAccountClient tests over mocked HTTP (no network).
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arbx.accounts.kalshi import KalshiAccountClient, KalshiAccountError
from arbx.accounts.kalshi_auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KalshiSigner,
)
from arbx.accounts.types import AccountClient
from arbx.venues.http import MockableHttpClient


@pytest.fixture()
def signer_pair(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "key.pem"
    pem_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pem_path.chmod(0o600)
    return key, KalshiSigner("acct-test-key", pem_path)


class _Recorder:
    """Response provider that records every request it serves."""

    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = responses  # endpoint path -> queue of payloads
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers):
        self.requests.append((url, dict(headers)))
        path = urlsplit(url).path.removeprefix("/trade-api/v2")
        queue = self.responses.get(path)
        if not queue:
            return (404, {"error": "unexpected endpoint"})
        return queue.pop(0)


def _client(recorder: _Recorder, signer: KalshiSigner) -> KalshiAccountClient:
    return KalshiAccountClient(
        signer, http=MockableHttpClient(response_provider=recorder)
    )


def test_balance_prefers_dollars_string(signer_pair):
    _, signer = signer_pair
    recorder = _Recorder({"/portfolio/balance": [{"balance": 123456, "balance_dollars": "1234.56"}]})
    assert asyncio.run(_client(recorder, signer).balance_usd()) == 1234.56


def test_balance_falls_back_to_cents(signer_pair):
    _, signer = signer_pair
    recorder = _Recorder({"/portfolio/balance": [{"balance": 250}]})
    assert asyncio.run(_client(recorder, signer).balance_usd()) == 2.50


def test_positions_follow_cursor_pagination(signer_pair):
    _, signer = signer_pair
    recorder = _Recorder(
        {
            "/portfolio/positions": [
                {"market_positions": [{"ticker": "AAA"}], "cursor": "page2"},
                {"market_positions": [{"ticker": "BBB"}], "cursor": ""},
            ]
        }
    )
    positions = asyncio.run(_client(recorder, signer).positions())
    assert [p["ticker"] for p in positions] == ["AAA", "BBB"]
    second_url = recorder.requests[1][0]
    assert parse_qs(urlsplit(second_url).query)["cursor"] == ["page2"]


def test_open_orders_requests_resting_status(signer_pair):
    _, signer = signer_pair
    recorder = _Recorder({"/portfolio/orders": [{"orders": [{"order_id": "o1", "status": "resting"}]}]})
    orders = asyncio.run(_client(recorder, signer).open_orders())
    assert orders == [{"order_id": "o1", "status": "resting"}]
    query = parse_qs(urlsplit(recorder.requests[0][0]).query)
    assert query["status"] == ["resting"]


def test_fills_pass_since_as_unix_seconds(signer_pair):
    _, signer = signer_pair
    recorder = _Recorder({"/portfolio/fills": [{"fills": [{"fill_id": "f1"}]}]})
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fills = asyncio.run(_client(recorder, signer).fills(since=since))
    assert fills == [{"fill_id": "f1"}]
    query = parse_qs(urlsplit(recorder.requests[0][0]).query)
    assert query["min_ts"] == [str(int(since.timestamp()))]


def test_auth_headers_applied_and_signed_without_query(signer_pair):
    key, signer = signer_pair
    recorder = _Recorder({"/portfolio/orders": [{"orders": []}]})
    asyncio.run(_client(recorder, signer).open_orders())
    _, headers = recorder.requests[0]
    assert headers[HEADER_KEY] == "acct-test-key"
    # The signature must cover the path WITHOUT the query string (docs rule).
    key.public_key().verify(
        base64.b64decode(headers[HEADER_SIGNATURE]),
        f"{headers[HEADER_TIMESTAMP]}GET/trade-api/v2/portfolio/orders".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_http_error_raises_without_payload_echo(signer_pair):
    _, signer = signer_pair
    recorder = _Recorder({"/portfolio/balance": [(401, {"error": "supersecret-detail"})]})
    recorder.responses["/portfolio/balance"] = [(401, {"error": "supersecret-detail"})]
    with pytest.raises(KalshiAccountError) as excinfo:
        asyncio.run(_client(recorder, signer).balance_usd())
    message = str(excinfo.value)
    assert "401" in message
    assert "supersecret-detail" not in message


def test_client_satisfies_account_client_protocol(signer_pair):
    _, signer = signer_pair
    client = _client(_Recorder({}), signer)
    assert isinstance(client, AccountClient)
