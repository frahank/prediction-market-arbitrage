# Scope: TEST — M2-T3 EDGES writer: standardized rows persisted at capture time.
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from arbx.pairs.registry import EquivalenceRecord, PairSpec, PairTaxonomy
from arbx.scanner.edges_writer import EdgesWriter, build_edge_row
from arbx.services.datastore import DataServiceImpl, SoakStoreImpl
from arbx.ui.schemas import StandardizedEdgeRow

EDGE_FIELDS = {f.name for f in dataclasses.fields(StandardizedEdgeRow)}
DEPTH_HAIRCUT = 0.5


def _pair_spec(*, include: bool = True, equivalence_status: str = "verified_equivalent") -> PairSpec:
    return PairSpec(
        pair_key="KXGOOD-26|0xgood",
        kalshi_market_id="KXGOOD-26",
        polymarket_condition_id="0xgood",
        polymarket_yes_token_id="yes",
        polymarket_no_token_id="no",
        orientation="same",
        status="approved_for_paper",
        include_in_strategy_metrics=include,
        raw={},
        taxonomy=PairTaxonomy(),
        equivalence=EquivalenceRecord(status=equivalence_status),
        display_name="Good equivalent pair",
    )


def _record(**overrides) -> dict:
    record = {
        "pair_key": "KXGOOD-26|0xgood",
        "direction": "kalshi_yes_poly_no",
        "capture_ts_utc": "2026-07-05T14:15:30+00:00",
        "scanned_at": "2026-07-05T14:15:31+00:00",
        "arb_detected": True,
        "qualifies": True,
        "raw_edge": 0.05,
        "fee_adj_edge": 0.03,
        "depth_adj_edge": 0.02,
        "target_size": 10.0,
        "depth_fillable_size": 100.0,
        "max_profitable_size": 40.0,
        "kalshi_vwap": 0.45,
        "polymarket_vwap": 0.52,
        "slippage": 0.001,
        "capture_skew_ms": -5.1,
        "kalshi_freshness_status": "fresh",
        "polymarket_freshness_status": "fresh",
        "fee_model_version": "kalshi_v1+poly_endpoint_v1",
        "fee_usd_at_target": 0.2,
        "kalshi_fetch_elapsed_ms": 110.3,
        "polymarket_fetch_elapsed_ms": 95.2,
    }
    record.update(overrides)
    return record


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_rows_are_standardized_and_json_safe(tmp_path: Path):
    soak = tmp_path / "scan_20260705-141530_EDGES"
    writer = EdgesWriter(soak, depth_haircut=DEPTH_HAIRCUT)
    writer.write(_pair_spec(), _record())
    writer.write(_pair_spec(), _record(direction="kalshi_no_poly_yes"))
    writer.close()

    assert writer.path.name == "EDGES_20260705-141530.jsonl"
    rows = _read_rows(writer.path)
    assert len(rows) == 2
    for row in rows:
        assert set(row) == EDGE_FIELDS
        json.dumps(row)
    assert rows[0]["edge_id"].startswith("scan_20260705-141530_EDGES:EDGES_20260705-141530.jsonl:")
    assert rows[0]["edge_id"] != rows[1]["edge_id"]
    assert rows[0]["est_fees"] == pytest.approx(0.02)  # fee_usd_at_target / target


def test_est_profit_uses_executable_not_visible(tmp_path: Path):
    row = build_edge_row(
        _pair_spec(), _record(), edge_id="x", depth_haircut=DEPTH_HAIRCUT
    ).to_dict()

    assert row["visible_size"] == pytest.approx(100.0)
    assert row["executable_size"] == pytest.approx(40.0 * DEPTH_HAIRCUT)
    assert row["est_profit"] == pytest.approx(0.02 * 20.0)
    assert row["est_profit"] != pytest.approx(0.02 * 100.0)  # never visible size


def test_honest_fields_flow_from_registry():
    good = build_edge_row(
        _pair_spec(equivalence_status="tail_divergence_documented"),
        _record(), edge_id="x", depth_haircut=DEPTH_HAIRCUT,
    ).to_dict()
    connectivity = build_edge_row(
        _pair_spec(include=False, equivalence_status="unreviewed"),
        _record(), edge_id="y", depth_haircut=DEPTH_HAIRCUT,
    ).to_dict()

    assert good["contract_equivalent"] == "tail_divergence_documented"
    assert good["include_in_strategy_metrics"] is True
    assert good["display_name"] == "Good equivalent pair"
    assert good["simulation_scope"] == "public_displayed_books"
    assert connectivity["contract_equivalent"] == "unreviewed"
    assert connectivity["include_in_strategy_metrics"] is False


def test_latency_definition_pinned():
    row = build_edge_row(
        _pair_spec(),
        _record(kalshi_fetch_elapsed_ms=110.3, polymarket_fetch_elapsed_ms=95.2),
        edge_id="x", depth_haircut=DEPTH_HAIRCUT,
    ).to_dict()
    # the slower leg's wire time — not the 5.1ms capture skew
    assert row["round_trip_latency_ms"] == pytest.approx(110.3)
    assert row["capture_skew_ms"] == pytest.approx(-5.1)

    missing = build_edge_row(
        _pair_spec(),
        _record(kalshi_fetch_elapsed_ms=None, polymarket_fetch_elapsed_ms=None),
        edge_id="x", depth_haircut=DEPTH_HAIRCUT,
    ).to_dict()
    assert missing["round_trip_latency_ms"] == pytest.approx(5.1)  # |skew| fallback


def test_flat_fee_rows_labeled_heuristic():
    row = build_edge_row(
        _pair_spec(),
        _record(fee_usd_at_target=None, fee_model_version=None),
        edge_id="x", depth_haircut=DEPTH_HAIRCUT,
    ).to_dict()
    assert row["est_fees"] == pytest.approx(0.05 - 0.03)  # raw − fee_adj
    assert row["fee_model_version"] == "flat_heuristic"


def test_qualifying_only_gate(tmp_path: Path):
    writer = EdgesWriter(tmp_path / "scan_20260705-141530", depth_haircut=DEPTH_HAIRCUT, qualifying_only=True)
    writer.write(_pair_spec(), _record(qualifies=False, arb_detected=True))
    writer.write(_pair_spec(), _record(qualifies=True))
    writer.close()

    rows = _read_rows(writer.path)
    assert len(rows) == 1
    assert rows[0]["qualifies"] is True


def test_written_file_reads_back_through_data_service(tmp_path: Path):
    """DoD: Module 4 consumes writer output with no mapping fallbacks."""
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530_EDGES"
    writer = EdgesWriter(soak, depth_haircut=DEPTH_HAIRCUT)
    writer.write(_pair_spec(), _record())
    writer.close()
    (soak / "manifest.json").write_text(json.dumps({
        "soak_id": soak.name, "label": soak.name,
        "started_at": "2026-07-05T14:15:30+00:00", "ended_at": None,
        "pair_keys": ["KXGOOD-26|0xgood"], "edges_only": True,
        "record_books": False, "schema_version": 1,
    }), encoding="utf-8")

    service = DataServiceImpl(SoakStoreImpl(soaks_root, []), depth_haircut=DEPTH_HAIRCUT)
    items = service.list_soak_rows(soak.name, "edges")["items"]

    assert len(items) == 1
    written = _read_rows(writer.path)[0]
    assert items[0] == written  # byte-for-byte passthrough, no fallback rewrites