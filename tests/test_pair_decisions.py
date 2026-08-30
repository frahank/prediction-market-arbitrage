# Pair-decision workflow: append-only log, strategy gate, archive.
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arbx.pairs.equivalence import archive_pair, record_decision
from arbx.pairs.registry import (
    load_pairs,
    verify_registry_integrity,
    write_registry_integrity,
)

V2_MINI = """\
schema_version: 2
pairs:
- pair_key: KXGOOD-26|0xaaa
  kalshi_market_id: KXGOOD-26
  orientation: same
  status: approved_for_paper
  include_in_strategy_metrics: true
  polymarket_identifiers: {condition_id: '0xaaa', yes_token_id: '1', no_token_id: '2'}
  equivalence: {status: verified_equivalent, notes: audited}
  decision_log: []
- pair_key: KXBAD-26|0xbbb
  kalshi_market_id: KXBAD-26
  orientation: same
  status: approved_for_paper
  include_in_strategy_metrics: true
  polymarket_identifiers: {condition_id: '0xbbb', yes_token_id: '3', no_token_id: '4'}
  equivalence: {status: unreviewed}
  decision_log: []
"""


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "pairs.approved.yaml"
    path.write_text(V2_MINI, encoding="utf-8")
    write_registry_integrity(path)
    return path


def test_decision_appends_and_rehashes(tmp_path: Path):
    reg = _registry(tmp_path)
    record_decision(reg, "KXGOOD-26", "needs_more_data", "thin evidence", "tester")
    record_decision(reg, "KXGOOD-26", "approve", "targeted soak clean", "tester")

    assert verify_registry_integrity(reg).audited  # sidecar refreshed
    spec = {s.kalshi_market_id: s for s in load_pairs(reg)}["KXGOOD-26"]
    assert [d["decision"] for d in spec.decision_log] == ["needs_more_data", "approve"]
    assert spec.latest_decision == "approve"
    assert all(d["at"] and d["rationale"] for d in spec.decision_log)

    with pytest.raises(ValueError):
        record_decision(reg, "KXGOOD-26", "yolo", "r", "tester")
    with pytest.raises(ValueError):
        record_decision(reg, "KXGOOD-26", "approve", "  ", "tester")
    with pytest.raises(KeyError):
        record_decision(reg, "NOPE", "approve", "r", "tester")

    # tampering breaks the integrity gate — decisions refuse to write
    reg.write_text(reg.read_text() + "# sneaky\n", encoding="utf-8")
    from arbx.pairs.registry import RegistryIntegrityError
    with pytest.raises(RegistryIntegrityError):
        record_decision(reg, "KXGOOD-26", "approve", "r", "tester")


def test_unapproved_pair_excluded_from_strategy(tmp_path: Path):
    reg = _registry(tmp_path)
    specs = {s.kalshi_market_id: s for s in load_pairs(reg)}
    # YAML flag true, but no approve decision yet → denied
    assert specs["KXGOOD-26"].include_in_strategy_metrics is False

    record_decision(reg, "KXGOOD-26", "approve", "audited + soak clean", "tester")
    record_decision(reg, "KXBAD-26", "approve", "should not matter", "tester")
    specs = {s.kalshi_market_id: s for s in load_pairs(reg)}
    # verified + approved → allowed
    assert specs["KXGOOD-26"].include_in_strategy_metrics is True
    # approved but unreviewed equivalence → still denied
    assert specs["KXBAD-26"].include_in_strategy_metrics is False

    # a later non-approve decision revokes the gate
    record_decision(reg, "KXGOOD-26", "needs_more_data", "rules changed", "tester")
    specs = {s.kalshi_market_id: s for s in load_pairs(reg)}
    assert specs["KXGOOD-26"].include_in_strategy_metrics is False


def test_archive_moves_with_history(tmp_path: Path):
    reg = _registry(tmp_path)
    record_decision(reg, "KXBAD-26", "archive", "unreviewed + no edge", "tester")
    archived_path = archive_pair(reg, "KXBAD-26")

    remaining = load_pairs(reg)
    assert [s.kalshi_market_id for s in remaining] == ["KXGOOD-26"]
    assert verify_registry_integrity(reg).audited
    assert verify_registry_integrity(archived_path).audited

    archived = yaml.safe_load(archived_path.read_text())
    assert len(archived["pairs"]) == 1
    entry = archived["pairs"][0]
    assert entry["kalshi_market_id"] == "KXBAD-26"
    assert entry["decision_log"][-1]["decision"] == "archive"

    # archiving again into the same file appends, not overwrites
    record_decision(reg, "KXGOOD-26", "archive", "test second archive", "tester")
    archive_pair(reg, "KXGOOD-26", archived_path)
    archived = yaml.safe_load(archived_path.read_text())
    assert len(archived["pairs"]) == 2
    assert load_pairs(reg) == []
