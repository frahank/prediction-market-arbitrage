from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "review_pair_candidates.py"
SPEC = importlib.util.spec_from_file_location("review_pair_candidates", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)

NOW = datetime(2026, 6, 23, 20, 0, tzinfo=timezone.utc)


def candidate() -> dict:
    return {
        "pair_key": "KXKRAKEN|POLY-KRAKEN",
        "kalshi_market_id": "KXKRAKEN",
        "polymarket_market_id": "POLY-KRAKEN",
        "kalshi_question": "Will Kraken complete an IPO by December 31, 2026?",
        "polymarket_question": "Will Kraken complete its IPO by December 31, 2026?",
        "orientation": "same",
        "confidence": 0.93,
        "status": "candidate",
        "auto_approved": False,
        "warnings": ["manual_resolution_review_required"],
        "evidence": {
            "title_similarity": 0.94,
            "rule_similarity": 0.91,
            "orientation_basis": "matching_outcome_labels",
        },
        "kalshi_close_time": "2027-01-01T05:00:00+00:00",
        "polymarket_close_time": "2027-01-01T05:00:00+00:00",
        "kalshi_identifiers": {"ticker": "KXKRAKEN"},
        "polymarket_identifiers": {"yes_token_id": "yes-token"},
    }


def market(venue: str, market_id: str, question: str) -> dict:
    return {
        "venue": venue,
        "market_id": market_id,
        "question": question,
        "rules": "Resolves Yes only if the IPO is completed by the stated deadline.",
        "close_time": "2027-01-01T05:00:00+00:00",
        "liquidity": 1250.0,
        "volume_24h": 450.0,
        "yes_depth": 175.0,
        "no_depth": 160.0,
    }


def context() -> dict:
    item = candidate()
    return {
        "kalshi": market("kalshi", item["kalshi_market_id"], item["kalshi_question"]),
        "polymarket": market(
            "polymarket",
            item["polymarket_market_id"],
            item["polymarket_question"],
        ),
    }


def write_candidate_queue(path: Path, candidates: list[dict]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "queue_type": "candidate_only",
                "auto_approval_enabled": False,
                "candidates": candidates,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def write_discovery(path: Path, venue: str, markets: list[dict]) -> None:
    path.write_text(
        json.dumps({"venue": venue, "markets": markets}),
        encoding="utf-8",
    )


def test_committed_registry_has_only_verified_strategy_pairs_and_is_paper_only():
    registry = review.load_approved_registry(ROOT / "configs" / "pairs.approved.yaml")

    assert registry["registry_type"] == "approved_for_paper_only"
    assert registry["live_trading_allowed"] is False
    # 30 approved in June; four basis pairs and three completed World Cup
    # continent pairs have since been archived.
    assert len(registry["pairs"]) == 23
    for pair in registry["pairs"]:
        assert pair["status"] == "approved_for_paper"
        assert pair["human_approved"] is True
    legacy = [p for p in registry["pairs"] if not p.get("contract_equivalent")]
    assert legacy == []
    strategy = [p for p in registry["pairs"] if p.get("contract_equivalent")]
    assert len(strategy) == 23
    assert all(p["simulation_scope"] == "strategy" for p in strategy)
    assert all(p["include_in_strategy_metrics"] is True for p in strategy)
    assert len(review.load_approved_pairs(ROOT / "configs" / "pairs.approved.yaml")) == 23


def test_candidate_render_shows_required_human_review_information():
    output = review.render_candidate(candidate(), context())

    assert "Will Kraken complete an IPO" in output
    assert "Rules:" in output
    assert "2027-01-01T05:00:00+00:00" in output
    assert "Orientation: same" in output
    assert "Liquidity: 1250.0" in output
    assert "YES/NO depth: 175.0 / 160.0" in output
    assert "manual_resolution_review_required" in output
    assert "orientation_basis" in output


def test_approval_requires_exact_explicit_confirmation():
    with pytest.raises(review.PairApprovalError, match="confirmation"):
        review.apply_review_decision(
            review.new_approved_registry(),
            candidate(),
            context(),
            decision="approve",
            reviewer="Human Reviewer",
            notes="The rules and deadlines describe the same event.",
            confirmation="yes",
            reviewed_at=NOW,
        )


def test_approval_requires_both_discovery_records():
    with pytest.raises(review.PairApprovalError, match="Polymarket discovery record"):
        review.apply_review_decision(
            review.new_approved_registry(),
            candidate(),
            {"kalshi": context()["kalshi"], "polymarket": None},
            decision="approve",
            reviewer="Human Reviewer",
            notes="The rules and deadlines describe the same event.",
            confirmation=review.APPROVE_CONFIRMATION,
            reviewed_at=NOW,
        )


def test_approval_records_metadata_snapshot_and_runner_safe_status(tmp_path):
    registry = review.apply_review_decision(
        review.new_approved_registry(),
        candidate(),
        context(),
        decision="approve",
        reviewer="Human Reviewer",
        notes="Rules, deadline, outcomes, and resolution source are equivalent.",
        confirmation=review.APPROVE_CONFIRMATION,
        reviewed_at=NOW,
    )
    path = tmp_path / "pairs.approved.yaml"
    review.save_approved_registry(path, registry)

    loaded = review.load_approved_pairs(path)
    assert len(loaded) == 1
    approved = loaded[0]
    assert approved["status"] == "approved_for_paper"
    assert approved["human_approved"] is True
    assert approved["reviewer"] == "Human Reviewer"
    assert approved["approved_at"] == "2026-06-23T20:00:00+00:00"
    assert approved["review_snapshot"]["kalshi"]["rules"]
    assert approved["review_snapshot"]["polymarket"]["liquidity"] == 1250.0
    assert registry["review_decisions"][0]["decision"] == "approved"


def test_rejection_is_persisted_but_never_returned_to_runner():
    registry = review.apply_review_decision(
        review.new_approved_registry(),
        candidate(),
        context(),
        decision="reject",
        reviewer="Human Reviewer",
        notes="Settlement rules use different source.",
        confirmation=review.REJECT_CONFIRMATION,
        reviewed_at=NOW,
    )

    assert registry["pairs"] == []
    assert registry["review_decisions"] == [
        {
            "pair_key": "KXKRAKEN|POLY-KRAKEN",
            "decision": "rejected",
            "reviewer": "Human Reviewer",
            "reviewer_notes": "Settlement rules use different source.",
            "reviewed_at": "2026-06-23T20:00:00+00:00",
        }
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("live_trading_allowed", True, "prohibit live trading"),
        ("registry_type", "candidate_only", "approved_for_paper_only"),
    ],
)
def test_registry_rejects_non_paper_runner_input(field, value, message):
    registry = review.new_approved_registry()
    registry[field] = value

    with pytest.raises(review.PairApprovalError, match=message):
        review.validate_approved_registry(registry)


