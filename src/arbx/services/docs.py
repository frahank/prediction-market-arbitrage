# Scope: BOT_RUNTIME — M5 document and notes service implementations.
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from arbx.ui.envelope import OpError

NOTE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _repo_relative(repo_root: Path, path: Path) -> str:
    return path.relative_to(repo_root).as_posix()


@dataclass(frozen=True, slots=True)
class _DocRoot:
    configured: str
    resolved: Path
    is_file: bool


class DocStoreImpl:
    """Read-only Markdown document store for configured repository docs."""

    def __init__(self, repo_root: Path, docs_roots: list[str]) -> None:
        self.repo_root = repo_root.resolve()
        self.docs_roots = self._resolve_roots(docs_roots)
        self._markdown = MarkdownIt("commonmark", {"html": False})

    def list_docs(self) -> list[dict[str, Any]]:
        docs: dict[Path, dict[str, Any]] = {}
        for root in self.docs_roots:
            paths = (root.resolved,) if root.is_file else sorted(root.resolved.rglob("*.md"))
            for path in paths:
                if not path.is_file() or path.suffix.lower() != ".md":
                    continue
                resolved = path.resolve()
                if not self._is_allowed_doc(resolved):
                    continue
                docs[resolved] = self._doc_meta(resolved)
        return [docs[path] for path in sorted(docs, key=lambda item: _repo_relative(self.repo_root, item))]

    def read_doc(self, path: str) -> dict[str, Any] | OpError:
        requested = Path(path)
        if not path or requested.is_absolute() or ".." in requested.parts:
            return OpError("invalid_request", "document path must be repository-relative")
        try:
            candidate = (self.repo_root / requested).resolve(strict=True)
        except FileNotFoundError:
            return OpError("not_found", "document was not found")
        if candidate.suffix.lower() != ".md":
            return OpError("invalid_request", "document path must point to a markdown file")
        if not self._is_allowed_doc(candidate):
            return OpError("invalid_request", "document path is outside configured docs roots")
        markdown = candidate.read_text(encoding="utf-8")
        return {
            **self._doc_meta(candidate, markdown=markdown),
            "markdown": markdown,
            "rendered_html": self._markdown.render(markdown),
        }

    def _resolve_roots(self, docs_roots: list[str]) -> tuple[_DocRoot, ...]:
        roots: list[_DocRoot] = []
        for configured in docs_roots:
            if not configured:
                continue
            raw_path = Path(configured)
            if raw_path.is_absolute():
                continue
            root_path = (self.repo_root / raw_path).resolve()
            if not _is_relative_to(root_path, self.repo_root) or not root_path.exists():
                continue
            roots.append(_DocRoot(configured=configured, resolved=root_path, is_file=root_path.is_file()))
        return tuple(roots)

    def _is_allowed_doc(self, path: Path) -> bool:
        if not _is_relative_to(path, self.repo_root):
            return False
        for root in self.docs_roots:
            if root.is_file and path == root.resolved:
                return True
            if not root.is_file and _is_relative_to(path, root.resolved):
                return True
        return False

    def _doc_meta(self, path: Path, *, markdown: str | None = None) -> dict[str, Any]:
        text = markdown if markdown is not None else path.read_text(encoding="utf-8")
        return {
            "path": _repo_relative(self.repo_root, path),
            "title": self._title_for(path, text),
        }

    @staticmethod
    def _title_for(path: Path, markdown: str) -> str:
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                if title:
                    return title
        return path.stem.replace("_", " ").replace("-", " ").title()


class NotesStoreImpl:
    """Versioned Markdown notes store for local operator notes."""

    def __init__(self, notes_dir: Path) -> None:
        self.notes_dir = notes_dir.resolve()
        self.history_dir = self.notes_dir / ".history"

    def list_notes(self) -> list[dict[str, Any]]:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        notes: list[dict[str, Any]] = []
        for path in sorted(self.notes_dir.glob("*.md")):
            if path.parent != self.notes_dir:
                continue
            name = path.stem
            if not NOTE_NAME_RE.fullmatch(name):
                continue
            notes.append({"name": name, "version": self._read_version(name)})
        return notes

    def read_note(self, name: str) -> dict[str, Any] | OpError:
        if not self._valid_name(name):
            return OpError("invalid_request", "note name must match [a-z0-9_-]+")
        note_path = self._note_path(name)
        if not note_path.exists():
            return OpError("not_found", "note was not found")
        return {
            "name": name,
            "markdown": note_path.read_text(encoding="utf-8"),
            "version": self._read_version(name),
        }

    def save_note(
        self,
        name: str,
        markdown: str,
        expected_version: int | None = None,
    ) -> dict[str, Any] | OpError:
        if not self._valid_name(name):
            return OpError("invalid_request", "note name must match [a-z0-9_-]+")
        if not isinstance(markdown, str):
            return OpError("invalid_request", "markdown must be a string")

        self.notes_dir.mkdir(parents=True, exist_ok=True)
        note_path = self._note_path(name)
        current_version = self._read_version(name) if note_path.exists() else 0
        if expected_version is not None and expected_version != current_version:
            return OpError(
                "conflict",
                "note version conflict",
                {"current_version": current_version, "expected_version": expected_version},
            )

        if note_path.exists():
            self.history_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(note_path, self.history_dir / f"{name}.{current_version}.md")

        next_version = current_version + 1
        self._atomic_write_text(note_path, markdown)
        self._atomic_write_text(self._meta_path(name), json.dumps({"version": next_version}, sort_keys=True) + "\n")
        return {"name": name, "version": next_version}

    def _note_path(self, name: str) -> Path:
        return self.notes_dir / f"{name}.md"

    def _meta_path(self, name: str) -> Path:
        return self.notes_dir / f"{name}.meta.json"

    def _read_version(self, name: str) -> int:
        meta_path = self._meta_path(name)
        if not meta_path.exists():
            return 0
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return 0
        version = data.get("version")
        return version if isinstance(version, int) and version >= 0 else 0

    @staticmethod
    def _valid_name(name: str) -> bool:
        return bool(NOTE_NAME_RE.fullmatch(name))

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
