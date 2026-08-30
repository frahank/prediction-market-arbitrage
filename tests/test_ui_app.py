# FastAPI shell tests.
from __future__ import annotations

from fastapi.testclient import TestClient
from scripts.run_ui import enforce_localhost, load_ui_config

from arbx.exec.killswitch import KillSwitch
from arbx.ui.app import ServiceRegistry, create_app, register_op
from arbx.ui.envelope import SCHEMA_VERSION


def _paths(app):
    return {getattr(route, "path", "") for route in app.routes}


def test_five_tabs_and_no_stray_routes():
    app = create_app(ServiceRegistry())
    paths = _paths(app)
    page_paths = {
        path
        for path in paths
        if path == "/" or (not path.startswith("/api/") and path != "/static")
    }
    assert page_paths == {
        "/",
        "/live",
        "/paper",
        "/pairs",
        "/data",
        "/docs-viewer",
        # Not tabs: a favicon answer and the read-only asset route that serves
        # images referenced by rendered documents.
        "/favicon.ico",
        "/doc-asset/{asset_path:path}",
    }
    for path in paths:
        assert "mode" not in path
        assert "enable" not in path
        assert "order" not in path
        assert "trade" not in path

    client = TestClient(app)
    response = client.get("/paper")
    assert response.status_code == 200
    html = response.text
    assert html.count("<nav") == 1
    for label in ("Access &amp; Safety", "Paper", "Pairs", "Data", "Documents"):
        assert f">{label}<" in html


def test_api_envelope_on_success_and_error():
    app = create_app(ServiceRegistry())

    def ok_handler():
        return {"answer": 42}

    def failing_handler():
        raise RuntimeError("secret provider traceback")

    register_op(app, "test_ok", ok_handler)
    register_op(app, "test_error", failing_handler)

    client = TestClient(app)
    ok = client.get("/api/test_ok").json()
    assert ok["ok"] is True
    assert ok["data"] == {"answer": 42}
    assert ok["error"] is None
    assert ok["meta"]["schema_version"] == SCHEMA_VERSION

    err = client.get("/api/test_error").json()
    assert err["ok"] is False
    assert err["data"] is None
    assert err["error"]["code"] == "internal_error"
    assert "traceback" not in err["error"]["message"].lower()
    assert "provider" not in err["error"]["message"].lower()


def test_app_status_reports_paper(tmp_path, monkeypatch):
    monkeypatch.delenv("ARBX_KILL", raising=False)
    registry = ServiceRegistry(killswitch=KillSwitch(tmp_path / "KILL"))
    client = TestClient(create_app(registry))
    payload = client.get("/api/get_app_status").json()
    assert payload["ok"] is True
    assert payload["data"] == {
        "mode": "paper",
        "real_orders_enabled": False,
        "killswitch_engaged": False,
        "killswitch_reason": None,
        "schema_version": SCHEMA_VERSION,
    }


def test_binds_localhost_only():
    config = load_ui_config()
    assert enforce_localhost(config["host"]) == "127.0.0.1"
    assert enforce_localhost("localhost") == "localhost"
    assert enforce_localhost("::1") == "::1"
    for host in ("0.0.0.0", "192.168.1.10", ""):
        try:
            enforce_localhost(host)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{host!r} should not be accepted")


def test_security_headers_on_pages_and_api():
    """The docs tab assigns server-rendered markdown to innerHTML; CSP is the
    backstop that keeps that safe independent of the renderer's settings."""
    client = TestClient(create_app(ServiceRegistry()))
    for path in ("/paper", "/api/get_app_status"):
        response = client.get(path)
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_security_headers_present_on_forbidden_response():
    client = TestClient(create_app(ServiceRegistry()))
    response = client.get("/api/get_app_status", headers={"host": "evil.example.com"})
    assert response.status_code == 403
    assert "content-security-policy" in response.headers


def test_unknown_query_parameters_are_rejected():
    """Request keys are splatted into the handler, so anything the handler does
    not declare must be refused rather than silently forwarded."""
    app = create_app(ServiceRegistry())

    def handler(name: str = "default") -> dict[str, str]:
        return {"name": name}

    register_op(app, "echo_name", handler)
    client = TestClient(app)

    ok = client.get("/api/echo_name", params={"name": "allowed"})
    assert ok.json()["ok"] is True
    assert ok.json()["data"] == {"name": "allowed"}

    rejected = client.get("/api/echo_name", params={"name": "x", "verify_sha256": "0"})
    body = rejected.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_request"
    assert "verify_sha256" in body["error"]["message"]


def test_declared_parameters_still_pass_through():
    client = TestClient(create_app(ServiceRegistry()))
    # list_soaks declares cursor/limit/edges_only; a declared name must not 403.
    response = client.get("/api/list_soaks", params={"limit": "5"})
    assert response.json()["ok"] in (True, False)
    if response.json()["ok"] is False:
        assert response.json()["error"]["code"] != "invalid_request" or (
            "unknown parameter" not in response.json()["error"]["message"]
        )
