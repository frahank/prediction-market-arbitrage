# Scope: BOT_RUNTIME - M2-T6 Paper tab route/API wiring tests.
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from arbx.ui.app import ServiceRegistry, create_app

ROOT = Path(__file__).resolve().parents[1]


class _PairService:
    def list_active_pairs(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {
            "items": [
                {
                    "pair_key": "KXONE|0x1",
                    "display_name": "One",
                    "include_in_strategy_metrics": True,
                },
                {
                    "pair_key": "KXTWO|0x2",
                    "display_name": "Two",
                    "include_in_strategy_metrics": True,
                },
            ],
            "next_cursor": None,
        }

    def list_pairs_needing_approval(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": [], "next_cursor": None}

    def list_archived_pairs(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": [], "next_cursor": None}

    def get_pair_summary(self, pair_key: str) -> dict[str, Any]:
        return {"pair_key": pair_key}

    def review_pair(
        self,
        pair_key: str,
        decision: str,
        reviewer: str,
        notes: str,
        confirm: str,
    ) -> dict[str, Any]:
        return {"pair_key": pair_key, "decision": decision}


class _ScannerService:
    def __init__(self) -> None:
        self.started: dict[str, Any] | None = None
        self.running = False

    def start_scanner(
        self,
        pairs: list[str] | str | None = None,
        pair_keys: list[str] | str | None = None,
        record: bool | str = True,
        edges_only: bool | str = False,
        batch: int | str | None = None,
        batch_size: int | str | None = None,
        tick: float | str | None = None,
        tick_s: float | str | None = None,
        confirm_survival_ms: int | str | None = None,
    ) -> dict[str, Any]:
        self.started = {
            "pairs": pairs,
            "pair_keys": pair_keys,
            "record": record,
            "edges_only": edges_only,
            "batch_size": batch_size if batch_size is not None else batch,
            "tick_s": tick_s if tick_s is not None else tick,
            "confirm_survival_ms": confirm_survival_ms,
        }
        self.running = True
        return {
            "run_id": "scan_20260705-160000",
            "soak_id": "scan_20260705-160000_EDGES" if edges_only else "scan_20260705-160000",
            "soak_path": "/tmp/scan_20260705-160000",
            "pair_count": len(pair_keys or ["KXONE|0x1", "KXTWO|0x2"]),
            "effective_cadence_s": 1.0,
            "started_at": "2026-07-05T16:00:00+00:00",
        }

    def stop_scanner(self) -> dict[str, Any]:
        self.running = False
        return {
            "run_id": "scan_20260705-160000",
            "stopped_at": "2026-07-05T16:01:00+00:00",
            "summary": {"ticks": 3},
        }

    def get_scanner_status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "state": "running" if self.running else "idle",
            "run_id": "scan_20260705-160000" if self.running else None,
            "soak_id": "scan_20260705-160000_EDGES" if self.running else None,
            "pair_count": 2 if self.running else 0,
            "batch_size": 20,
            "tick_s": 1.0,
            "effective_cadence_s": 1.0 if self.running else 0.0,
            "ticks": 3 if self.running else 0,
            "snapshots": 6 if self.running else 0,
            "arbs_detected": 1 if self.running else 0,
            "qualifying": 1 if self.running else 0,
            "fetch_errors": 0,
            "last_tick_at": "2026-07-05T16:00:05+00:00" if self.running else None,
            "edges_written": 4 if self.running else 0,
            "return_code": None,
            "last_error": None,
        }


class _DataService:
    def list_soaks(
        self,
        cursor: str | None = None,
        limit: int = 50,
        edges_only: bool | str | None = None,
    ) -> dict[str, Any]:
        return {
            "items": [
                {
                    "soak_id": "scan_20260705-160000",
                    "label": "fixture scan",
                    "started_at": "2026-07-05T16:00:00+00:00",
                    "row_counts": {"book": 2, "opportunities": 1, "edges": 1},
                }
            ],
            "next_cursor": None,
        }

    def get_soak(self, soak_id: str) -> dict[str, Any]:
        return {"meta": {"soak_id": soak_id}, "dq": None}

    def list_soak_rows(
        self,
        soak_id: str,
        kind: str = "edges",
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {"items": [], "next_cursor": None, "kind": kind}


class _AnalysisService:
    def run_full_analysis(self, soak_ids: list[str] | str | None = None) -> dict[str, Any]:
        return {"job_id": "analysis_20260705-160100_1"}

    def get_analysis_status(self, job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "state": "done",
            "progress": {"stage": "summary", "pct": 100.0},
            "summary": {
                "soak_ids": ["scan_20260705-160000"],
                "generated_at": "2026-07-05T16:01:00+00:00",
                "profit_score": 0.25,
                "min_latency_needed_ms": 250.0,
                "chance_of_profit": 0.2,
                "chance_of_loss": 0.8,
                "would_have_made_money_live": {
                    "verdict": "marginal",
                    "rationale": ["fixture rationale"],
                    "basis": "model_v1",
                },
                "dq": {"passed": True, "detail": {}},
                "fee_sensitivity": {"real": 1},
                "per_pair": [],
                "sample": {"snapshots": 3, "qualifying_rows": 1, "soak_hours": 0.1},
                "graph": {"kind": "edge_timeline_v1", "payload": {"series": {}}},
                "caveats": ["fixture caveat"],
            },
            "error": None,
        }


class _TestSuiteService:
    def run_test_suite(self) -> dict[str, Any]:
        return {"job_id": "test_20260705-160200"}

    def get_test_suite_result(self, job_id: str) -> dict[str, Any]:
        return {
            "state": "done",
            "result": {
                "passed": True,
                "total": 10,
                "failures": 0,
                "errors": 0,
                "duration_s": 1.2,
                "message": "The bot is working properly.",
                "detail_path": "reports/test_runs/test_20260705-160200.txt",
            },
            "error": None,
            "note": None,
        }

    def get_test_run_detail(self, path: str) -> dict[str, str]:
        return {"path": path, "text": "10 passed in 1.20s"}


def _client(scanner: _ScannerService | None = None) -> TestClient:
    return TestClient(
        create_app(
            ServiceRegistry(
                pair_registry_service=_PairService(),
                scanner_controller=scanner or _ScannerService(),
                data_service=_DataService(),
                analysis_service=_AnalysisService(),
                test_suite_runner=_TestSuiteService(),
                paper_defaults={"batch_size": 20, "tick_s": 1.0},
            )
        )
    )


def test_panel_renders_without_soak_button():
    response = _client().get("/paper")

    assert response.status_code == 200
    assert "paper.js" in response.text
    assert "Scanner" in response.text
    assert "Run Full Analysis" in response.text
    assert "Run Test Suite" in response.text
    assert "run soak" not in response.text.lower()
    assert "data-run-soak" not in response.text


def test_start_stop_scanner_end_to_end():
    scanner = _ScannerService()
    client = _client(scanner)

    started = client.post(
        "/api/start_scanner",
        json={
            "pair_keys": ["KXONE|0x1", "KXTWO|0x2"],
            "record": True,
            "edges_only": False,
            "batch_size": 2,
            "tick_s": 1.0,
        },
    ).json()
    status = client.get("/api/get_scanner_status").json()
    stopped = client.post("/api/stop_scanner", json={}).json()

    assert started["ok"] is True
    assert started["data"]["soak_id"] == "scan_20260705-160000"
    assert scanner.started is not None
    assert scanner.started["pair_keys"] == ["KXONE|0x1", "KXTWO|0x2"]
    assert status["ok"] is True
    assert status["data"]["running"] is True
    assert status["data"]["arbs_detected"] == 1
    assert stopped["ok"] is True
    assert stopped["data"]["summary"]["ticks"] == 3


def test_edges_only_forces_record_in_api():
    scanner = _ScannerService()
    client = _client(scanner)
    js = (ROOT / "src" / "arbx" / "ui" / "static" / "paper.js").read_text(encoding="utf-8")

    started = client.post(
        "/api/start_scanner",
        json={"record": True, "edges_only": True, "batch_size": 1, "tick_s": 1.0},
    ).json()

    assert "record: edgesOnly ? true : recordToggle.checked" in js
    assert "recordToggle.checked = true" in js
    assert started["ok"] is True
    assert scanner.started is not None
    assert scanner.started["record"] is True
    assert scanner.started["edges_only"] is True
    assert started["data"]["soak_id"].endswith("_EDGES")


def test_analysis_flow_end_to_end_on_fixture():
    client = _client()

    soaks = client.get("/api/list_soaks").json()
    started = client.post(
        "/api/run_full_analysis",
        json={"soak_ids": [soaks["data"]["items"][0]["soak_id"]]},
    ).json()
    status = client.get(
        "/api/get_analysis_status",
        params={"job_id": started["data"]["job_id"]},
    ).json()

    assert soaks["ok"] is True
    assert started["ok"] is True
    assert status["ok"] is True
    assert status["data"]["state"] == "done"
    summary = status["data"]["summary"]
    assert summary["profit_score"] == 0.25
    assert summary["would_have_made_money_live"]["verdict"] == "marginal"
    assert summary["dq"]["passed"] is True
    assert summary["graph"]["kind"] == "edge_timeline_v1"


def test_testsuite_flow():
    client = _client()

    started = client.post("/api/run_test_suite", json={}).json()
    status = client.get(
        "/api/get_test_suite_result",
        params={"job_id": started["data"]["job_id"]},
    ).json()
    detail = client.get(
        "/api/get_test_run_detail",
        params={"path": status["data"]["result"]["detail_path"]},
    ).json()

    assert started["ok"] is True
    assert status["ok"] is True
    assert status["data"]["result"]["passed"] is True
    assert status["data"]["result"]["message"] == "The bot is working properly."
    assert detail["ok"] is True
    assert "10 passed" in detail["data"]["text"]
