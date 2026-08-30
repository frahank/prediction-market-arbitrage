# Scope: BOT_RUNTIME — M5-T3 Documents tab route/API wiring tests.
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from arbx.services.docs import DocStoreImpl, NotesStoreImpl
from arbx.ui.app import ServiceRegistry, create_app


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _client_for_repo(repo_root: Path) -> TestClient:
    services = ServiceRegistry(
        doc_store=DocStoreImpl(repo_root, ["README.md", "docs"]),
        notes_store=NotesStoreImpl(repo_root / "docs" / "notes"),
    )
    return TestClient(create_app(services))


def test_docs_page_renders():
    client = TestClient(create_app(ServiceRegistry()))

    response = client.get("/docs-viewer")

    assert response.status_code == 200
    assert "Documents" in response.text
    assert "Repo Docs" in response.text
    assert "Operator Note" in response.text
    assert "docs.js" in response.text


def test_list_and_read_ops_return_envelopes(tmp_path: Path):
    _write(tmp_path / "README.md", "# Readme\n")
    _write(tmp_path / "docs" / "SAFETY.md", "# Safety\n\n<script>x</script>\n")
    client = _client_for_repo(tmp_path)

    listed = client.get("/api/list_docs").json()
    read = client.get("/api/read_doc", params={"path": "docs/SAFETY.md"}).json()

    assert listed["ok"] is True
    assert listed["error"] is None
    assert listed["data"] == [
        {"path": "README.md", "title": "Readme"},
        {"path": "docs/SAFETY.md", "title": "Safety"},
    ]
    assert read["ok"] is True
    assert read["data"]["path"] == "docs/SAFETY.md"
    assert "<script>" not in read["data"]["rendered_html"]
    assert "&lt;script&gt;" in read["data"]["rendered_html"]


def test_save_note_roundtrip_via_api(tmp_path: Path):
    _write(tmp_path / "README.md", "# Readme\n")
    _write(tmp_path / "docs" / "SAFETY.md", "# Safety\n")
    client = _client_for_repo(tmp_path)

    saved = client.post(
        "/api/save_note",
        json={"name": "shift_notes", "markdown": "# Shift", "expected_version": None},
    ).json()
    read = client.get("/api/read_note", params={"name": "shift_notes"}).json()
    listed = client.get("/api/list_notes").json()
    stale = client.post(
        "/api/save_note",
        json={"name": "shift_notes", "markdown": "stale", "expected_version": 0},
    ).json()

    assert saved["ok"] is True
    assert saved["data"] == {"name": "shift_notes", "version": 1}
    assert read["ok"] is True
    assert read["data"] == {"name": "shift_notes", "markdown": "# Shift", "version": 1}
    assert listed["ok"] is True
    assert listed["data"] == [{"name": "shift_notes", "version": 1}]
    assert stale["ok"] is False
    assert stale["error"]["code"] == "conflict"
