# Authenticated Kalshi WebSocket handshake wiring.
"""ws_auth_headers → KalshiBookStream: the stream passes correctly shaped
signed headers to the connect factory. Keys are generated at runtime only.

The live-connection integration test is skipped unless the operator opts in
with ARBX_KALSHI_WS_INTEGRATION=1 (it needs real stored credentials)."""
from __future__ import annotations

import asyncio
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from arbx.accounts.kalshi_auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KalshiSigner,
    ws_auth_headers,
)
from arbx.capture.kalshi_ws import KalshiBookStream


def _signer(tmp_path) -> KalshiSigner:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "ws_key.pem"
    pem_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pem_path.chmod(0o600)
    return KalshiSigner("ws-test-key", pem_path)


class _HeaderCapture:
    """connect(url, headers) factory that records headers then stops the run."""

    def __init__(self):
        self.seen: list[dict[str, str]] = []

    def __call__(self, url, headers):
        self.seen.append(dict(headers))

        class _Ctx:
            async def __aenter__(self_inner):
                raise asyncio.CancelledError  # captured what we need; stop

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def test_book_stream_hands_signed_headers_to_connect(tmp_path):
    capture = _HeaderCapture()
    stream = KalshiBookStream(
        auth_headers=ws_auth_headers(_signer(tmp_path)),
        connect=capture,
        max_reconnects=0,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream.run(["KXTEST-26"], lambda _b: None))
    assert capture.seen, "connect was never called with handshake headers"
    headers = capture.seen[0]
    assert set(headers) == {HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP}
    assert headers[HEADER_KEY] == "ws-test-key"
    assert headers[HEADER_TIMESTAMP].isdigit()


@pytest.mark.skipif(
    os.environ.get("ARBX_KALSHI_WS_INTEGRATION") != "1",
    reason="live WS integration needs stored Kalshi credentials; opt in with "
    "ARBX_KALSHI_WS_INTEGRATION=1",
)
def test_live_authenticated_ws_connects():  # pragma: no cover - operator-run
    from arbx.accounts.kalshi_auth import KALSHI_WS_URL_DOCS, signer_from_credentials

    async def _probe() -> None:
        import websockets

        signer = signer_from_credentials("paper")
        async with websockets.connect(
            KALSHI_WS_URL_DOCS, additional_headers=ws_auth_headers(signer)()
        ):
            pass  # a completed handshake is the whole assertion

    asyncio.run(asyncio.wait_for(_probe(), timeout=15))
