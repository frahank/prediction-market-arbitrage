#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
"""Compatibility wrapper for the packaged :mod:`arbx.launcher` entry point."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from arbx.launcher import (  # noqa: E402
    build_services,
    create_default_app,
    enforce_localhost,
    load_ui_config,
    main,
)

__all__ = [
    "build_services",
    "create_default_app",
    "enforce_localhost",
    "load_ui_config",
    "main",
]

if __name__ == "__main__":
    main()
