# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Lightweight read-only access verification per venue.
"""verify_access(): can we *read* account state right now? Nothing more.

One cheap authenticated ``GET /portfolio/balance`` proves a Kalshi credential
works; Polymarket has no read-only account client and always reports not
connected. Results carry only a boolean + a redaction-safe detail string —
never balances, never credential material. A "connected" result grants no
order capability: the only client involved is read-only by type
(``tests/test_accounts_readonly.py``).

No network is touched unless a usable stored credential exists.
"""
from __future__ import annotations

import os
from typing import Any

from arbx.accounts.kalshi import KalshiAccountError
from arbx.accounts.kalshi_auth import (
    KALSHI_REST_BASE,
    KalshiAuthError,
    signer_from_credentials,
)
from arbx.accounts.secrets import LIVE_ENV_VAR, CredentialError, credential_status
from arbx.core.redact import redact_text
from arbx.venues.http import MockableHttpClient, RetryConfig


def _not_connected(detail: str, profile: str | None = None) -> dict[str, Any]:
    return {"connected": False, "read_only": True, "profile": profile, "detail": detail}


def _usable_kalshi_profile() -> str | None:
    """Stored 600-perm profile the current process may load: live only behind
    its env gate, else paper."""
    stored = {
        row["profile"]
        for row in credential_status()
        if row["venue"] == "kalshi" and row["present"] and row["mode_600"]
    }
    if "live" in stored and os.environ.get(LIVE_ENV_VAR) == "1":
        return "live"
    if "paper" in stored:
        return "paper"
    return None


def _verify_kalshi(http: MockableHttpClient | None = None) -> dict[str, Any]:
    profile = _usable_kalshi_profile()
    if profile is None:
        stored_live = any(
            row["venue"] == "kalshi" and row["profile"] == "live" and row["present"]
            for row in credential_status()
        )
        if stored_live:
            return _not_connected(
                f"live credentials are stored but gated; set {LIVE_ENV_VAR}=1 to use them"
            )
        return _not_connected("no stored credentials")
    try:
        signer = signer_from_credentials(profile)
    except (CredentialError, KalshiAuthError) as exc:
        return _not_connected(redact_text(str(exc)), profile)

    client = http or MockableHttpClient(retry_config=RetryConfig(max_retries=0))
    result = client.get_json(
        f"{KALSHI_REST_BASE}/portfolio/balance",
        venue="kalshi",
        headers=signer.headers("GET", "/trade-api/v2/portfolio/balance"),
    )
    if 200 <= result.status_code < 300:
        return {
            "connected": True,
            "read_only": True,
            "profile": profile,
            "detail": f"{profile} credentials verified via GET /portfolio/balance",
        }
    return _not_connected(
        f"balance probe failed with status {result.status_code}", profile
    )


def verify_access(http: MockableHttpClient | None = None) -> dict[str, dict[str, Any]]:
    """Per-venue read-only connectivity: {venue: {connected, read_only, profile, detail}}."""
    try:
        kalshi = _verify_kalshi(http)
    except (KalshiAccountError, OSError) as exc:  # fail closed, never raise into the UI
        kalshi = _not_connected(redact_text(str(exc)))
    return {
        "kalshi": kalshi,
        "polymarket": _not_connected("read-only account client not implemented"),
    }
