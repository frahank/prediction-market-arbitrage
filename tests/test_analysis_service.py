# Scope: TEST — M2-T4 AnalysisService: pinned AnalysisSummary v1 pipeline.
from __future__ import annotations

import dataclasses
import json
import threading
import time
from pathlib import Path

import pytest
import yaml

import arbx.services.analysis as analysis_module
from arbx.pairs.registry import write_registry_integrity
from arbx.services.analysis import STAGES, AnalysisServiceImpl
from arbx.services.datastore import SoakStoreImpl
from arbx.ui.envelope import OpError
from arbx.ui.schemas import AnalysisSummary

PAIR_KEY = "KXGOOD-26|0xgood"
SUMMARY_FIELDS = {f.name for f in dataclasses.fields(AnalysisSummary)}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _jsonl(path: Path, rows: list[dict]) -> None:
    _write(path, "".join(json.dumps(row) + "\n" for row in rows))


def _modeling_yaml(path: Path) -> None:
    _write(path, yaml.safe_dump({
        "schema_version": 1,
        "executable": {
            "depth_haircut": 0.5,
            "staleness_penalty_per_s": 0.001,
            "min_episode_snapshots": 2,
            "scenarios": {
                "clean_concurrency": {"max_skew_ms": 50.0, "validity": "provisional_small_sample"},
            },
        },
        "fill_probability": {
            "provisional": True,
            "tiers": {"survived_1000ms": 0.8, "survived_500ms": 0.5, "survived_250ms": 0.25},
            "unprobed": 0.25,
        },
        "fees": {"depth_fee_base": 0.02},
        "carry": {"capital_apr": 0.05, "collateral_model": "both_legs_full"},
        "failed_leg": {
            "leg_failure_prob": 0.1,
            "unwind_cost_model": "cross_spread",
            "unwind_fallback_spread": 0.02,
        },
        "analysis": {"assumed_reaction_latency_ms": 250},
        "viability": {"viable_min_ev_per_day_usd": 5.0, "viable_min_opportunities_per_week": 5},
    }))


def _registry_yaml(path: Path) -> None:
    _write(path, yaml.safe_dump({
        "schema_version": 2.1,
        "pairs": [{
            "pair_key": PAIR_KEY,
            "display_name": "Good equivalent pair",
            "kalshi_market_id": "KXGOOD-26",
            "orientation": "same",
            "status": "approved_for_paper",
            "include_in_strategy_metrics": True,
            "polymarket_identifiers": {
                "condition_id": "0xgood",
                "yes_token_id": "yes-KXGOOD-26",
                "no_token_id": "no-KXGOOD-26",
            },
            "taxonomy": {"time_to_resolution_days": 10.0},
            "equivalence": {"status": "verified_equivalent", "audited_at": "2026-07-05"},
            "decision_log": [{
                "at": "2026-07-05T00:00:00+00:00", "decision": "approve",
                "rationale": "fixture", "auditor": "fixture",
            }],
        }],
    }, sort_keys=False))
    write_registry_integrity(path)


def _opportunity_row(*, ts: str, qualifying: bool = True, **overrides) -> dict:
    row = {
        "pair_key": PAIR_KEY,
        "direction": "kalshi_yes_poly_no",
        "capture_ts_utc": ts,
        "scanned_at": ts,
        "books_fresh": qualifying,
        "capture_skew_ms": 5.0,
        "raw_edge": 0.05,
        "fee_adj_edge": 0.03,
        "depth_adj_edge": 0.03,
        "max_profitable_size": 50.0,
        "include_in_strategy_metrics": True,
        "arb_detected": True,
        "qualifies": qualifying,
    }
    row.update(overrides)
    return row


