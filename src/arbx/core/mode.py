# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Runtime mode + real-order safety gate (deny-by-default).
"""Runtime mode guard for arbx.

The repo ships in ``paper`` mode with real orders disabled. Placing real orders
requires **three independent gates** to all be set:

1. ``configs/runtime.yaml`` ``mode: live``
2. ``configs/runtime.yaml`` ``enable_real_orders: true``
3. environment variable ``ARBX_ENABLE_REAL_ORDERS == "1"``

If any gate is missing — including a missing config file or missing env var —
real orders are disabled. This is the deny-by-default invariant asserted by the
safety tests; nothing in the codebase should place a real order without first
calling :func:`assert_paper` (which raises unless all three gates are set).
"""
from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

# Repo config: src/arbx/core/mode.py -> parents[3] is the repo root.
DEFAULT_RUNTIME_YAML = Path(__file__).resolve().parents[3] / "configs" / "runtime.yaml"

ENABLE_ENV_VAR = "ARBX_ENABLE_REAL_ORDERS"


class RuntimeMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SafetyViolation(RuntimeError):
    """Raised when a real-order-enabled runtime is detected where paper is required."""


def _load_config(config_path: Path) -> dict[str, Any]:
    """Load the runtime YAML; return ``{}`` on any missing/unreadable/malformed file."""
    try:
        text = Path(config_path).read_text()
    except OSError:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def current_mode(config_path: Path = DEFAULT_RUNTIME_YAML) -> RuntimeMode:
    """Read ``mode`` from ``configs/runtime.yaml``; default (missing/unknown) is PAPER."""
    raw = str(_load_config(config_path).get("mode", "")).strip().lower()
    return RuntimeMode.LIVE if raw == RuntimeMode.LIVE.value else RuntimeMode.PAPER


def real_orders_enabled() -> bool:
    """True ONLY if all three gates are set: mode==live, enable_real_orders, env=="1"."""
    config = _load_config(DEFAULT_RUNTIME_YAML)
    if current_mode(DEFAULT_RUNTIME_YAML) is not RuntimeMode.LIVE:
        return False
    if config.get("enable_real_orders") is not True:
        return False
    if os.environ.get(ENABLE_ENV_VAR) != "1":
        return False
    return True


def assert_paper() -> None:
    """Raise :class:`SafetyViolation` if real orders are enabled."""
    if real_orders_enabled():
        raise SafetyViolation(
            "real orders are enabled but paper mode is required; "
            "clear the runtime gates before proceeding"
        )
