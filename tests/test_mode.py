# Runtime mode guard: deny-by-default, three-gate real-order enable.
from __future__ import annotations

from pathlib import Path

import pytest

from arbx.core import mode
from arbx.core.mode import (
    RuntimeMode,
    SafetyViolation,
    assert_paper,
    current_mode,
    real_orders_enabled,
)


def _write_runtime(tmp_path: Path, *, mode_value: str, enable_real_orders: bool) -> Path:
    config = tmp_path / "runtime.yaml"
    config.write_text(
        f"mode: {mode_value}\n"
        f"real_orders: {1 if enable_real_orders else 0}\n"
        f"enable_real_orders: {'true' if enable_real_orders else 'false'}\n"
    )
    return config


def test_default_is_paper(monkeypatch):
    # No env override, shipped config (paper) -> PAPER and real orders disabled.
    monkeypatch.delenv(mode.ENABLE_ENV_VAR, raising=False)
    assert current_mode() is RuntimeMode.PAPER
    assert real_orders_enabled() is False


def test_default_is_paper_when_config_missing(monkeypatch, tmp_path):
    # Deny-by-default: a missing config file must resolve to PAPER, disabled.
    monkeypatch.setattr(mode, "DEFAULT_RUNTIME_YAML", tmp_path / "does_not_exist.yaml")
    monkeypatch.delenv(mode.ENABLE_ENV_VAR, raising=False)
    assert current_mode(mode.DEFAULT_RUNTIME_YAML) is RuntimeMode.PAPER
    assert real_orders_enabled() is False


def test_live_requires_all_three_gates(monkeypatch, tmp_path):
    # Gate 1 missing: config mode is paper (env + enable set) -> disabled.
    monkeypatch.setattr(
        mode, "DEFAULT_RUNTIME_YAML",
        _write_runtime(tmp_path, mode_value="paper", enable_real_orders=True),
    )
    monkeypatch.setenv(mode.ENABLE_ENV_VAR, "1")
    assert real_orders_enabled() is False

    # Gate 2 missing: enable_real_orders false (mode live + env set) -> disabled.
    monkeypatch.setattr(
        mode, "DEFAULT_RUNTIME_YAML",
        _write_runtime(tmp_path, mode_value="live", enable_real_orders=False),
    )
    monkeypatch.setenv(mode.ENABLE_ENV_VAR, "1")
    assert real_orders_enabled() is False

    # Gate 3 missing: env var absent (mode live + enable set) -> disabled.
    monkeypatch.setattr(
        mode, "DEFAULT_RUNTIME_YAML",
        _write_runtime(tmp_path, mode_value="live", enable_real_orders=True),
    )
    monkeypatch.delenv(mode.ENABLE_ENV_VAR, raising=False)
    assert real_orders_enabled() is False


def test_assert_paper_raises_when_live(monkeypatch, tmp_path):
    # All three gates set -> real orders enabled -> assert_paper raises.
    monkeypatch.setattr(
        mode, "DEFAULT_RUNTIME_YAML",
        _write_runtime(tmp_path, mode_value="live", enable_real_orders=True),
    )
    monkeypatch.setenv(mode.ENABLE_ENV_VAR, "1")
    assert current_mode(mode.DEFAULT_RUNTIME_YAML) is RuntimeMode.LIVE
    assert real_orders_enabled() is True
    with pytest.raises(SafetyViolation):
        assert_paper()


def test_assert_paper_silent_in_paper(monkeypatch):
    monkeypatch.delenv(mode.ENABLE_ENV_VAR, raising=False)
    assert assert_paper() is None
