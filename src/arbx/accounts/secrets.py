# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Storage-only credential store with redaction.
"""Credential storage, status, and loading — nothing else.

Files live at ``~/.arbx/credentials/<venue>.<profile>.yaml`` with mode 600,
outside the repo. Every stored or loaded value is registered with
``arbx.core.redact`` so it can never appear in logs, UI output, or error
messages. This module NEVER prints, logs, or embeds credential values in
exceptions.

Hard rules:
- loading refuses files with permissions looser than 600 (and symlinks);
- ``profile == "live"`` additionally requires ``ARBX_ALLOW_LIVE_CREDS == "1"``
  to *load* (staging live keys via ``store_credentials`` is allowed — they
  are unusable until the operator sets the env gate);
- status reports presence and permissions only, never values.

Venue clients are separate and read-only.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from arbx.core.redact import register_secret

CREDENTIALS_DIR = "~/.arbx/credentials"
LIVE_ENV_VAR = "ARBX_ALLOW_LIVE_CREDS"

PROFILES = ("paper", "live")

# Template keys per venue — mirrored keys-only in docs/credentials_templates/.
TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "kalshi": (
        "api_key_id",
        "private_key_pem_path",
    ),
    "polymarket": (
        "wallet_private_key",
        "api_key",
        "api_secret",
        "api_passphrase",
        "funder_address",
    ),
}


class CredentialError(RuntimeError):
    """Credential-store failure with a safe message (never contains values)."""


def credentials_dir() -> Path:
    return Path(CREDENTIALS_DIR).expanduser()


def credential_path(venue: str, profile: str) -> Path:
    _validate_venue_profile(venue, profile)
    return credentials_dir() / f"{venue}.{profile}.yaml"


def _validate_venue_profile(venue: str, profile: str) -> None:
    if venue not in TEMPLATE_FIELDS:
        raise CredentialError(f"unknown venue {venue!r}; expected one of {sorted(TEMPLATE_FIELDS)}")
    if profile not in PROFILES:
        raise CredentialError(f"unknown profile {profile!r}; expected one of {list(PROFILES)}")


def _validate_fields(venue: str, fields: dict[str, Any]) -> dict[str, str]:
    if not isinstance(fields, dict):
        raise CredentialError("fields must be a mapping of template keys to string values")
    expected = set(TEMPLATE_FIELDS[venue])
    provided = {str(key) for key in fields}
    missing = sorted(expected - provided)
    unknown = sorted(provided - expected)
    if missing:
        raise CredentialError(f"missing credential fields for {venue}: {missing}")
    if unknown:
        raise CredentialError(f"unknown credential fields for {venue}: {unknown}")
    validated: dict[str, str] = {}
    for key in TEMPLATE_FIELDS[venue]:
        value = fields[key]
        if not isinstance(value, str) or not value.strip():
            raise CredentialError(f"credential field {key!r} must be a non-empty string")
        validated[key] = value
    return validated


def store_credentials(venue: str, profile: str, fields: dict[str, str]) -> Path:
    """Write ``~/.arbx/credentials/<venue>.<profile>.yaml`` atomically, mode 600."""
    _validate_venue_profile(venue, profile)
    validated = _validate_fields(venue, fields)
    for value in validated.values():
        register_secret(value)

    path = credential_path(venue, profile)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    os.chmod(directory.parent, 0o700)

    # mkstemp creates the temp file 600, so the content is never readable by
    # anyone else at any point; os.replace makes the final write atomic.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{venue}.{profile}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(validated, handle, default_flow_style=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def credential_status() -> list[dict[str, Any]]:
    """Presence/permission report for every known venue+profile — never values."""
    rows: list[dict[str, Any]] = []
    for venue in sorted(TEMPLATE_FIELDS):
        for profile in PROFILES:
            path = credential_path(venue, profile)
            try:
                info = path.lstat()
                present = stat.S_ISREG(info.st_mode)
                mode_600 = present and stat.S_IMODE(info.st_mode) == 0o600
            except OSError:
                present = False
                mode_600 = False
            rows.append(
                {
                    "venue": venue,
                    "profile": profile,
                    "present": present,
                    "path": str(path),
                    "mode_600": mode_600,
                }
            )
    return rows


def load_credentials(venue: str, profile: str) -> dict[str, str]:
    """Load one credential file; enforce the live env gate and 600 perms."""
    _validate_venue_profile(venue, profile)
    if profile == "live" and os.environ.get(LIVE_ENV_VAR) != "1":
        raise CredentialError(
            f"loading live credentials requires {LIVE_ENV_VAR}=1 "
            "(storage is allowed without it; use of live keys is not)"
        )
    path = credential_path(venue, profile)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CredentialError(
            f"no credential file for {venue}.{profile}; expected {path} "
            f"(see docs/credentials_templates/{venue}.{profile}.yaml)"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise CredentialError(f"credential file {path} is a symlink; refusing to follow it")
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError(f"credential path {path} is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise CredentialError(
            f"credential file {path} has permissions "
            f"{stat.S_IMODE(info.st_mode):o}; refusing anything looser than 600 "
            f"(fix with: chmod 600 {path})"
        )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # Deliberately does not include the parser detail: it can quote the
        # malformed (secret) content back.
        raise CredentialError(f"credential file {path} is not valid YAML") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"credential file {path} must be a YAML mapping")
    loaded = {str(key): str(value) for key, value in data.items()}
    for value in loaded.values():
        register_secret(value)
    return loaded