def test_registry_rejects_pair_without_human_approval():
    registry = review.apply_review_decision(
        review.new_approved_registry(),
        candidate(),
        context(),
        decision="approve",
        reviewer="Human Reviewer",
        notes="Rules, deadline, outcomes, and resolution source are equivalent.",
        confirmation=review.APPROVE_CONFIRMATION,
        reviewed_at=NOW,
    )
    registry["pairs"][0]["human_approved"] = False

    with pytest.raises(review.PairApprovalError, match="human approved"):
        review.validate_approved_registry(registry)


def test_cli_approval_round_trip_is_explicit_and_offline(tmp_path):
    item = candidate()
    candidates_path = tmp_path / "pairs.candidates.yaml"
    approved_path = tmp_path / "pairs.approved.yaml"
    kalshi_path = tmp_path / "kalshi.json"
    polymarket_path = tmp_path / "polymarket.json"
    write_candidate_queue(candidates_path, [item])
    review.save_approved_registry(approved_path, review.new_approved_registry())
    write_discovery(kalshi_path, "kalshi", [context()["kalshi"]])
    write_discovery(polymarket_path, "polymarket", [context()["polymarket"]])

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--candidates",
            str(candidates_path),
            "--approved",
            str(approved_path),
            "--kalshi-discovery",
            str(kalshi_path),
            "--polymarket-discovery",
            str(polymarket_path),
            "--pair-key",
            item["pair_key"],
            "--decision",
            "approve",
            "--reviewer",
            "Human Reviewer",
            "--notes",
            "Rules, deadline, outcomes, and resolution source are equivalent.",
            "--confirmation",
            review.APPROVE_CONFIRMATION,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Recorded approve decision" in completed.stdout
    assert len(review.load_approved_pairs(approved_path)) == 1


def test_empty_candidate_queue_is_a_clean_no_op(tmp_path):
    candidates_path = tmp_path / "pairs.candidates.yaml"
    write_candidate_queue(candidates_path, [])

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--candidates",
            str(candidates_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "No candidate pairs" in completed.stdout
