# Scope: BOT_RUNTIME — Kalshi RSA-PSS request signer (read-only auth track).
"""Signed-request headers for Kalshi's authenticated API. Auth only — no orders.

Verified against the official docs (2026-07-06, ``docs.kalshi.com``:
``getting_started/api_keys``, ``getting_started/quick_start_websockets``,
``api-reference/portfolio/*``):

- Message to sign: ``str(timestamp_ms) + METHOD + path`` where ``path`` is the
  request path *without query parameters* (e.g. sign
  ``/trade-api/v2/portfolio/fills`` for ``.../fills?limit=5``).
- Algorithm: RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length;
  signature is base64-encoded.
- Headers: ``KALSHI-ACCESS-KEY`` (api key id), ``KALSHI-ACCESS-SIGNATURE``,
  ``KALSHI-ACCESS-TIMESTAMP`` (the same millisecond timestamp that was signed).
- The WebSocket handshake signs ``timestamp + "GET" + "/trade-api/ws/v2"`` —
  "the same pattern as REST API requests" — so :func:`ws_auth_headers` builds
  fresh headers per connection attempt for ``KalshiBookStream(auth_headers=…)``.

The private key never leaves this module: the PEM text is registered with
``arbx.core.redact.register_secret`` at load, and no exception message ever
contains key material. Signing is generic over method+path because that is how
the venue authenticates *every* request; order capability lives (and is gated)
elsewhere — this repo's account clients are read-only by type
(``tests/test_accounts_readonly.py``).
"""
from __future__ import annotations

import base64
import stat
import time
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arbx.core.redact import register_secret

# Current official hosts (docs.kalshi.com, 2026-07-06). NOTE: the older
# ``api.elections.kalshi.com`` host still appears in some third-party writeups;
# the docs now consistently use the ``external-api`` hosts below.
KALSHI_REST_BASE = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_WS_URL_DOCS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_WS_SIGNING_PATH = "/trade-api/ws/v2"

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"


class KalshiAuthError(RuntimeError):
    """Signer failure with a safe message — never contains key material."""


def _load_private_key(pem_path: Path) -> rsa.RSAPrivateKey:
    try:
        info = pem_path.lstat()
    except OSError as exc:
        raise KalshiAuthError(f"private key PEM not found at {pem_path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise KalshiAuthError(f"private key PEM {pem_path} is a symlink; refusing to follow it")
    if not stat.S_ISREG(info.st_mode):
        raise KalshiAuthError(f"private key PEM path {pem_path} is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise KalshiAuthError(
            f"private key PEM {pem_path} has permissions "
            f"{stat.S_IMODE(info.st_mode):o}; refusing anything looser than 600 "
            f"(fix with: chmod 600 {pem_path})"
        )
    pem_text = pem_path.read_text(encoding="utf-8")
    register_secret(pem_text)
    try:
        key = serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        # Deliberately excludes parser detail — it can quote key bytes back.
        raise KalshiAuthError(
            f"private key PEM at {pem_path} is not a loadable unencrypted key "
            "(encrypted/passphrase PEMs are not supported)"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiAuthError(
            f"private key at {pem_path} is not an RSA key; Kalshi API keys are RSA"
        )
    return key


class KalshiSigner:
    """Builds the three KALSHI-ACCESS-* headers for one api_key_id + RSA key."""

    def __init__(self, api_key_id: str, private_key_pem_path: str | Path) -> None:
        if not isinstance(api_key_id, str) or not api_key_id.strip():
            raise KalshiAuthError("api_key_id must be a non-empty string")
        register_secret(api_key_id)
        self._api_key_id = api_key_id.strip()
        self._private_key = _load_private_key(Path(private_key_pem_path).expanduser())

    def headers(
        self,
        method: str,
        path: str,
        *,
        timestamp_ms: int | None = None,
    ) -> dict[str, str]:
        """Signed headers for ``METHOD path``. ``path`` may carry a query
        string; per the docs it is stripped before signing."""
        if not path.startswith("/"):
            raise KalshiAuthError(f"signing path must start with '/': {path!r}")
        signing_path = path.split("?", 1)[0]
        ts = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
        message = f"{ts}{method.upper()}{signing_path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            HEADER_KEY: self._api_key_id,
            HEADER_SIGNATURE: base64.b64encode(signature).decode("ascii"),
            HEADER_TIMESTAMP: str(ts),
        }


def signer_from_credentials(profile: str = "paper") -> KalshiSigner:
    """Signer from the stored ``~/.arbx/credentials/kalshi.<profile>.yaml``.

    Inherits every gate in ``arbx.accounts.secrets.load_credentials`` (600
    perms, live profile requires ``ARBX_ALLOW_LIVE_CREDS=1``).
    """
    from arbx.accounts.secrets import load_credentials

    creds = load_credentials("kalshi", profile)
    return KalshiSigner(creds["api_key_id"], creds["private_key_pem_path"])


def ws_auth_headers(
    signer: KalshiSigner,
    path: str = KALSHI_WS_SIGNING_PATH,
) -> Callable[[], dict[str, str]]:
    """Build the ``auth_headers`` callable that ``KalshiBookStream`` expects.

    Returns a builder that signs ``timestamp + "GET" + path`` freshly on every
    call, so each reconnect attempt gets a current timestamp.
    """

    def build() -> dict[str, str]:
        return signer.headers("GET", path)

    return build
