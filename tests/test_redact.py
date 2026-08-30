# Value-targeted redaction (labels stay readable, secrets do not).
"""Regression coverage for ``arbx.core.redact``.

The bug these guard against: matching a sensitive *word* and blanking that word
leaves the adjacent secret in place, producing output that looks sanitized and
is not. Every case below asserts on the VALUE being gone, never on the label.
"""
from __future__ import annotations

import pytest

from arbx.core.redact import REDACTED, redact_jsonable, redact_text, register_secret

# Assembled from fragments so that no single line in this file matches the
# PEM header pattern that tests/test_secrets.py scans the repo for. That scan is
# the safety net against real key material landing in the tree; this fake must
# not desensitize it.
_BEGIN = "-----BEGIN RSA PRIVATE" + " KEY-----"
_END = "-----END RSA PRIVATE" + " KEY-----"
FAKE_PEM = f"{_BEGIN}\nMIIEpAIBAAKCAQEA0123456789abcdefFAKEKEYMATERIAL\n{_END}"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJlLWZha2U"
FAKE_SIG = "aGVsbG8rd29ybGQvaGVsbG8rd29ybGQvYWJjZGVmZ2hpams="


@pytest.mark.parametrize(
    "text, secret",
    [
        (FAKE_PEM, "MIIEpAIBAAKCAQEA0123456789abcdefFAKEKEYMATERIAL"),
        (f"Authorization: Bearer {FAKE_JWT}", FAKE_JWT),
        (f"KALSHI-ACCESS-SIGNATURE: {FAKE_SIG}", FAKE_SIG),
        (f"KALSHI-ACCESS-KEY: {FAKE_SIG}", FAKE_SIG),
        ("cookie: session=deadbeefcafedeadbeefcafe0123456789", "deadbeefcafe"),
        ("set-cookie: sid=abc123def456ghi789jkl", "abc123def456ghi789jkl"),
        ("api_secret: s3cr3t-abcdef123456", "s3cr3t-abcdef123456"),
        ('"api_key_id": "kid-abcdef-123456"', "kid-abcdef-123456"),
        ("wallet_private_key=0xdeadbeefcafe1234567890", "0xdeadbeefcafe1234567890"),
        ("passphrase = hunter2-hunter2-hunter2", "hunter2-hunter2-hunter2"),
        (f"raw signature blob {FAKE_SIG}", FAKE_SIG),
    ],
)
def test_secret_values_are_removed(text: str, secret: str) -> None:
    assert secret not in redact_text(text)


def test_pem_block_is_replaced_wholesale() -> None:
    assert redact_text(FAKE_PEM) == REDACTED


def test_label_stays_readable() -> None:
    """Redaction must remain debuggable: the key survives, the value does not."""
    assert redact_text("api_secret: s3cr3t-abcdef123456") == f"api_secret: {REDACTED}"
    assert redact_text(f"Authorization: Bearer {FAKE_JWT}") == (
        f"Authorization: Bearer {REDACTED}"
    )


def test_redaction_is_idempotent() -> None:
    once = redact_text(f"KALSHI-ACCESS-SIGNATURE: {FAKE_SIG}")
    assert redact_text(once) == once
    assert "]]" not in once


def test_mapping_key_redacts_its_value() -> None:
    payload = {
        "api_secret": "s3cr3t-abcdef123456",
        "authorization": "Bearer abc123xyz789",
        "nested": {"wallet_private_key": "0xdeadbeefcafe1234"},
        "legs": [{"api_passphrase": "hunter2-hunter2"}],
    }
    out = redact_jsonable(payload)
    assert out["api_secret"] == REDACTED
    assert out["authorization"] == REDACTED
    assert out["nested"]["wallet_private_key"] == REDACTED
    assert out["legs"][0]["api_passphrase"] == REDACTED
    # Keys are preserved so redacted structures stay diagnosable.
    assert set(payload) == set(out)


def test_registered_values_are_removed_anywhere() -> None:
    register_secret("kid-registered-canary-4242")
    assert "kid-registered-canary-4242" not in redact_text(
        "prefix kid-registered-canary-4242 suffix"
    )


def test_short_values_are_not_registered() -> None:
    """A 4-char registration would blank that substring out of unrelated text."""
    register_secret("edge")
    assert "edge" in redact_text("the edge survived the fee model")


PUBLIC_IDENTIFIERS = [
    "condition_id: 0xcb239105ed21a2420ba4d85090b9bc32755c56601ffdc528afd17fd6282fe930",
    "yes_token_id: '35165603539035270209469650915267170915291414186795231160733227083640059691764'",
    "kalshi_market_id: KXPRESNOMD-28-GR",
]


@pytest.mark.parametrize("line", PUBLIC_IDENTIFIERS)
def test_public_market_identifiers_survive(line: str) -> None:
    """Over-redaction has a cost: these are public ids the capture logs need."""
    assert redact_text(line) == line


def test_ordinary_prose_is_untouched() -> None:
    prose = "The signature scheme uses RSA-PSS and a token bucket rate limiter."
    assert redact_text(prose) == prose


def test_non_string_input_passes_through() -> None:
    assert redact_text("") == ""
    assert redact_jsonable(42) == 42
    assert redact_jsonable(None) is None
