# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
"""Interactive credential onboarding for the public console entry point."""
#
# Prompts for credential fields and calls arbx.accounts.secrets.store_credentials
# to write them to ~/.arbx/credentials/<venue>.<profile>.yaml with mode 600.
# Validates venue against TEMPLATE_FIELDS; all other validation delegated to
# the store function. Live profiles require ARBX_ALLOW_LIVE_CREDS=1 to load
# (but staging is always allowed).
from __future__ import annotations

import argparse
import getpass
import stat
import sys

from arbx.accounts.kalshi_auth import KalshiAuthError, KalshiSigner
from arbx.accounts.secrets import (
    TEMPLATE_FIELDS,
    CredentialError,
    store_credentials,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Collect and store venue credentials securely."
    )
    ap.add_argument(
        "venue",
        help="credential venue (e.g., kalshi, polymarket)",
    )
    ap.add_argument(
        "profile",
        help="credential profile (paper or live)",
    )
    args = ap.parse_args()

    venue = args.venue
    profile = args.profile

    if venue not in TEMPLATE_FIELDS:
        print(f"unknown venue {venue!r}; valid venues: {sorted(TEMPLATE_FIELDS)}", file=sys.stderr)
        return 2

    fields_to_prompt = TEMPLATE_FIELDS[venue]
    fields: dict[str, str] = {}

    print(f"Storing credentials for {venue}.{profile}")
    for field in fields_to_prompt:
        value = getpass.getpass(f"{field}: ")
        fields[field] = value

    try:
        if venue == "kalshi":
            # Validate the path, permissions, PEM format, and key type before
            # persisting a credential reference. No network request is made.
            KalshiSigner(fields["api_key_id"], fields["private_key_pem_path"])
        path = store_credentials(venue, profile, fields)
    except (CredentialError, KalshiAuthError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    mode_bits = stat.S_IMODE(path.stat().st_mode)
    mode_str = oct(mode_bits)[2:]

    print(path)
    print(f"permissions: {mode_str}")
    print("Live-profile loading additionally requires: ARBX_ALLOW_LIVE_CREDS=1")

    return 0
