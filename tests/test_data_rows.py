# Bounded standardized row-read tests.
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from arbx.services.datastore import DataServiceImpl, SoakStoreImpl, _map_book_row
from arbx.ui.schemas import StandardizedDataRow, StandardizedEdgeRow

DEPTH_HAIRCUT = 0.5

EDGE_FIELDS = {f.name for f in dataclasses.fields(StandardizedEdgeRow)}
DATA_FIELDS = {f.name for f in dataclasses.fields(StandardizedDataRow)}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _jsonl(path: Path, rows: list[dict]) -> None:
    _write(path, "".join(json.dumps(row) + "\n" for row in rows))


def _manifest(soak: Path, name: str) -> None:
    _write(
        soak / "manifest.json",
        json.dumps(
            {
                "soak_id": name,
                "label": name,
                "started_at": "2026-07-05T14:15:30+00:00",
                "ended_at": None,
                "pair_keys": ["PAIR_A"],
                "edges_only": False,
                "record_books": True,
                "schema_version": 1,
            }
        ),
    )


def _opportunity_row(**overrides) -> dict:
    row = {
        "pair_key": "PAIR_A",
        "direction": "kalshi_yes_poly_no",
        "capture_ts_utc": "2026-07-05T14:15:30+00:00",
        "scanned_at": "2026-07-05T14:15:31+00:00",
        "arb_detected": True,
        "qualifies": False,
        "raw_edge": 0.05,
        "fee_adj_edge": 0.03,
        "depth_adj_edge": 0.02,
        "target_size": 10.0,
        "depth_fillable_size": 100.0,
        "max_profitable_size": 40.0,
        "kalshi_vwap": 0.45,
        "polymarket_vwap": 0.52,
        "slippage": 0.001,
        "capture_skew_ms": -6.5,
        "kalshi_freshness_status": "fresh",
        "polymarket_freshness_status": "fresh",
        "survival_tier": None,
        "fee_model_version": "kalshi_v1+poly_endpoint_v1",
        "fee_usd_at_target": 0.2,
        "include_in_strategy_metrics": True,
    }
    row.update(overrides)
    return row


def _book_row(*, bid: float = 0.4, ask: float = 0.5, captured_at: str = "2026-07-05T14:15:30+00:00") -> dict:
    return {
        "venue": "kalshi",
        "market_id": "KXTEST",
        "capture_ts_utc": captured_at,
        "best_bid": bid,
        "best_ask": ask,
        "fetch_elapsed_ms": 12.0,
        "staleness_seconds": 1.5,
        "freshness_status": "fresh",
    }


def _service(soaks_root: Path, legacy_roots: list[Path] | None = None) -> DataServiceImpl:
    store = SoakStoreImpl(soaks_root, legacy_roots or [])
    return DataServiceImpl(store, depth_haircut=DEPTH_HAIRCUT)


def test_edges_rows_match_schema_fields(tmp_path: Path):
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530"
    _manifest(soak, "scan_20260705-141530")
    _jsonl(
        soak / "scan" / "opportunities" / "2026-07-05.jsonl",
        [_opportunity_row(), _opportunity_row(direction="kalshi_no_poly_yes")],
    )
    service = _service(soaks_root)

    result = service.list_soak_rows("scan_20260705-141530", "edges")

    assert result["next_cursor"] is None
    assert len(result["items"]) == 2
    for item in result["items"]:
        assert set(item) == EDGE_FIELDS
        json.dumps(item)
    first = result["items"][0]
    assert first["edge_id"].startswith("scan_20260705-141530:scan/opportunities/")
    assert first["est_fees"] == pytest.approx(0.02)  # fee_usd_at_target / target_size
    assert first["round_trip_latency_ms"] == pytest.approx(6.5)  # |capture_skew_ms|
    assert first["freshness_status"] == "fresh"
    assert first["display_name"] == "PAIR_A"


