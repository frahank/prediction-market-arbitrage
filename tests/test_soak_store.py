# Soak metadata store tests.
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from arbx.services.datastore import DataServiceImpl, SoakStoreImpl
from arbx.ui.app import ServiceRegistry, create_app


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _jsonl(path: Path, rows: list[dict]) -> None:
    _write(path, "".join(json.dumps(row) + "\n" for row in rows))


def _book_row(*, bid: float = 0.4, ask: float = 0.5, seq: int = 1) -> dict:
    return {
        "venue": "kalshi",
        "market_id": "KXTEST",
        "capture_seq": seq,
        "capture_ts_utc": "2026-07-05T14:15:30+00:00",
        "best_bid": bid,
        "best_ask": ask,
        "fetch_elapsed_ms": 12.0,
        "freshness_status": "fresh",
    }


def _contract_soak(root: Path, name: str, *, edges_only: bool = False, started_at: str = "2026-07-05T14:15:30+00:00") -> Path:
    soak = root / name
    _write(
        soak / "manifest.json",
        json.dumps(
            {
                "soak_id": name,
                "label": "contract soak",
                "started_at": started_at,
                "ended_at": None,
                "pair_keys": ["PAIR_A", "PAIR_B"],
                "edges_only": edges_only,
                "record_books": not edges_only,
                "schema_version": 1,
            }
        ),
    )
    if edges_only:
        _jsonl(soak / "EDGES_20260705-141530.jsonl", [{"pair_key": "PAIR_A"}])
    else:
        _jsonl(soak / "raw" / "book" / "venue=kalshi" / "2026-07-05.jsonl", [_book_row()])
        _jsonl(soak / "scan" / "opportunities" / "2026-07-05.jsonl", [{"pair_key": "PAIR_A"}])
    return soak


def test_lists_contract_dirs_with_manifest(tmp_path: Path):
    soaks_root = tmp_path / "data" / "soaks"
    _contract_soak(soaks_root, "scan_20260705-141530")
    store = SoakStoreImpl(soaks_root, [])

    items, next_cursor = store.list_soaks()

    assert next_cursor is None
    assert len(items) == 1
    meta = items[0]
    assert meta.soak_id == "scan_20260705-141530"
    assert meta.label == "contract soak"
    assert meta.pair_keys == ("PAIR_A", "PAIR_B")
    assert meta.row_counts == {"book": 1, "opportunities": 1, "edges": 0}
    assert meta.edges_only is False
    assert meta.record_books is True


def test_synthesizes_meta_for_precontract_dirs(tmp_path: Path):
    legacy_parent = tmp_path / "legacy"
    data_dir = legacy_parent / "data_streaming_soak_20260703"
    _jsonl(data_dir / "raw" / "book" / "venue=kalshi" / "2026-07-03.jsonl", [_book_row()])
    _jsonl(data_dir / "raw" / "edge" / "2026-07-03.jsonl", [{"pair_key": "PAIR_SYN"}])
    store = SoakStoreImpl(tmp_path / "data" / "soaks", [data_dir])

    items, _ = store.list_soaks()

    assert len(items) == 1
    assert items[0].soak_id == "data_streaming_soak_20260703"
    assert items[0].label == "data_streaming_soak_20260703"
    assert items[0].pair_keys == ("PAIR_SYN",)
    assert items[0].row_counts["book"] == 1
    assert items[0].row_counts["edges"] == 1


def test_legacy_dir_flagged_and_corrector_required(tmp_path: Path):
    legacy_parent = tmp_path / "legacy"
    data_dir = legacy_parent / "data_swapped"
    _jsonl(data_dir / "raw" / "book" / "venue=kalshi" / "2026-07-03.jsonl", [_book_row(bid=0.7, ask=0.6)])
    store = SoakStoreImpl(tmp_path / "data" / "soaks", [legacy_parent])

    items, _ = store.list_soaks()

    assert len(items) == 1
    assert items[0].legacy_book_fix_applied is True
    assert items[0].dq_status == "fail"


def test_edges_only_filter(tmp_path: Path):
    soaks_root = tmp_path / "data" / "soaks"
    _contract_soak(soaks_root, "scan_20260705-141530")
    _contract_soak(soaks_root, "scan_20260705-151530_EDGES", edges_only=True, started_at="2026-07-05T15:15:30+00:00")
    store = SoakStoreImpl(soaks_root, [])

    items, _ = store.list_soaks(edges_only=True)

    assert [item.soak_id for item in items] == ["scan_20260705-151530_EDGES"]


def test_cursor_pagination_stable(tmp_path: Path):
    soaks_root = tmp_path / "data" / "soaks"
    _contract_soak(soaks_root, "scan_20260705-141530", started_at="2026-07-05T14:15:30+00:00")
    _contract_soak(soaks_root, "scan_20260705-151530", started_at="2026-07-05T15:15:30+00:00")
    _contract_soak(soaks_root, "scan_20260705-161530", started_at="2026-07-05T16:15:30+00:00")
    store = SoakStoreImpl(soaks_root, [])

    first, cursor = store.list_soaks(limit=2)
    second, next_cursor = store.list_soaks(cursor=cursor, limit=2)

    assert [item.soak_id for item in first] == ["scan_20260705-161530", "scan_20260705-151530"]
    assert cursor == "scan_20260705-151530"
    assert [item.soak_id for item in second] == ["scan_20260705-141530"]
    assert next_cursor is None


def test_list_soaks_api_returns_legacy_streaming_soak_dir(tmp_path: Path):
    # A legacy `data_*` root with a raw/ tree surfaces as a soak (book-fix flagged).
    legacy = tmp_path / "data_streaming_soak_20260703"
    book = legacy / "raw" / "book" / "venue=kalshi"
    book.mkdir(parents=True)
    (book / "2026-07-03.jsonl").write_text(
        '{"venue": "kalshi", "market_id": "KX", "best_bid": 0.4, "best_ask": 0.41}\n',
        encoding="utf-8",
    )
    store = SoakStoreImpl(
        tmp_path / "data" / "soaks",
        [legacy],
        cache_dir=tmp_path / "cache",
    )
    client = TestClient(create_app(ServiceRegistry(data_service=DataServiceImpl(store))))

    payload = client.get("/api/list_soaks", params={"limit": 10}).json()

    assert payload["ok"] is True
    ids = {item["soak_id"] for item in payload["data"]["items"]}
    assert "data_streaming_soak_20260703" in ids
