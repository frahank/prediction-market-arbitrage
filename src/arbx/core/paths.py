# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Shared local filesystem defaults.
from __future__ import annotations

from pathlib import Path

DEFAULT_DATA_DIR_NAME = "data_strategy_30"


def default_data_dir(root: Path) -> Path:
    """Return the clean 30 verified-pair dataset root for operator workflows."""
    return root / DEFAULT_DATA_DIR_NAME
