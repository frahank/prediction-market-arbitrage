# Documents tab route and API wiring tests.
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


def test_rendered_links_resolve_for_the_viewer(tmp_path: Path):
    """Repo-relative links must not 404 against /docs-viewer.

    Rendered markdown carries paths relative to the document, but the page is a
    single view; images go to the asset route and markdown links become
    in-viewer links.
    """
    (tmp_path / "docs" / "images").mkdir(parents=True)
    (tmp_path / "docs" / "images" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "docs" / "other.md").write_text("# Other\n", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n![shot](images/shot.png)\n\n[other](other.md)\n"
        "[up](../README.md)\n[ext](https://example.com/a.png)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Top\n", encoding="utf-8")

    store = DocStoreImpl(tmp_path, ["docs", "README.md"])
    html = store.read_doc("docs/guide.md")["rendered_html"]

    assert '"/doc-asset/docs/images/shot.png"' in html
    assert '"#doc=docs/other.md"' in html
    assert '"#doc=README.md"' in html          # ../ normalized, not left relative
    assert '"https://example.com/a.png"' in html  # absolute URLs untouched


def test_doc_asset_route_serves_only_allowlisted_media(tmp_path: Path):
    (tmp_path / "docs" / "images").mkdir(parents=True)
    (tmp_path / "docs" / "images" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "docs" / "notes.md").write_text("# n\n", encoding="utf-8")
    (tmp_path / "secret.yaml").write_text("api_key: x\n", encoding="utf-8")

    store = DocStoreImpl(tmp_path, ["docs", "README.md"])
    client = TestClient(create_app(ServiceRegistry(doc_store=store)))

    assert client.get("/doc-asset/docs/images/shot.png").status_code == 200
    # Not an allowlisted media type, outside the docs roots, and traversal.
    assert client.get("/doc-asset/docs/notes.md").status_code == 404
    assert client.get("/doc-asset/secret.yaml").status_code == 404
    assert client.get("/doc-asset/../../etc/passwd").status_code == 404


def test_favicon_does_not_404():
    client = TestClient(create_app(ServiceRegistry()))
    assert client.get("/favicon.ico").status_code in (200, 204)