def test_books_rows_bounded_and_cursor_resumes(tmp_path: Path):
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530"
    _manifest(soak, "scan_20260705-141530")
    stamps = [f"2026-07-05T14:15:3{i}+00:00" for i in range(5)]
    _jsonl(
        soak / "raw" / "book" / "venue=kalshi" / "2026-07-05.jsonl",
        [_book_row(captured_at=stamp) for stamp in stamps[:3]],
    )
    _jsonl(
        soak / "raw" / "book" / "venue=polymarket" / "2026-07-05.jsonl",
        [_book_row(captured_at=stamp) for stamp in stamps[3:]],
    )
    service = _service(soaks_root)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        result = service.list_soak_rows("scan_20260705-141530", "books", cursor=cursor, limit=2)
        pages += 1
        assert len(result["items"]) <= 2
        for item in result["items"]:
            assert set(item) == DATA_FIELDS
        seen.extend(item["captured_at"] for item in result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert seen == stamps  # every row exactly once, stable file-sorted order
    assert len(seen) == len(set(seen))


def test_legacy_rows_corrected_before_mapping(tmp_path: Path):
    legacy_parent = tmp_path / "legacy"
    data_dir = legacy_parent / "data_swapped"
    _jsonl(
        data_dir / "raw" / "book" / "venue=kalshi" / "2026-07-03.jsonl",
        [_book_row(bid=0.7, ask=0.6)],  # legacy labels: bid above ask
    )
    service = _service(tmp_path / "data" / "soaks", [legacy_parent])

    result = service.list_soak_rows("data_swapped", "books")

    assert len(result["items"]) == 1
    flags = result["items"][0]["dq_flags"]
    # corrected: mapped best bid < best ask, so no crossed_book flag survives
    assert "crossed_book" not in flags
    assert "legacy_book_fix" in flags

    # control: the same raw row mapped WITHOUT the corrector stays crossed —
    # the corrector routing is what makes the difference
    uncorrected = _map_book_row(_book_row(bid=0.7, ask=0.6), legacy_fix=False)
    assert "crossed_book" in uncorrected.dq_flags


def test_est_profit_is_depth_haircut_not_visible(tmp_path: Path):
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530"
    _manifest(soak, "scan_20260705-141530")
    _jsonl(soak / "scan" / "opportunities" / "2026-07-05.jsonl", [_opportunity_row()])
    service = _service(soaks_root)

    item = service.list_soak_rows("scan_20260705-141530", "edges")["items"][0]

    assert item["visible_size"] == pytest.approx(100.0)  # depth_fillable_size
    assert item["executable_size"] == pytest.approx(40.0 * DEPTH_HAIRCUT)
    assert item["est_profit"] == pytest.approx(0.02 * 20.0)  # depth_adj × executable
    # never the visible-depth value
    assert item["est_profit"] != pytest.approx(0.02 * 100.0)
    assert item["executable_size"] != item["visible_size"]


def test_edges_file_passthrough_and_legacy_strategy_gate(tmp_path: Path):
    # EDGES_*.jsonl takes priority and passes through with defaults filled
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-151530_EDGES"
    _manifest(soak, "scan_20260705-151530_EDGES")
    _jsonl(
        soak / "EDGES_20260705-151530.jsonl",
        [{"pair_key": "PAIR_A", "direction": "kalshi_yes_poly_no", "est_profit": 0.1}],
    )
    service = _service(soaks_root)
    item = service.list_soak_rows("scan_20260705-151530_EDGES", "edges")["items"][0]
    assert set(item) == EDGE_FIELDS
    assert item["est_profit"] == pytest.approx(0.1)
    assert item["edge_id"].startswith("scan_20260705-151530_EDGES:EDGES_")
    assert item["include_in_strategy_metrics"] is False  # conservative default

    # legacy dirs: swap-artifact edges never re-enter strategy metrics
    legacy_parent = tmp_path / "legacy"
    data_dir = legacy_parent / "data_old"
    _jsonl(
        data_dir / "raw" / "edge" / "2026-07-03.jsonl",
        [_opportunity_row(include_in_strategy_metrics=True)],
    )
    legacy_service = _service(tmp_path / "data" / "soaks2", [legacy_parent])
    legacy_item = legacy_service.list_soak_rows("data_old", "edges")["items"][0]
    assert legacy_item["include_in_strategy_metrics"] is False


def test_invalid_kind_and_unknown_soak(tmp_path: Path):
    service = _service(tmp_path / "data" / "soaks")
    missing = service.list_soak_rows("nope", "edges")
    assert missing.code == "not_found"

    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530"
    _manifest(soak, "scan_20260705-141530")
    _jsonl(soak / "scan" / "opportunities" / "2026-07-05.jsonl", [_opportunity_row()])
    invalid = _service(soaks_root).list_soak_rows("scan_20260705-141530", "trades")
    assert invalid.code == "invalid_request"
