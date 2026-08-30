# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Account credential surface.
"""Accounts package.

The public package exposes storage-only credential helpers. Authenticated
account clients remain read-only and are imported explicitly by their callers.
"""

from arbx.accounts.secrets import (
    CredentialError,
    credential_status,
    load_credentials,
    store_credentials,
)

__all__ = [
    "CredentialError",
    "credential_status",
    "load_credentials",
    "store_credentials",
]
