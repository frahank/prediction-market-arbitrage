# Data tab route and API wiring tests.
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


def _fixture_client(tmp_path: Path) -> TestClient:
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530"
    _write(
        soak / "manifest.json",
        json.dumps(
            {
                "soak_id": "scan_20260705-141530",
                "label": "fixture soak",
                "started_at": "2026-07-05T14:15:30+00:00",
                "ended_at": None,
                "pair_keys": ["PAIR_A"],
                "edges_only": False,
                "record_books": True,
                "schema_version": 1,
            }
        ),
    )
    _jsonl(
        soak / "scan" / "opportunities" / "2026-07-05.jsonl",
        [
            {
                "pair_key": "PAIR_A",
                "direction": "kalshi_yes_poly_no",
                "scanned_at": "2026-07-05T14:15:31+00:00",
                "arb_detected": True,
                "qualifies": False,
                "raw_edge": 0.05,
                "fee_adj_edge": 0.03,
                "depth_adj_edge": 0.02,
                "target_size": 10.0,
                "depth_fillable_size": 100.0,
                "max_profitable_size": 20.0,
                "capture_skew_ms": 5.0,
                "kalshi_freshness_status": "fresh",
                "polymarket_freshness_status": "fresh",
                "fee_usd_at_target": 0.2,
            }
        ],
    )
    _jsonl(
        soak / "raw" / "book" / "venue=kalshi" / "2026-07-05.jsonl",
        [
            {
                "venue": "kalshi",
                "market_id": "KXTEST",
                "capture_ts_utc": "2026-07-05T14:15:30+00:00",
                "best_bid": 0.4,
                "best_ask": 0.5,
                "fetch_elapsed_ms": 12.0,
                "freshness_status": "fresh",
            }
        ],
    )
    edge_soak = soaks_root / "scan_20260705-151530_EDGES"
    _write(
        edge_soak / "manifest.json",
        json.dumps(
            {
                "soak_id": "scan_20260705-151530_EDGES",
                "label": "edges only",
                "started_at": "2026-07-05T15:15:30+00:00",
                "ended_at": None,
                "pair_keys": ["PAIR_B"],
                "edges_only": True,
                "record_books": False,
                "schema_version": 1,
            }
        ),
    )
    _jsonl(edge_soak / "EDGES_20260705-151530.jsonl", [{"pair_key": "PAIR_B", "est_profit": 0.1}])

    store = SoakStoreImpl(soaks_root, [], cache_dir=tmp_path / "cache")
    return TestClient(create_app(ServiceRegistry(data_service=DataServiceImpl(store))))


def test_data_page_renders():
    client = TestClient(create_app(ServiceRegistry()))

    response = client.get("/data")

    assert response.status_code == 200
    assert "Data" in response.text
    assert "Edges only" in response.text
    assert "displayed public books" in response.text
    assert "data.js" in response.text


def test_end_to_end_list_and_rows_envelopes(tmp_path: Path):
    client = _fixture_client(tmp_path)

    soaks = client.get("/api/list_soaks").json()
    edges = client.get(
        "/api/list_soak_rows",
        params={"soak_id": "scan_20260705-141530", "kind": "edges", "limit": 10},
    ).json()
    books = client.get(
        "/api/list_soak_rows",
        params={"soak_id": "scan_20260705-141530", "kind": "books", "limit": 10},
    ).json()

    assert soaks["ok"] is True
    assert len(soaks["data"]["items"]) == 2
    assert edges["ok"] is True
    assert edges["data"]["items"][0]["pair_key"] == "PAIR_A"
    assert edges["data"]["items"][0]["est_profit"] > 0
    assert books["ok"] is True
    assert books["data"]["items"][0]["display_name"] == "kalshi:KXTEST"


def test_edge_only_filter_via_api(tmp_path: Path):
    client = _fixture_client(tmp_path)

    payload = client.get("/api/list_soaks", params={"edges_only": "true"}).json()

    assert payload["ok"] is True
    assert [item["soak_id"] for item in payload["data"]["items"]] == ["scan_20260705-151530_EDGES"]
