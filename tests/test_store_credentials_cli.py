# CLI wrapper for store_credentials.
"""Coverage for scripts/store_credentials.py CLI.

The CLI prompts for credential fields and calls the store function.
All file handling, validation, and redaction are tested in test_secrets.py;
this test covers: prompt collection, field ordering, and the printed output.
"""
from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ARBX_ALLOW_LIVE_CREDS", raising=False)
    return tmp_path


@pytest.fixture()
def store_credentials_cli(tmp_path):
    """Load scripts/store_credentials.py as a module."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "store_credentials.py"
    spec = importlib.util.spec_from_file_location("store_credentials_cli", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_store_credentials_cli_success(
    tmp_home, store_credentials_cli, monkeypatch, capsys
):
    """CLI collects fields via getpass and calls store_credentials."""
    pem_path = tmp_home / "kalshi.pem"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pem_path.chmod(0o600)
    canned_responses = [
        "test-key-id-abc123",
        str(pem_path),
    ]
    response_iter = iter(canned_responses)

    def mock_getpass(prompt: str) -> str:
        return next(response_iter)

    monkeypatch.setattr("getpass.getpass", mock_getpass)

    # Invoke the CLI with sys.argv.
    monkeypatch.setattr("sys.argv", ["store_credentials.py", "kalshi", "paper"])
    result = store_credentials_cli.main()

    assert result == 0

    # Credential file exists and has correct permissions.
    cred_path = tmp_home / ".arbx" / "credentials" / "kalshi.paper.yaml"
    assert cred_path.is_file()
    mode_bits = stat.S_IMODE(cred_path.stat().st_mode)
    assert mode_bits == 0o600

    # Captured output includes the path but NOT the entered values.
    captured = capsys.readouterr()
    output = captured.out

    assert str(cred_path) in output
    assert "600" in output
    assert "ARBX_ALLOW_LIVE_CREDS=1" in output

    # Entered values must not appear anywhere in stdout.
    for canned in canned_responses:
        assert canned not in output


def test_store_credentials_cli_unknown_venue(tmp_home, store_credentials_cli, monkeypatch, capsys):
    """CLI exits with code 2 if venue is unknown."""
    monkeypatch.setattr("sys.argv", ["store_credentials.py", "unknown_exchange", "paper"])
    result = store_credentials_cli.main()

    assert result == 2
    captured = capsys.readouterr()
    assert "unknown venue" in captured.err
