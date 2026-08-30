# Pinned envelope and standardized-schema contract tests.
from __future__ import annotations

import json
from datetime import datetime

from arbx.ui.envelope import SCHEMA_VERSION, OpError, envelope
from arbx.ui.schemas import (
    AnalysisSummary,
    PairSummary,
    SoakFileMeta,
    StandardizedDataRow,
    StandardizedEdgeRow,
    TestSuiteResult,
)

SAMPLE_EDGE_ROW = StandardizedEdgeRow(
    edge_id="scan_20260705-141530:12345",
    pair_key="REMA",
    display_name="Remain example",
    direction="kalshi_yes_poly_no",
    scanned_at="2026-07-05T14:15:30+00:00",
    arb_detected=True,
    qualifies=False,
    round_trip_latency_ms=41.2,
    est_fees=0.013,
    est_profit=0.42,
    raw_edge=0.031,
    fee_adj_edge=0.018,
    depth_adj_edge=0.012,
    visible_size=120.0,
    executable_size=35.0,
    vwap_kalshi=0.44,
    vwap_polymarket=0.53,
    slippage=0.004,
    capture_skew_ms=5.1,
    freshness_status="fresh",
    survival_tier=None,
    fee_model_version="fees_v2",
    simulation_scope="paper",
    contract_equivalent="verified_equivalent",
    include_in_strategy_metrics=True,
)

SAMPLE_DATA_ROW = StandardizedDataRow(
    pair_key="REMA",
    display_name="Remain example",
    captured_at="2026-07-05T14:15:30+00:00",
    round_trip_duration_ms=88.0,
    est_fees=None,
    est_profit=None,
    freshness_status="fresh",
    staleness_seconds=1.5,
    dq_flags=("gap_over_5s",),
    simulation_scope="paper",
    include_in_strategy_metrics=True,
)

SAMPLE_SOAK_META = SoakFileMeta(
    soak_id="scan_20260705-141530",
    label="overnight soak",
    path="data/soaks/scan_20260705-141530",
    started_at="2026-07-05T14:15:30+00:00",
    ended_at=None,
    pair_keys=("REMA", "WCNA"),
    pair_count=2,
    edges_only=False,
    record_books=True,
    row_counts={"book": 10, "opportunities": 2, "edges": 4},
    dq_status="pass",
    legacy_book_fix_applied=False,
    size_bytes=2048,
    schema_version=SCHEMA_VERSION,
)

SAMPLE_ANALYSIS = AnalysisSummary(
    soak_ids=("scan_20260705-141530",),
    generated_at="2026-07-05T18:00:00+00:00",
    profit_score=0.12,
    min_latency_needed_ms=None,
    chance_of_profit=0.2,
    chance_of_loss=0.8,
    would_have_made_money_live={
        "verdict": "insufficient_data",
        "rationale": ["zero trustworthy clean-concurrency episodes"],
        "basis": "model_v1",
    },
    dq={
        "passed": True,
        "recorder_health_passed": True,
        "freshness_passed": True,
        "detail": "",
    },
    fee_sensitivity={"1c": 3, "2c": 1, "real": 0},
    per_pair=({"pair_key": "REMA", "ev": -0.01},),
    sample={"snapshots": 100, "qualifying_rows": 0, "soak_hours": 8.0},
    graph=None,
    caveats=("sampling resolution is not survival",),
)

SAMPLE_PAIR = PairSummary(
    pair_key="REMA",
    display_name="Remain example",
    status="approved",
    kalshi_market_id="KXEXAMPLE-26",
    polymarket_condition_id="0xabc",
    polymarket_yes_token_id="123",
    resolution_structure="objective_single_event",
    grouping_alignment="clean",
    date_cutoff_delta_hours=0.0,
    time_to_resolution_days=30.0,
    persistence_cause="price_stickiness",
    equivalence={
        "status": "verified_equivalent",
        "audited_at": "2026-07-03",
        "auditor": "operator",
        "notes": "",
        "tail_risks": [],
    },
    orientation_confirmed={"confirmed": True},
    liquidity=None,
    edge_behavior=None,
    evidence_links=("docs/pair_equivalence_checklist.md",),
    latest_decision={"at": "2026-07-03", "decision": "approve", "rationale": "ok"},
    simulation_scope="paper",
    contract_equivalent="verified_equivalent",
    include_in_strategy_metrics=True,
)

SAMPLE_TEST_RESULT = TestSuiteResult(
    passed=True,
    total=244,
    failures=0,
    errors=0,
    duration_s=12.3,
    message="The bot is working properly.",
    detail_path="reports/test_runs/20260705-180000.txt",
)

ALL_SAMPLES = [
    SAMPLE_EDGE_ROW,
    SAMPLE_DATA_ROW,
    SAMPLE_SOAK_META,
    SAMPLE_ANALYSIS,
    SAMPLE_PAIR,
    SAMPLE_TEST_RESULT,
]


def test_envelope_shape_ok_and_error():
    ok = envelope({"x": 1})
    assert set(ok) == {"ok", "data", "error", "meta"}
    assert ok["ok"] is True
    assert ok["data"] == {"x": 1}
    assert ok["error"] is None
    assert ok["meta"]["schema_version"] == SCHEMA_VERSION
    # generated_at is ISO-8601 UTC
    stamp = datetime.fromisoformat(ok["meta"]["generated_at"])
    assert stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0

    err = envelope(error=OpError("not_found", "no such pair", {"pair_key": "X"}))
    assert set(err) == {"ok", "data", "error", "meta"}
    assert err["ok"] is False
    assert err["data"] is None
    assert err["error"] == {
        "code": "not_found",
        "message": "no such pair",
        "details": {"pair_key": "X"},
    }
    assert err["meta"]["schema_version"] == SCHEMA_VERSION
    # both branches serialize as-is
    json.dumps(ok)
    json.dumps(err)


def test_all_schemas_roundtrip_to_dict_json_safe():
    for sample in ALL_SAMPLES:
        d = sample.to_dict()
        dumped = json.dumps(d)
        # roundtrip: every field survives and nothing non-JSON leaks through
        assert json.loads(dumped) == d
        assert not any(isinstance(v, tuple) for v in d.values())


def test_honest_status_fields_present():
    for sample in (SAMPLE_EDGE_ROW, SAMPLE_DATA_ROW, SAMPLE_PAIR):
        d = sample.to_dict()
        assert "simulation_scope" in d
        assert "include_in_strategy_metrics" in d
    for sample in (SAMPLE_EDGE_ROW, SAMPLE_PAIR):
        assert "contract_equivalent" in sample.to_dict()


def test_schema_version_constant():
    assert SCHEMA_VERSION == 1