def _fixture_service(
    tmp_path: Path,
    rows: list[dict] | None = None,
    *,
    stage_hook=None,
) -> tuple[AnalysisServiceImpl, str]:
    soaks_root = tmp_path / "data" / "soaks"
    soak = soaks_root / "scan_20260705-141530"
    _write(soak / "manifest.json", json.dumps({
        "soak_id": soak.name, "label": soak.name,
        "started_at": "2026-07-05T14:15:30+00:00", "ended_at": None,
        "pair_keys": [PAIR_KEY], "edges_only": False,
        "record_books": False, "schema_version": 1,
    }))
    if rows is None:
        rows = [
            _opportunity_row(ts="2026-07-05T14:15:30+00:00"),
            _opportunity_row(ts="2026-07-05T14:16:00+00:00"),
            _opportunity_row(ts="2026-07-05T14:16:30+00:00", direction="kalshi_no_poly_yes"),
        ]
    _jsonl(soak / "scan" / "opportunities" / "2026-07-05.jsonl", rows)

    registry = tmp_path / "configs" / "pairs.approved.yaml"
    _registry_yaml(registry)
    modeling = tmp_path / "configs" / "modeling.yaml"
    _modeling_yaml(modeling)

    service = AnalysisServiceImpl(
        SoakStoreImpl(soaks_root, []),
        tmp_path / "reports" / "analysis_jobs",
        registry_path=registry,
        modeling_path=modeling,
        stage_hook=stage_hook,
    )
    return service, soak.name


