
from arbx.pairs.registry import (
    verify_registry_integrity,
    write_registry_integrity,
)


def test_registry_sidecar_detects_manual_edits(tmp_path):
    registry = tmp_path / "pairs.yaml"
    registry.write_text("pairs: []\n", encoding="utf-8")
    write_registry_integrity(registry)
    assert verify_registry_integrity(registry).audited is True

    registry.write_text("pairs:\n  - changed\n", encoding="utf-8")
    result = verify_registry_integrity(registry)
    assert result.audited is False
    assert result.status == "unreviewed_external_change"
