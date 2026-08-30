# verify_access + Live-tab accounts payload (Task E backend).
"""Per-venue read-only connectivity: no creds → no network; probe result maps
to the connected boolean; the live status payload carries it; failures never
leak values. Keys are generated at runtime only."""
from __future__ import annotations

from pathlib import Path

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from arbx.accounts.verify import verify_access
from arbx.exec.killswitch import KillSwitch
from arbx.services.live import LiveControllerImpl
from arbx.venues.http import MockableHttpClient


def _isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ARBX_ALLOW_LIVE_CREDS", raising=False)
    return home


def _store_kalshi_paper_creds(home: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = home / "kalshi_key.pem"
    pem_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pem_path.chmod(0o600)
    cred_dir = home / ".arbx" / "credentials"
    cred_dir.mkdir(parents=True)
    cred_file = cred_dir / "kalshi.paper.yaml"
    cred_file.write_text(
        yaml.safe_dump(
            {"api_key_id": "verify-test-key", "private_key_pem_path": str(pem_path)}
        ),
        encoding="utf-8",
    )
    cred_file.chmod(0o600)


class _CountingProvider:
    def __init__(self, status: int):
        self.status = status
        self.calls = 0

    def __call__(self, url, headers):
        self.calls += 1
        assert "KALSHI-ACCESS-KEY" in headers  # the probe must be authenticated
        return (self.status, {"balance": 100, "balance_dollars": "1.00"})


def test_no_credentials_short_circuits_without_network(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    provider = _CountingProvider(200)
    report = verify_access(MockableHttpClient(response_provider=provider))
    assert provider.calls == 0
    assert report["kalshi"]["connected"] is False
    assert report["kalshi"]["detail"] == "no stored credentials"
    assert report["polymarket"]["connected"] is False
    assert report["polymarket"]["read_only"] is True


def test_stored_paper_creds_probe_balance_and_connect(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    _store_kalshi_paper_creds(home)
    provider = _CountingProvider(200)
    report = verify_access(MockableHttpClient(response_provider=provider))
    assert provider.calls == 1
    assert report["kalshi"] == {
        "connected": True,
        "read_only": True,
        "profile": "paper",
        "detail": "paper credentials verified via GET /portfolio/balance",
    }


def test_failed_probe_reports_status_only(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    _store_kalshi_paper_creds(home)
    report = verify_access(MockableHttpClient(response_provider=_CountingProvider(401)))
    kalshi = report["kalshi"]
    assert kalshi["connected"] is False
    assert "401" in kalshi["detail"]
    assert "verify-test-key" not in kalshi["detail"]


def test_live_status_carries_accounts_and_caches_probe(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    sentinel = tmp_path / "kill" / "KILL"
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "mode": "paper",
                "real_orders": 0,
                "enable_real_orders": False,
                "killswitch_path": str(sentinel),
            }
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def prober():
        calls["n"] += 1
        return {
            "kalshi": {"connected": True, "read_only": True, "profile": "paper", "detail": "ok"},
            "polymarket": {"connected": False, "read_only": True, "profile": None, "detail": "not built"},
        }

    controller = LiveControllerImpl(
        KillSwitch(sentinel),
        runtime_yaml=runtime,
        access_prober=prober,
    )
    first = controller.get_live_status()
    second = controller.get_live_status()
    assert first["accounts"]["kalshi"]["connected"] is True
    assert first["accounts"]["kalshi"]["read_only"] is True
    assert second["accounts"] == first["accounts"]
    assert calls["n"] == 1  # TTL cache: UI polling must not re-probe every tick
    # Paper posture is untouched by a connected read-only account.
    assert first["mode"] == "paper"
    assert first["real_orders_enabled"] is False


def test_crashing_prober_fails_closed(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    sentinel = tmp_path / "kill" / "KILL"
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("mode: paper\nreal_orders: 0\nenable_real_orders: false\n", encoding="utf-8")

    def prober():
        raise RuntimeError("probe exploded")

    controller = LiveControllerImpl(
        KillSwitch(sentinel),
        runtime_yaml=runtime,
        access_prober=prober,
    )
    accounts = controller.get_live_status()["accounts"]
    assert accounts["kalshi"]["connected"] is False
    assert accounts["polymarket"]["connected"] is False
