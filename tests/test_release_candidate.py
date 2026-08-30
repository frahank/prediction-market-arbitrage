"""Offline release-candidate and packaging checks."""
from __future__ import annotations

import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts.verify_distribution import REQUIRED_WHEEL_FILES, verify_wheel

from arbx.release_check import check_release

ROOT = Path(__file__).resolve().parents[1]


def test_one_command_launcher_is_executable_valid_shell():
    launcher = ROOT / "run"
    assert launcher.is_file()
    assert stat.S_IMODE(launcher.stat().st_mode) & stat.S_IXUSR
    result = subprocess.run(
        ["sh", "-n", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_offline_release_check_passes():
    result = check_release(ROOT)
    assert result["mode"] == "paper"
    assert result["registry_pairs"] > 0
    assert result["scannable_pairs"] > 0
    assert result["ui_routes"] == 5


def test_release_check_reports_strategy_eligibility_separately():
    """A registry can be fully scannable while nothing counts toward strategy.

    That is the shipped state, and reporting it as one number is what let a
    registry no default scan could run on still look healthy.
    """
    result = check_release(ROOT)
    assert result["scannable_pairs"] <= result["registry_pairs"]
    assert result["strategy_eligible_pairs"] <= result["registry_pairs"]


def test_live_adapter_guide_preserves_paper_boundary():
    guide = (ROOT / "docs" / "LIVE_ADAPTER_GUIDE.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "LIVE_ADAPTER_GUIDE.md" in readme
    assert "There is deliberately no `live=true` switch" in guide
    assert "outside `src/arbx`" in guide
    assert "does not enable trading" in guide
    assert "https://docs.kalshi.com/" in guide
    assert "https://docs.polymarket.com/" in guide


def test_wheel_asset_verifier(tmp_path: Path):
    good = tmp_path / "arbx-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(good, "w") as archive:
        for name in REQUIRED_WHEEL_FILES:
            archive.writestr(name, "fixture")
    verify_wheel(good)

    bad = tmp_path / "arbx-0.1.0-bad.whl"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("arbx/__init__.py", "")
    with pytest.raises(RuntimeError, match="missing required UI assets"):
        verify_wheel(bad)
