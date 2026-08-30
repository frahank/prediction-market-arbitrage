# PairRegistryService reads and guarded review.
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from arbx.pairs.registry import (
    load_pairs,
    verify_registry_integrity,
    write_registry_integrity,
)
from arbx.services.pairs import PairRegistryServiceImpl
from arbx.ui.app import ServiceRegistry, create_app
from arbx.ui.envelope import OpError

GOOD_KEY = "KXGOOD-26|0xgood"
MORE_KEY = "KXMORE-26|0xmore"
BAD_KEY = "KXBAD-26|0xbad"
CONN_KEY = "KXCONN-26|0xconn"
CAND_KEY = "KXCAND-26|0xcand"
REJECTED_KEY = "KXREJECT-26|0xreject"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pair_entry(
    *,
    pair_key: str,
    market: str,
    display_name: str,
    equivalence_status: str,
    decision_log: list[dict] | None = None,
    contract_equivalent: bool = True,
    simulation_scope: str = "strategy",
) -> dict:
    return {
        "pair_key": pair_key,
        "display_name": display_name,
        "kalshi_market_id": market,
        "polymarket_market_id": pair_key.split("|", 1)[1],
        "orientation": "same",
        "status": "approved_for_paper",
        "contract_equivalent": contract_equivalent,
        "simulation_scope": simulation_scope,
        "include_in_strategy_metrics": True,
        "kalshi_identifiers": {"event_ticker": market.rsplit("-", 1)[0], "market_ticker": market},
        "polymarket_identifiers": {
            "condition_id": pair_key.split("|", 1)[1],
            "yes_token_id": f"yes-{market}",
            "no_token_id": f"no-{market}",
            "market_slug": f"{market.lower()}-market",
        },
        "review_snapshot": {
            "kalshi": {"url": f"https://kalshi.example/{market}", "question": display_name},
            "polymarket": {"url": f"https://poly.example/{market}", "question": display_name},
        },
        "taxonomy": {
            "resolution_structure": "objective_single_event",
            "grouping_alignment": "n/a",
            "date_cutoff_delta_hours": 0.0,
            "time_to_resolution_days": 10.0,
            "persistence_cause": "none",
        },
        "equivalence": {
            "status": equivalence_status,
            "audited_at": "2026-07-05",
            "auditor": "fixture-auditor",
            "notes": "fixture equivalence record",
            "tail_risks": [],
        },
        "orientation_confirmed": {
            "kalshi_yes_meaning": display_name,
            "poly_yes_token_checked": True,
        },
        "decision_log": decision_log or [],
    }


