# Service seam registration tests.
from __future__ import annotations

from fastapi.testclient import TestClient

from arbx.services.contracts import iter_seam_operations
from arbx.ui.app import ServiceRegistry, create_app


def _route_methods(app):
    return {
        getattr(route, "path", ""): set(getattr(route, "methods", set()))
        for route in app.routes
    }


def test_every_seam_operation_registered():
    app = create_app(ServiceRegistry())
    routes = _route_methods(app)
    for op in iter_seam_operations():
        path = f"/api/{op.name}"
        assert path in routes
        assert op.method in routes[path]


def test_stub_returns_not_implemented_envelope():
    client = TestClient(create_app(ServiceRegistry()))

    read_payload = client.get("/api/list_docs").json()
    assert read_payload["ok"] is False
    assert read_payload["data"] is None
    assert read_payload["error"]["code"] == "not_implemented"
    assert "traceback" not in read_payload["error"]["message"].lower()

    empty_body_payload = client.post("/api/run_test_suite").json()
    assert empty_body_payload["ok"] is False
    assert empty_body_payload["data"] is None
    assert empty_body_payload["error"]["code"] == "not_implemented"


def test_registry_swappable():
    class FakeDocStore:
        def list_docs(self):
            return [{"path": "docs/SAFETY.md", "title": "SAFETY"}]

        def read_doc(self, path: str):
            return {"path": path, "markdown": "# Safety", "rendered_html": "<h1>Safety</h1>"}

    app = create_app(ServiceRegistry(doc_store=FakeDocStore()))
    client = TestClient(app)

    listed = client.get("/api/list_docs").json()
    assert listed["ok"] is True
    assert listed["data"] == [{"path": "docs/SAFETY.md", "title": "SAFETY"}]

    read = client.get("/api/read_doc", params={"path": "docs/SAFETY.md"}).json()
    assert read["ok"] is True
    assert read["data"]["path"] == "docs/SAFETY.md"
