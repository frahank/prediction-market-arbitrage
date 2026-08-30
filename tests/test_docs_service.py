# Scope: BOT_RUNTIME — M5-T1 DocStore implementation tests.
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from arbx.services.docs import DocStoreImpl
from arbx.ui.app import ServiceRegistry, create_app
from arbx.ui.envelope import OpError


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lists_only_markdown_under_roots(tmp_path: Path):
    _write(tmp_path / "README.md", "# Read Me\n")
    _write(tmp_path / "docs" / "SAFETY.md", "# Safety\n")
    _write(tmp_path / "docs" / "notes.txt", "not markdown")
    _write(tmp_path / "configs" / "runtime.yaml", "mode: paper\n")

    docs = DocStoreImpl(tmp_path, ["docs", "README.md"]).list_docs()

    assert docs == [
        {"path": "README.md", "title": "Read Me"},
        {"path": "docs/SAFETY.md", "title": "Safety"},
    ]


def test_read_renders_safe_html(tmp_path: Path):
    _write(tmp_path / "docs" / "unsafe.md", "# Unsafe\n\n<script>alert('x')</script>\n")
    store = DocStoreImpl(tmp_path, ["docs"])

    doc = store.read_doc("docs/unsafe.md")

    assert not isinstance(doc, OpError)
    assert doc["title"] == "Unsafe"
    assert doc["markdown"].startswith("# Unsafe")
    assert "<script>" not in doc["rendered_html"]
    assert "&lt;script&gt;" in doc["rendered_html"]


def test_path_traversal_rejected(tmp_path: Path):
    _write(tmp_path / "docs" / "SAFETY.md", "# Safety\n")
    _write(tmp_path / "configs" / "runtime.yaml", "mode: paper\n")
    store = DocStoreImpl(tmp_path, ["docs"])

    result = store.read_doc("../../configs/runtime.yaml")

    assert isinstance(result, OpError)
    assert result.code == "invalid_request"


def test_missing_doc_not_found(tmp_path: Path):
    _write(tmp_path / "docs" / "SAFETY.md", "# Safety\n")
    store = DocStoreImpl(tmp_path, ["docs"])

    result = store.read_doc("docs/missing.md")

    assert isinstance(result, OpError)
    assert result.code == "not_found"


def test_symlink_escape_rejected(tmp_path: Path):
    _write(tmp_path / "docs" / "SAFETY.md", "# Safety\n")
    outside = tmp_path.parent / "outside-secret.md"
    outside.write_text("# Secret\n", encoding="utf-8")
    (tmp_path / "docs" / "secret.md").symlink_to(outside)
    store = DocStoreImpl(tmp_path, ["docs"])

    listed_paths = {doc["path"] for doc in store.list_docs()}
    result = store.read_doc("docs/secret.md")

    assert "docs/secret.md" not in listed_paths
    assert isinstance(result, OpError)
    assert result.code == "invalid_request"


def test_api_read_doc_returns_rendered_content():
    repo_root = Path(__file__).resolve().parents[1]
    app = create_app(ServiceRegistry(doc_store=DocStoreImpl(repo_root, ["docs", "README.md"])))
    client = TestClient(app)

    response = client.get("/api/read_doc", params={"path": "docs/SAFETY.md"}).json()

    assert response["ok"] is True
    assert response["data"]["path"] == "docs/SAFETY.md"
    assert response["data"]["markdown"]
    assert response["data"]["rendered_html"]