def _wait_done(service: AnalysisServiceImpl, job_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = service.get_analysis_status(job_id)
        if isinstance(status, dict) and status["state"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("analysis job did not finish in time")


def test_summary_all_fields_on_fixture_soak(tmp_path: Path):
    service, soak_id = _fixture_service(tmp_path)

    started = service.run_full_analysis([soak_id])
    assert not isinstance(started, OpError), started
    status = _wait_done(service, started["job_id"])

    assert status["state"] == "done", status.get("error")
    summary = status["summary"]
    assert set(summary) == SUMMARY_FIELDS
    json.dumps(summary)
    assert summary["soak_ids"] == [soak_id]
    assert summary["caveats"]  # honesty caveats non-empty
    assert any("sampling" in c for c in summary["caveats"])
    assert any("REST" in c for c in summary["caveats"])
    assert summary["would_have_made_money_live"]["basis"] == "model_v1"
    assert summary["would_have_made_money_live"]["verdict"] in {
        "viable", "marginal", "not_viable", "insufficient_data",
    }
    assert summary["sample"]["snapshots"] == 3
    assert summary["sample"]["qualifying_rows"] == 3
    assert summary["fee_sensitivity"]["real"] == 3
    assert summary["graph"]["kind"] == "edge_timeline_v1"
    assert PAIR_KEY in summary["graph"]["payload"]["series"]
    assert summary["per_pair"]  # classify_pairs rows present
    # no probes in fixture → placeholder P(survival) at 250ms = 0.25, rate = 1.0
    assert summary["chance_of_profit"] == pytest.approx(0.25)
    assert summary["chance_of_loss"] == pytest.approx(1 - 0.25 * 0.9)


def test_no_qualifying_rows_yields_none_latency_and_insufficient_data_verdict(tmp_path: Path):
    rows = [
        _opportunity_row(ts="2026-07-05T14:15:30+00:00", qualifying=False),
        _opportunity_row(ts="2026-07-05T14:16:00+00:00", qualifying=False),
    ]
    service, soak_id = _fixture_service(tmp_path, rows)

    started = service.run_full_analysis([soak_id])
    status = _wait_done(service, started["job_id"])

    summary = status["summary"]
    assert summary["min_latency_needed_ms"] is None
    assert summary["would_have_made_money_live"]["verdict"] == "insufficient_data"
    assert summary["chance_of_profit"] == 0.0
    assert summary["sample"]["qualifying_rows"] == 0


def _legacy_book_row(*, venue: str, market_id: str, bid: float, ask: float, recv_ns: int, seq: int) -> dict:
    # stored with swapped labels (bid above ask), as every pre-fix row is
    row = {
        "venue": venue, "market_id": market_id, "capture_seq": seq,
        "capture_ts_utc": "2026-07-03T05:00:00+00:00",
        "recv_monotonic_ns": recv_ns,
        "best_bid": ask, "best_ask": bid,  # swapped on purpose
        "freshness_status": "fresh", "fetch_elapsed_ms": 100.0,
    }
    for i in range(1, 6):
        row[f"bid_px_{i}"], row[f"bid_sz_{i}"] = ask + 0.01 * (i - 1), 50.0
        row[f"ask_px_{i}"], row[f"ask_sz_{i}"] = bid - 0.01 * (i - 1), 50.0
    return row


def test_legacy_soak_routed_through_corrector(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)
    legacy_parent = tmp_path / "legacy"
    data_dir = legacy_parent / "data_old_soak"
    _jsonl(data_dir / "raw" / "book" / "venue=kalshi" / "2026-07-03.jsonl", [
        _legacy_book_row(venue="kalshi", market_id="KXGOOD-26", bid=0.40, ask=0.42, recv_ns=1_000_000_000, seq=1),
    ])
    _jsonl(data_dir / "raw" / "book" / "venue=polymarket" / "2026-07-03.jsonl", [
        _legacy_book_row(venue="polymarket", market_id="yes-KXGOOD-26", bid=0.47, ask=0.49, recv_ns=1_001_000_000, seq=2),
    ])
    # poison stored edge rows: pre-fix artifacts that must NOT be read
    _jsonl(data_dir / "raw" / "edge" / "2026-07-03.jsonl", [
        _opportunity_row(ts="2026-07-03T05:00:00+00:00", raw_edge=9.9, fee_adj_edge=9.9),
    ])
    service.soak_store = SoakStoreImpl(tmp_path / "data" / "soaks", [legacy_parent])

    started = service.run_full_analysis(["data_old_soak"])
    status = _wait_done(service, started["job_id"])

    assert status["state"] == "done", status.get("error")
    summary = status["summary"]
    assert any("corrector" in c for c in summary["caveats"])
    series = summary["graph"]["payload"]["series"][PAIR_KEY]
    fee_edges = sorted(point[1] for point in series)
    # corrected: raw = poly_bid(0.47) − kalshi_ask(0.42) = 0.05 → fee_adj 0.03.
    # uncorrected would be 0.49 − 0.40 = 0.09 → fee_adj 0.07; poisoned rows 9.9.
    assert any(abs(edge - 0.03) < 1e-9 for edge in fee_edges)
    assert all(edge < 0.07 - 1e-9 for edge in fee_edges)
    assert 9.9 not in fee_edges


def test_concurrent_job_conflicts(tmp_path: Path):
    release = threading.Event()

    def hook(stage: str) -> None:
        if stage == "dq":
            release.wait(timeout=15.0)

    service, soak_id = _fixture_service(tmp_path, stage_hook=hook)
    started = service.run_full_analysis([soak_id])
    assert not isinstance(started, OpError)
    try:
        second = service.run_full_analysis([soak_id])
        assert isinstance(second, OpError)
        assert second.code == "conflict"
    finally:
        release.set()
    status = _wait_done(service, started["job_id"])
    assert status["state"] == "done"
    third = service.run_full_analysis([soak_id])
    assert not isinstance(third, OpError)
    _wait_done(service, third["job_id"])


def test_progress_stages_recorded(tmp_path: Path):
    seen: list[str] = []
    service, soak_id = _fixture_service(tmp_path, stage_hook=seen.append)

    started = service.run_full_analysis([soak_id])
    status = _wait_done(service, started["job_id"])

    assert seen == list(STAGES)
    assert status["progress"]["stage"] == "summary"
    assert status["progress"]["pct"] == 100.0
    # progress file is the crash-resumable read side
    job_file = service.jobs_dir / f"{started['job_id']}.json"
    assert json.loads(job_file.read_text())["state"] == "done"


def test_graph_payload_swappable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, soak_id = _fixture_service(tmp_path)
    monkeypatch.setattr(
        analysis_module, "build_graph_payload",
        lambda rows: {"kind": "candlestick_v2", "payload": {"n": len(rows)}},
    )

    started = service.run_full_analysis([soak_id])
    status = _wait_done(service, started["job_id"])

    assert status["summary"]["graph"]["kind"] == "candlestick_v2"


def test_unknown_soak_and_bad_job_id(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)
    missing = service.run_full_analysis(["nope"])
    assert isinstance(missing, OpError) and missing.code == "not_found"
    empty = service.run_full_analysis([])
    assert isinstance(empty, OpError) and empty.code == "invalid_request"
    traversal = service.get_analysis_status("../evil")
    assert isinstance(traversal, OpError) and traversal.code == "invalid_request"
