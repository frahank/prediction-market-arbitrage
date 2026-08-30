# NotesStore implementation tests.
from __future__ import annotations

from pathlib import Path

from arbx.services.docs import NotesStoreImpl
from arbx.ui.envelope import OpError


def test_create_and_read_roundtrip(tmp_path: Path):
    store = NotesStoreImpl(tmp_path / "notes")

    saved = store.save_note("shift_notes", "# Shift\n\nWatch spreads.", expected_version=None)
    read = store.read_note("shift_notes")

    assert saved == {"name": "shift_notes", "version": 1}
    assert not isinstance(read, OpError)
    assert read == {
        "name": "shift_notes",
        "markdown": "# Shift\n\nWatch spreads.",
        "version": 1,
    }
    assert store.list_notes() == [{"name": "shift_notes", "version": 1}]


def test_version_conflict_rejected(tmp_path: Path):
    store = NotesStoreImpl(tmp_path / "notes")
    assert store.save_note("ops", "v1") == {"name": "ops", "version": 1}

    result = store.save_note("ops", "v2", expected_version=0)
    read = store.read_note("ops")

    assert isinstance(result, OpError)
    assert result.code == "conflict"
    assert not isinstance(read, OpError)
    assert read["markdown"] == "v1"
    assert read["version"] == 1


def test_history_preserved_on_overwrite(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    store = NotesStoreImpl(notes_dir)
    assert store.save_note("ops", "v1") == {"name": "ops", "version": 1}

    saved = store.save_note("ops", "v2", expected_version=1)

    assert saved == {"name": "ops", "version": 2}
    assert (notes_dir / ".history" / "ops.1.md").read_text(encoding="utf-8") == "v1"
    assert (notes_dir / "ops.md").read_text(encoding="utf-8") == "v2"
    assert (notes_dir / "ops.meta.json").read_text(encoding="utf-8") == '{"version": 2}\n'


def test_writes_confined_to_notes_dir(tmp_path: Path):
    notes_dir = tmp_path / "notes"
    store = NotesStoreImpl(notes_dir)

    result = store.save_note("../evil", "nope")

    assert isinstance(result, OpError)
    assert result.code == "invalid_request"
    assert not (tmp_path / "evil.md").exists()
    assert not notes_dir.exists()
