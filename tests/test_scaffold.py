"""Scaffold-level tests for the arbx repository (P1-T1).

These assert the deny-by-default runtime configuration and that the package
imports. The full safety-boundary scan lands later in P1-T9.
"""

from pathlib import Path

import yaml

RUNTIME_YAML = Path(__file__).resolve().parents[1] / "configs" / "runtime.yaml"


def test_runtime_config_defaults():
    config = yaml.safe_load(RUNTIME_YAML.read_text())
    assert config["mode"] == "paper"
    assert config["real_orders"] == 0
    assert config["enable_real_orders"] is False


def test_package_imports():
    import arbx

    assert arbx is not None
