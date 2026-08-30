# Pairs tab route and API wiring tests.
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from scripts.run_ui import build_services, load_ui_config
from tests.test_pairs_service import GOOD_KEY, MORE_KEY, _fixture_service

from arbx.pairs.registry import load_pairs, verify_registry_integrity
from arbx.ui.app import ServiceRegistry, create_app

ROOT = Path(__file__).resolve().parents[1]


def _fixture_client(tmp_path: Path) -> tuple[TestClient, dict[str, Path]]:
    service, paths = _fixture_service(tmp_path)
    return TestClient(create_app(ServiceRegistry(pair_registry_service=service))), paths


def test_two_subtabs_render():
    client = TestClient(create_app(ServiceRegistry()))

    response = client.get("/pairs")

    assert response.status_code == 200
    assert "Pairs" in response.text
    assert "Active" in response.text
    assert "Needs approval" in response.text
    assert "pairs.js" in response.text
    assert "data-pair-tab=\"active\"" in response.text
    assert "data-pair-tab=\"approval\"" in response.text
    assert "data-review-form" in response.text


def test_summary_expansion_fields_present(tmp_path: Path):
    client, _ = _fixture_client(tmp_path)

    summary = client.get("/api/get_pair_summary", params={"pair_key": GOOD_KEY}).json()
    js = (ROOT / "src" / "arbx" / "ui" / "static" / "pairs.js").read_text(encoding="utf-8")

    assert summary["ok"] is True
    data = summary["data"]
    assert data["resolution_structure"] == "objective_single_event"
    assert data["equivalence"]["status"] == "verified_equivalent"
    assert data["orientation_confirmed"]["poly_yes_token_checked"] is True
    assert data["liquidity"]["venues"]["kalshi"]["rows"] == 3
    assert data["edge_behavior"]["episode_count"] == 2
    assert "evidence/KXGOOD-26/2026-07-05/ai_audit.md" in data["evidence_links"]
    assert "https://kalshi.example/KXGOOD-26" in data["evidence_links"]
    for label in ("Registry taxonomy", "Tail risks", "Orientation", "Liquidity", "Evidence links"):
        assert label in js
    assert "/docs-viewer?path=" in js


def test_repository_markdown_is_docs_viewer_readable():
    client = TestClient(create_app(build_services(load_ui_config())))

    response = client.get(
        "/api/read_doc",
        params={"path": "docs/FINAL_VERDICT.md"},
    ).json()

    assert response["ok"] is True
    assert response["data"]["path"] == "docs/FINAL_VERDICT.md"
    assert response["data"]["rendered_html"]


def test_review_flow_end_to_end_on_tmp_registry(tmp_path: Path):
    client, paths = _fixture_client(tmp_path)
    before = {pair.pair_key: pair for pair in load_pairs(paths["approved"])}[MORE_KEY]
    assert before.latest_decision == "needs_more_data"

    queue = client.get("/api/list_pairs_needing_approval").json()
    assert queue["ok"] is True
    assert MORE_KEY in {item["pair_key"] for item in queue["data"]["items"]}

    reviewed = client.post(
        "/api/review_pair",
        json={
            "pair_key": MORE_KEY,
            "decision": "approve",
            "reviewer": "operator",
            "notes": "reviewed evidence and targeted soak for UI flow",
            "confirm": f"APPROVE {MORE_KEY}",
        },
    ).json()

    assert reviewed["ok"] is True
    assert reviewed["data"]["latest_decision"]["decision"] == "approve"
    assert verify_registry_integrity(paths["approved"]).audited
    after = {pair.pair_key: pair for pair in load_pairs(paths["approved"])}[MORE_KEY]
    assert after.latest_decision == "approve"
    assert len(after.decision_log) == len(before.decision_log) + 1