def _fixture_service(tmp_path: Path) -> tuple[PairRegistryServiceImpl, dict[str, Path]]:
    configs = tmp_path / "configs"
    evidence = tmp_path / "evidence"
    approved = configs / "pairs.approved.yaml"
    archived = configs / "pairs.archived.yaml"
    candidates = configs / "pairs.candidates.yaml"
    configs.mkdir()

    approved.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2.1,
                "pairs": [
                    _pair_entry(
                        pair_key=GOOD_KEY,
                        market="KXGOOD-26",
                        display_name="Good equivalent pair",
                        equivalence_status="verified_equivalent",
                    ),
                    _pair_entry(
                        pair_key=MORE_KEY,
                        market="KXMORE-26",
                        display_name="Needs more data pair",
                        equivalence_status="verified_equivalent",
                        decision_log=[
                            {
                                "at": "2026-07-04T00:00:00+00:00",
                                "decision": "needs_more_data",
                                "rationale": "targeted soak missing",
                                "auditor": "fixture",
                            }
                        ],
                    ),
                    _pair_entry(
                        pair_key=BAD_KEY,
                        market="KXBAD-26",
                        display_name="Unreviewed pair",
                        equivalence_status="unreviewed",
                    ),
                    _pair_entry(
                        pair_key=CONN_KEY,
                        market="KXCONN-26",
                        display_name="Connectivity only pair",
                        equivalence_status="verified_equivalent",
                        contract_equivalent=False,
                        simulation_scope="connectivity_only",
                    ),
                    _pair_entry(
                        pair_key=REJECTED_KEY,
                        market="KXREJECT-26",
                        display_name="Rejected but readable pair",
                        equivalence_status="verified_equivalent",
                        decision_log=[
                            {
                                "at": "2026-07-04T01:00:00+00:00",
                                "decision": "reject",
                                "rationale": "liquidity vanished",
                                "auditor": "fixture",
                            }
                        ],
                    ),
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    write_registry_integrity(approved)

    archived.write_text(
        yaml.safe_dump({"schema_version": 2.1, "registry_type": "archived_pairs", "pairs": []}),
        encoding="utf-8",
    )
    write_registry_integrity(archived)

    candidates.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "queue_type": "candidate_only",
                "candidates": [
                    {
                        "pair_key": CAND_KEY,
                        "display_name": "Candidate pair",
                        "kalshi_market_id": "KXCAND-26",
                        "orientation": "same",
                        "status": "candidate",
                        "include_in_strategy_metrics": False,
                        "polymarket_identifiers": {
                            "condition_id": "0xcand",
                            "yes_token_id": "yes-cand",
                            "no_token_id": "no-cand",
                        },
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    pack = evidence / "KXGOOD-26" / "2026-07-05"
    _write_json(pack / "liquidity_profile.json", {"venues": {"kalshi": {"rows": 3}}, "depth_ratio": 1.2})
    _write_json(
        pack / "episodes.json",
        {
            "pair_key": GOOD_KEY,
            "edge_rows": 7,
            "episodes": [
                {"bucket": "transient_candidate", "duration_s": 30},
                {"bucket": "basis_suspect", "duration_s": 90},
            ],
        },
    )
    _write_json(pack / "survival_summary.json", {"max_survived_through_ms": 500, "probe_ladders": 1})
    _write_json(pack / "rules_snapshot.json", {"sha256": "abc"})
    (pack / "ai_audit.md").write_text("# Audit\n", encoding="utf-8")

    service = PairRegistryServiceImpl(approved, candidates, archived, evidence)
    return service, {"approved": approved, "archived": archived, "candidates": candidates}


def _assert_ok(result):
    assert not isinstance(result, OpError), result
    return result


def test_active_and_needing_approval_partition(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)

    active = _assert_ok(service.list_active_pairs(limit=2))
    assert len(active["items"]) == 2
    assert active["next_cursor"]
    second_page = _assert_ok(service.list_active_pairs(cursor=active["next_cursor"], limit=20))
    active_keys = {item["pair_key"] for item in active["items"] + second_page["items"]}
    assert active_keys == {GOOD_KEY, MORE_KEY, BAD_KEY, CONN_KEY, REJECTED_KEY}

    needing = _assert_ok(service.list_pairs_needing_approval(limit=50))
    needing_keys = {item["pair_key"] for item in needing["items"]}
    assert needing_keys == {CAND_KEY, MORE_KEY, BAD_KEY}
    assert GOOD_KEY not in needing_keys
    assert REJECTED_KEY not in needing_keys


def test_summary_composes_registry_and_evidence(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)

    summary = _assert_ok(service.get_pair_summary(GOOD_KEY))
    data = summary.to_dict()

    assert data["display_name"] == "Good equivalent pair"
    assert data["equivalence"]["status"] == "verified_equivalent"
    assert data["liquidity"]["venues"]["kalshi"]["rows"] == 3
    assert data["edge_behavior"]["edge_rows"] == 7
    assert data["edge_behavior"]["episode_count"] == 2
    assert data["edge_behavior"]["bucket_counts"] == {
        "basis_suspect": 1,
        "transient_candidate": 1,
    }
    assert data["edge_behavior"]["survival"]["max_survived_through_ms"] == 500
    assert "evidence/KXGOOD-26/2026-07-05/ai_audit.md" in data["evidence_links"]
    assert "https://kalshi.example/KXGOOD-26" in data["evidence_links"]
    assert data["simulation_scope"] == "strategy"
    assert data["contract_equivalent"] == "verified_equivalent"


def test_list_rows_are_compact_and_detail_composes_evidence(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)

    listed = _assert_ok(service.list_active_pairs(limit=50))
    good_row = [item for item in listed["items"] if item["pair_key"] == GOOD_KEY][0]
    # list rows never read evidence packs (loaded only on selection)
    assert good_row["liquidity"] is None
    assert good_row["edge_behavior"] is None
    assert good_row["evidence_links"] == []
    # registry fields the list UI renders are still present
    assert good_row["display_name"] == "Good equivalent pair"
    assert good_row["equivalence"]["status"] == "verified_equivalent"

    detail = _assert_ok(service.get_pair_summary(GOOD_KEY)).to_dict()
    assert detail["liquidity"] is not None
    assert detail["evidence_links"]


def test_latest_evidence_pack_picks_newest_date_across_roots(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)
    # An OLDER pack in a second root keyed by pair_key, which sorts AFTER the
    # kalshi-id root holding the 2026-07-05 pack: full-path sorting would let
    # the root name outrank the date and pick this stale pack. The newest
    # DATED pack must win regardless of which root it lives in.
    stale = tmp_path / "evidence" / GOOD_KEY / "2026-07-01"
    _write_json(stale / "liquidity_profile.json", {"depth_ratio": 0.1, "stale": True})

    detail = _assert_ok(service.get_pair_summary(GOOD_KEY)).to_dict()

    assert detail["liquidity"]["venues"]["kalshi"]["rows"] == 3  # the 2026-07-05 pack
    assert "stale" not in detail["liquidity"]
    assert any("2026-07-05" in link for link in detail["evidence_links"])


def test_api_routes_serialize_pair_summaries(tmp_path: Path):
    service, _ = _fixture_service(tmp_path)
    client = TestClient(create_app(ServiceRegistry(pair_registry_service=service)))

    listed = client.get("/api/list_active_pairs", params={"limit": 1}).json()
    summary = client.get("/api/get_pair_summary", params={"pair_key": GOOD_KEY}).json()

    assert listed["ok"] is True
    assert listed["data"]["items"][0]["pair_key"]
    assert summary["ok"] is True
    assert summary["data"]["pair_key"] == GOOD_KEY
    assert isinstance(summary["data"]["evidence_links"], list)


def test_review_requires_exact_confirmation(tmp_path: Path):
    service, paths = _fixture_service(tmp_path)

    result = service.review_pair(
        GOOD_KEY,
        decision="approve",
        reviewer="operator",
        notes="targeted soak and evidence reviewed",
        confirm=f"APPROVE {GOOD_KEY} ",
    )

    assert isinstance(result, OpError)
    assert result.code == "invalid_request"
    assert result.details == {"expected": f"APPROVE {GOOD_KEY}"}
    assert load_pairs(paths["approved"])[0].decision_log == ()


def test_approve_blocked_without_equivalence(tmp_path: Path):
    service, paths = _fixture_service(tmp_path)

    result = service.review_pair(
        BAD_KEY,
        decision="approve",
        reviewer="operator",
        notes="attempted approval should be blocked",
        confirm=f"APPROVE {BAD_KEY}",
    )

    assert isinstance(result, OpError)
    assert result.code == "safety_violation"
    bad = {pair.pair_key: pair for pair in load_pairs(paths["approved"])}[BAD_KEY]
    assert bad.equivalence.status == "unreviewed"
    assert bad.decision_log == ()


def test_decision_appends_and_rehashes(tmp_path: Path):
    service, paths = _fixture_service(tmp_path)

    result = _assert_ok(
        service.review_pair(
            GOOD_KEY,
            decision="approve",
            reviewer="operator",
            notes="targeted soak and evidence reviewed",
            confirm=f"APPROVE {GOOD_KEY}",
        )
    )

    assert result["latest_decision"]["decision"] == "approve"
    assert verify_registry_integrity(paths["approved"]).audited
    good = {pair.pair_key: pair for pair in load_pairs(paths["approved"])}[GOOD_KEY]
    assert good.latest_decision == "approve"
    assert good.decision_log[-1]["auditor"] == "operator"
    assert good.include_in_strategy_metrics is True


def test_connectivity_pair_cannot_become_equivalent(tmp_path: Path):
    service, paths = _fixture_service(tmp_path)
    before = yaml.safe_load(paths["approved"].read_text(encoding="utf-8"))
    conn_before = [p for p in before["pairs"] if p["pair_key"] == CONN_KEY][0]
    assert conn_before["equivalence"]["status"] == "verified_equivalent"
    assert conn_before["contract_equivalent"] is False

    invalid = service.review_pair(
        CONN_KEY,
        decision="verified_equivalent",
        reviewer="operator",
        notes="try to relabel through review",
        confirm=f"VERIFIED_EQUIVALENT {CONN_KEY}",
    )
    blocked = service.review_pair(
        CONN_KEY,
        decision="approve",
        reviewer="operator",
        notes="try to approve connectivity-only pair",
        confirm=f"APPROVE {CONN_KEY}",
    )
    rejected = _assert_ok(
        service.review_pair(
            CONN_KEY,
            decision="reject",
            reviewer="operator",
            notes="connectivity-only pair remains excluded",
            confirm=f"REJECT {CONN_KEY}",
        )
    )

    assert isinstance(invalid, OpError)
    assert invalid.code == "invalid_request"
    assert isinstance(blocked, OpError)
    assert blocked.code == "safety_violation"
    assert rejected["latest_decision"]["decision"] == "reject"
    after = yaml.safe_load(paths["approved"].read_text(encoding="utf-8"))
    conn_after = [p for p in after["pairs"] if p["pair_key"] == CONN_KEY][0]
    assert conn_after["equivalence"]["status"] == "verified_equivalent"
    assert conn_after["contract_equivalent"] is False
    assert conn_after["simulation_scope"] == "connectivity_only"


def test_unreviewed_candidate_cannot_be_approved(tmp_path: Path):
    """The safety invariant: a candidate whose equivalence is not
    verified/tail-documented can never be promoted into the approved registry."""
    service, paths = _fixture_service(tmp_path)
    approved_before = load_pairs(paths["approved"])

    result = service.review_pair(
        CAND_KEY,  # fixture candidate has no equivalence block -> unreviewed
        decision="approve",
        reviewer="operator",
        notes="tried to approve an unreviewed candidate",
        confirm=f"APPROVE {CAND_KEY}",
    )

    assert isinstance(result, OpError)
    assert result.code == "safety_violation"
    # approved registry untouched, integrity intact
    assert {p.pair_key for p in load_pairs(paths["approved"])} == {p.pair_key for p in approved_before}
    assert verify_registry_integrity(paths["approved"]).audited


def test_candidate_reject_files_to_archive(tmp_path: Path):
    """Rejecting a candidate is a safe write: it lands in the archived registry
    (never the approved one) with the decision recorded."""
    service, paths = _fixture_service(tmp_path)

    result = _assert_ok(
        service.review_pair(
            CAND_KEY,
            decision="reject",
            reviewer="operator",
            notes="candidate lacks completed equivalence evidence",
            confirm=f"REJECT {CAND_KEY}",
        )
    )

    assert result["status"] == "archived"
    assert result["promoted"] is False
    assert CAND_KEY not in {p.pair_key for p in load_pairs(paths["approved"])}
    assert CAND_KEY in {p.pair_key for p in load_pairs(paths["archived"])}
    assert verify_registry_integrity(paths["archived"]).audited


def test_archive_uses_existing_archive_workflow(tmp_path: Path):
    service, paths = _fixture_service(tmp_path)

    result = _assert_ok(
        service.review_pair(
            MORE_KEY,
            decision="archive",
            reviewer="operator",
            notes="no longer useful for paper review",
            confirm=f"ARCHIVE {MORE_KEY}",
        )
    )

    assert result["status"] == "archived"
    assert result["archived_path"] == "configs/pairs.archived.yaml"
    assert verify_registry_integrity(paths["approved"]).audited
    assert verify_registry_integrity(paths["archived"]).audited
    remaining_keys = {pair.pair_key for pair in load_pairs(paths["approved"])}
    archived_keys = {pair.pair_key for pair in load_pairs(paths["archived"])}
    assert MORE_KEY not in remaining_keys
    assert MORE_KEY in archived_keys
    archived_summary = _assert_ok(service.get_pair_summary(MORE_KEY)).to_dict()
    assert archived_summary["status"] == "archived"
