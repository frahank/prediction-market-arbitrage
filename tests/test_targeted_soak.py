# Evidence-pack post-processing over the soak_mini fixture.
from __future__ import annotations

import json
from pathlib import Path

from arbx.pairs.registry import PairSpec
from arbx.pairs.targeted_soak import build_evidence_pack

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "soak_mini"


def _pair() -> PairSpec:
    # soak_mini holds KXALIENS-27 rows; ids must match the recorded fetch ids.
    return PairSpec(
        pair_key="KXALIENS-27|0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        kalshi_market_id="KXALIENS-27",
        polymarket_condition_id="0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        polymarket_yes_token_id="107505882767731489358349912513945399560393482969656700824895970500493757150417",
        polymarket_no_token_id="7305630249804085635496399869905769372294302716159034447326228509068694952392",
        orientation="same", status="approved_for_paper",
        include_in_strategy_metrics=True, raw={},
    )


def test_evidence_pack_layout_complete(tmp_path: Path):
    evidence_dir = tmp_path / "KXALIENS-27" / "2026-07-03"
    manifest = build_evidence_pack(
        _pair(), evidence_dir, FIXTURE,
        fee_engine=None,          # flat path — no network in tests
        legacy_book_fix=True,     # soak_mini predates the book-semantics fix
        capture_stats={"run_id": "fixture", "paired_snapshots": 0, "probes": 0},
    )

    for name in ("dq_summary.json", "episodes.json", "survival_summary.json",
                 "liquidity_profile.json", "strike_map.html", "manifest.json"):
        assert (evidence_dir / name).exists(), name

    dq = json.loads((evidence_dir / "dq_summary.json").read_text())
    assert "recorder_health_passed" in dq
    assert "legacy fixture" in dq.get("note", "")

    episodes = json.loads((evidence_dir / "episodes.json").read_text())
    assert episodes["pair_key"].startswith("KXALIENS-27")
    assert episodes["edge_rows"] > 0
    assert isinstance(episodes["episodes"], list)

    survival = json.loads((evidence_dir / "survival_summary.json").read_text())
    assert survival["hybrid_kalshi_rest_poll"] is True
    assert "kalshi_rest_poll_stopgap" in survival["sub_110ms_validity"]
    assert "tier_distribution" in survival

    liquidity = json.loads((evidence_dir / "liquidity_profile.json").read_text())
    assert set(liquidity["venues"]) == {"kalshi", "polymarket"}
    for venue in liquidity["venues"].values():
        assert venue["rows"] > 0
        assert venue["median_top5_depth"] > 0
    assert liquidity["kalshi_to_poly_depth_ratio"] is not None

    assert manifest["files"] == sorted(manifest["files"])
    assert "dq_summary.json" in manifest["files"]


def test_pack_pulls_latest_rules_snapshot(tmp_path: Path):
    market_root = tmp_path / "KXALIENS-27"
    older = market_root / "2026-07-01"
    older.mkdir(parents=True)
    (older / "rules_snapshot.json").write_text('{"sha256": "abc"}')
    (older / "prescreen.json").write_text('{"score": "flagged"}')

    evidence_dir = market_root / "2026-07-03"
    build_evidence_pack(_pair(), evidence_dir, FIXTURE,
                        fee_engine=None, legacy_book_fix=True)
    assert json.loads((evidence_dir / "rules_snapshot.json").read_text())["sha256"] == "abc"
    assert (evidence_dir / "prescreen.json").exists()
