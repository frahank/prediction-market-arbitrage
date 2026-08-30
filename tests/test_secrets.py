# Credential store (storage/load/redaction only).
"""Adversarial coverage for ``arbx.accounts.secrets``.

Everything runs against a tmp HOME — the real ``~/.arbx`` is never touched.
The repo-scan test is part of the safety net: it fails if credential material
(PEM private keys, bare 64-hex keys, or populated credential fields) ever
lands anywhere in the tree.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from arbx.accounts.secrets import (
    LIVE_ENV_VAR,
    PROFILES,
    TEMPLATE_FIELDS,
    CredentialError,
    credential_status,
    load_credentials,
    store_credentials,
)
from arbx.core.redact import redact_text

REPO_ROOT = Path(__file__).resolve().parents[1]

KALSHI_FIELDS = {
    "api_key_id": "kid-fake-roundtrip-1234",
    "private_key_pem_path": "/tmp/fake/kalshi_signing_key.pem",
}
POLYMARKET_FIELDS = {
    "wallet_private_key": "wpk-fake-value-5678",
    "api_key": "pk-fake-api-key-9012",
    "api_secret": "ps-fake-api-secret-3456",
    "api_passphrase": "pp-fake-passphrase-7890",
    "funder_address": "fa-fake-funder-2345",
}


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
    return tmp_path


def test_store_sets_600_and_roundtrips(tmp_home):
    path = store_credentials("kalshi", "paper", dict(KALSHI_FIELDS))

    assert path == tmp_home / ".arbx" / "credentials" / "kalshi.paper.yaml"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    # Parent dirs are private too, and no temp litter remains.
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert [entry.name for entry in path.parent.iterdir()] == ["kalshi.paper.yaml"]

    assert load_credentials("kalshi", "paper") == KALSHI_FIELDS

    # Field validation is strict: missing and unknown keys both refuse.
    with pytest.raises(CredentialError):
        store_credentials("kalshi", "paper", {"api_key_id": "kid-only-1234"})
    with pytest.raises(CredentialError):
        store_credentials("kalshi", "paper", {**KALSHI_FIELDS, "extra": "nope-1234"})
    with pytest.raises(CredentialError):
        store_credentials("exchange", "paper", dict(KALSHI_FIELDS))
    with pytest.raises(CredentialError):
        store_credentials("kalshi", "prod", dict(KALSHI_FIELDS))


def test_loose_permissions_refused(tmp_home):
    path = store_credentials("polymarket", "paper", dict(POLYMARKET_FIELDS))

    for loose_mode in (0o644, 0o640, 0o660, 0o606):
        os.chmod(path, loose_mode)
        with pytest.raises(CredentialError) as excinfo:
            load_credentials("polymarket", "paper")
        assert "600" in str(excinfo.value)
        for value in POLYMARKET_FIELDS.values():
            assert value not in str(excinfo.value)

    os.chmod(path, 0o600)
    assert load_credentials("polymarket", "paper") == POLYMARKET_FIELDS

    # 400 (stricter than 600) is fine; a symlinked credential file is not.
    os.chmod(path, 0o400)
    assert load_credentials("polymarket", "paper") == POLYMARKET_FIELDS
    os.chmod(path, 0o600)
    link = path.parent / "kalshi.paper.yaml"
    link.symlink_to(path)
    with pytest.raises(CredentialError):
        load_credentials("kalshi", "paper")


def test_live_profile_gated_by_env(tmp_home, monkeypatch):
    # Staging live keys is allowed without the gate…
    path = store_credentials("kalshi", "live", dict(KALSHI_FIELDS))
    assert path.is_file()

    # …but loading them is not.
    with pytest.raises(CredentialError) as excinfo:
        load_credentials("kalshi", "live")
    assert LIVE_ENV_VAR in str(excinfo.value)

    monkeypatch.setenv(LIVE_ENV_VAR, "0")
    with pytest.raises(CredentialError):
        load_credentials("kalshi", "live")

    monkeypatch.setenv(LIVE_ENV_VAR, "1")
    assert load_credentials("kalshi", "live") == KALSHI_FIELDS

    # The gate check runs before any file I/O: a missing live file still
    # reports the env gate first.
    monkeypatch.delenv(LIVE_ENV_VAR)
    with pytest.raises(CredentialError) as excinfo:
        load_credentials("polymarket", "live")
    assert LIVE_ENV_VAR in str(excinfo.value)


def test_values_registered_for_redaction(tmp_home, monkeypatch):
    stored = {
        "api_key_id": "kid-redaction-canary-4242",
        "private_key_pem_path": "/tmp/fake/redaction_canary.pem",
    }
    store_credentials("kalshi", "paper", stored)
    for value in stored.values():
        assert value not in redact_text(f"leaked into a log line: {value} <-")

    # Values loaded from a file someone placed manually are registered too.
    manual = tmp_home / ".arbx" / "credentials" / "polymarket.paper.yaml"
    manual_fields = dict(POLYMARKET_FIELDS)
    manual_fields["wallet_private_key"] = "wpk-manual-canary-9999"
    manual.write_text(yaml.safe_dump(manual_fields), encoding="utf-8")
    os.chmod(manual, 0o600)
    load_credentials("polymarket", "paper")
    assert "wpk-manual-canary-9999" not in redact_text("dump: wpk-manual-canary-9999")


def test_repo_contains_no_credentials():
    pem_header = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
    bare_hex_key = re.compile(r"0x[0-9a-fA-F]{64}")
    # Public Polymarket identifiers share the 0x-64-hex shape; exempt lines
    # that name them. A leaked private key line would not carry these names
    # (and the field-name rule below catches it regardless of value shape).
    public_identifier = re.compile(
        r"condition_?id|question_?id|neg_?risk|market|token|pair|edge on ", re.IGNORECASE
    )

    credential_field = re.compile(
        r"^\s*\"?(wallet_private_key|api_secret|api_passphrase|"
        r"private_key_pem_path|api_key_id|api_key)\"?\s*:\s*(.*)$"
    )
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
    # Generated output is not part of the repository, and .gitignore already
    # keeps all of it untracked. A local scan writes Polymarket CLOB token ids
    # into evidence/ and reports/; those share the 0x-64-hex shape of key
    # material, so scanning them turned a successful capture into a failing
    # test suite. This gate is about what the tree contains, not what a run
    # produced in it.
    generated_dirs = {"evidence", "reports", "dist", "build", "htmlcov", "site"}

    violations: list[str] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        root_path = Path(root)
        if root_path == REPO_ROOT:
            dirs[:] = [
                d for d in dirs
                if d not in skip_dirs
                and d not in generated_dirs
                and not d.startswith(("data", "logs"))
            ]
        else:
            dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            # Root-level soak data/log artifacts hold public pair keys whose
            # 0x condition-id halves share the private-key hex shape.
            if root_path == REPO_ROOT and name.startswith(("data", "logs")):
                continue
            path = root_path / name
            try:
                if path.stat().st_size > 5_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pem_header.search(line):
                    violations.append(f"{rel}:{lineno}: PEM private-key header")
                if bare_hex_key.search(line) and not public_identifier.search(line):
                    violations.append(f"{rel}:{lineno}: bare 64-hex key material")
                if path.suffix in {".yaml", ".yml", ".json"}:
                    match = credential_field.match(line)
                    if match:
                        value = match.group(2).strip().strip("\",'")
                        if value and not value.startswith(("REPLACE_WITH_", "null", "~", "#")):
                            violations.append(
                                f"{rel}:{lineno}: populated credential field "
                                f"{match.group(1)!r}"
                            )
    assert not violations, "credential material found in the repo:\n" + "\n".join(violations)

    # The committed templates stay keys-only and in sync with the contract.
    for venue, keys in TEMPLATE_FIELDS.items():
        for profile in PROFILES:
            template = REPO_ROOT / "docs" / "credentials_templates" / f"{venue}.{profile}.yaml"
            assert template.is_file(), f"missing credential template {template.name}"
            data = yaml.safe_load(template.read_text(encoding="utf-8"))
            assert data == {key: "" for key in keys}, f"{template.name} must be keys-only"


def test_status_never_returns_values(tmp_home):
    store_credentials("kalshi", "paper", dict(KALSHI_FIELDS))
    store_credentials("polymarket", "live", dict(POLYMARKET_FIELDS))

    rows = credential_status()

    assert len(rows) == 4  # 2 venues x 2 profiles, always enumerated
    assert {(row["venue"], row["profile"]) for row in rows} == {
        ("kalshi", "paper"),
        ("kalshi", "live"),
        ("polymarket", "paper"),
        ("polymarket", "live"),
    }
    for row in rows:
        assert set(row) == {"venue", "profile", "present", "path", "mode_600"}

    by_key = {(row["venue"], row["profile"]): row for row in rows}
    assert by_key[("kalshi", "paper")]["present"] is True
    assert by_key[("kalshi", "paper")]["mode_600"] is True
    assert by_key[("polymarket", "live")]["present"] is True
    assert by_key[("kalshi", "live")]["present"] is False
    assert by_key[("polymarket", "paper")]["present"] is False

    serialized = json.dumps(rows)
    for value in list(KALSHI_FIELDS.values()) + list(POLYMARKET_FIELDS.values()):
        assert value not in serialized

    # Loose permissions are reported honestly (present but not mode_600).
    path = Path(by_key[("kalshi", "paper")]["path"])
    os.chmod(path, 0o644)
    loose = {
        (row["venue"], row["profile"]): row for row in credential_status()
    }[("kalshi", "paper")]
    assert loose["present"] is True
    assert loose["mode_600"] is False
