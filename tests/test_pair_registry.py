# Scope: TEST — Typed pair-registry loader + sha256 integrity gate.
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arbx.pairs.registry import (
    RegistryIntegrityError,
    load_pairs,
    write_registry_integrity,
)

ROOT = Path(__file__).resolve().parents[1]
APPROVED = ROOT / "configs" / "pairs.approved.yaml"


def test_load_approved_pairs():
    specs = load_pairs(APPROVED)
    assert len(specs) >= 1
    # every approved strategy pair carries non-empty Polymarket token ids
    for spec in specs:
        assert spec.pair_key
        assert spec.kalshi_market_id
        assert spec.polymarket_yes_token_id
        assert spec.polymarket_no_token_id


def test_sha256_mismatch_raises(tmp_path):
    copy = tmp_path / "pairs.approved.yaml"
    shutil.copy(APPROVED, copy)
    write_registry_integrity(copy)  # sidecar now matches the copy

    # sanity: matching hash loads fine
    assert load_pairs(copy) == load_pairs(copy, verify_sha256=False)

    # tamper the file so its hash no longer matches the recorded sidecar
    copy.write_text(copy.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(RegistryIntegrityError):
        load_pairs(copy)

    # explicitly disabling the check bypasses the gate
    assert load_pairs(copy, verify_sha256=False)
