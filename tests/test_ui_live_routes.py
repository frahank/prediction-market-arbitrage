# Scope: TEST — M1-T4 Main/Live dashboard scaffold wiring.
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from arbx.accounts.secrets import LIVE_ENV_VAR
from arbx.exec.killswitch import KillSwitch
from arbx.services.live import LiveControllerImpl
from arbx.ui.app import ServiceRegistry, create_app


def _runtime_yaml(root: Path, sentinel: Path) -> Path:
    config_dir = root / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "runtime.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mode": "paper",
                "real_orders": 0,
                "enable_real_orders": False,
                "killswitch_path": str(sentinel),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "reports").mkdir(exist_ok=True)
    return path


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
    sentinel = tmp_path / "kill" / "KILL"
    repo_root = tmp_path / "repo"
    runtime_yaml = _runtime_yaml(repo_root, sentinel)
    switch = KillSwitch(sentinel)
    live = LiveControllerImpl(switch, runtime_yaml=runtime_yaml)
    app = create_app(ServiceRegistry(live_controller=live, killswitch=switch))
    return TestClient(app), sentinel


def test_no_mode_or_order_mutation_routes(tmp_path, monkeypatch):
    client, _sentinel = _client(tmp_path, monkeypatch)
    paths = {getattr(route, "path", "") for route in client.app.routes}

    for path in paths:
        lowered = path.lower()
        assert "mode" not in lowered
        assert "enable" not in lowered
        assert "order" not in lowered
        assert "place" not in lowered
        assert "trade" not in lowered


def test_killswitch_post_engages_and_no_unengage_route_exists(tmp_path, monkeypatch):
    client, sentinel = _client(tmp_path, monkeypatch)

    payload = client.post("/api/engage_killswitch", json={"reason": "ui live route test"}).json()

    assert payload["ok"] is True
    assert payload["data"]["engaged"] is True
    assert payload["data"]["reason"] == "ui live route test"
    assert sentinel.exists()

    status = client.get("/api/get_live_status").json()
    assert status["ok"] is True
    assert status["data"]["killswitch"]["engaged"] is True
    assert status["data"]["killswitch"]["reason"] == "ui live route test"

    for path in ("/api/disengage_killswitch", "/api/clear_killswitch", "/api/unengage_killswitch"):
        assert client.post(path, json={}).status_code == 404


def test_credentials_post_stores_and_response_has_no_values(tmp_path, monkeypatch):
    client, _sentinel = _client(tmp_path, monkeypatch)
    fields = {
        "api_key_id": "kid-ui-live-test-123",
        "private_key_pem_path": "/tmp/not-real-ui-live-test.pem",
    }

    payload = client.post(
        "/api/store_credentials",
        json={"venue": "kalshi", "profile": "paper", "fields": fields},
    ).json()

    assert payload["ok"] is True
    assert payload["data"] == {
        "venue": "kalshi",
        "profile": "paper",
        "present": True,
        "path": str(tmp_path / "home" / ".arbx" / "credentials" / "kalshi.paper.yaml"),
        "mode_600": True,
    }
    stored_path = Path(payload["data"]["path"])
    assert stored_path.is_file()
    assert (stored_path.stat().st_mode & 0o777) == 0o600

    serialized = json.dumps(payload)
    for value in fields.values():
        assert value not in serialized


def test_paper_banner_default(tmp_path, monkeypatch):
    client, _sentinel = _client(tmp_path, monkeypatch)

    page = client.get("/live")
    status = client.get("/api/get_live_status").json()

    assert page.status_code == 200
    assert "KILL SWITCH" in page.text
    assert "STORAGE ONLY - not connected to trading" in page.text
    assert status["ok"] is True
    assert status["data"]["mode"] == "paper"
    assert status["data"]["real_orders_enabled"] is False
    assert status["data"]["gates"] == {
        "config_live": False,
        "enable_flag": False,
        "env_flag": False,
    }
