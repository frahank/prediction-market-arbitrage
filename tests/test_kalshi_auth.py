# Scope: BOT_RUNTIME — Kalshi RSA-PSS signer tests (keys generated at runtime only).
"""Signer correctness against a locally generated keypair.

No key material is ever committed: every test generates its RSA key with
``cryptography`` at runtime and writes it only under ``tmp_path``.
"""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from arbx.accounts.kalshi_auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KALSHI_WS_SIGNING_PATH,
    KalshiAuthError,
    KalshiSigner,
    ws_auth_headers,
)

API_KEY_ID = "test-key-id-0000"


def _write_pem(tmp_path, key=None, mode=0o600):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "kalshi_test_key.pem"
    pem_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pem_path.chmod(mode)
    return key, pem_path


def _verify(public_key, signature_b64: str, message: str) -> None:
    """Raises InvalidSignature if the header signature is wrong."""
    public_key.verify(
        base64.b64decode(signature_b64),
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_signature_verifies_against_generated_keypair(tmp_path):
    key, pem_path = _write_pem(tmp_path)
    signer = KalshiSigner(API_KEY_ID, pem_path)
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=1234567890000)
    _verify(
        key.public_key(),
        headers[HEADER_SIGNATURE],
        "1234567890000GET/trade-api/v2/portfolio/balance",
    )


def test_header_names_and_timestamp_format(tmp_path):
    _, pem_path = _write_pem(tmp_path)
    signer = KalshiSigner(API_KEY_ID, pem_path)
    headers = signer.headers("get", "/trade-api/v2/portfolio/balance")
    assert set(headers) == {HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP}
    assert headers[HEADER_KEY] == API_KEY_ID
    # Millisecond unix timestamp: 13 digits through the year 2286.
    assert headers[HEADER_TIMESTAMP].isdigit() and len(headers[HEADER_TIMESTAMP]) == 13
    base64.b64decode(headers[HEADER_SIGNATURE], validate=True)  # well-formed base64


def test_query_string_is_stripped_before_signing(tmp_path):
    key, pem_path = _write_pem(tmp_path)
    signer = KalshiSigner(API_KEY_ID, pem_path)
    headers = signer.headers(
        "GET", "/trade-api/v2/portfolio/orders?status=resting&limit=5", timestamp_ms=1000
    )
    _verify(key.public_key(), headers[HEADER_SIGNATURE], "1000GET/trade-api/v2/portfolio/orders")


def test_method_is_uppercased_in_message(tmp_path):
    key, pem_path = _write_pem(tmp_path)
    signer = KalshiSigner(API_KEY_ID, pem_path)
    headers = signer.headers("get", "/trade-api/ws/v2", timestamp_ms=42)
    _verify(key.public_key(), headers[HEADER_SIGNATURE], "42GET/trade-api/ws/v2")


def test_missing_pem_raises_safe_error(tmp_path):
    with pytest.raises(KalshiAuthError, match="not found"):
        KalshiSigner(API_KEY_ID, tmp_path / "nope.pem")


def test_loose_pem_permissions_refused(tmp_path):
    _, pem_path = _write_pem(tmp_path, mode=0o644)
    with pytest.raises(KalshiAuthError, match="permissions"):
        KalshiSigner(API_KEY_ID, pem_path)


def test_symlinked_pem_refused(tmp_path):
    _, pem_path = _write_pem(tmp_path)
    link = tmp_path / "link.pem"
    link.symlink_to(pem_path)
    with pytest.raises(KalshiAuthError, match="symlink"):
        KalshiSigner(API_KEY_ID, link)


def test_garbage_pem_error_contains_no_content(tmp_path):
    pem_path = tmp_path / "garbage.pem"
    pem_path.write_text("sekret-garbage-content-xyz")
    pem_path.chmod(0o600)
    with pytest.raises(KalshiAuthError) as excinfo:
        KalshiSigner(API_KEY_ID, pem_path)
    assert "sekret-garbage-content-xyz" not in str(excinfo.value)


def test_non_rsa_key_refused(tmp_path):
    _, pem_path = _write_pem(tmp_path, key=ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(KalshiAuthError, match="RSA"):
        KalshiSigner(API_KEY_ID, pem_path)


def test_empty_api_key_id_refused(tmp_path):
    _, pem_path = _write_pem(tmp_path)
    with pytest.raises(KalshiAuthError, match="api_key_id"):
        KalshiSigner("  ", pem_path)


def test_relative_signing_path_refused(tmp_path):
    _, pem_path = _write_pem(tmp_path)
    signer = KalshiSigner(API_KEY_ID, pem_path)
    with pytest.raises(KalshiAuthError, match="start with"):
        signer.headers("GET", "portfolio/balance")


def test_ws_auth_headers_shape_and_signature(tmp_path):
    key, pem_path = _write_pem(tmp_path)
    builder = ws_auth_headers(KalshiSigner(API_KEY_ID, pem_path))
    headers = builder()
    assert set(headers) == {HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP}
    _verify(
        key.public_key(),
        headers[HEADER_SIGNATURE],
        f"{headers[HEADER_TIMESTAMP]}GET{KALSHI_WS_SIGNING_PATH}",
    )
    # Fresh headers per attempt: a later call must not reuse a stale signature.
    assert builder()[HEADER_SIGNATURE] != headers[HEADER_SIGNATURE]
