# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Live round-robin arbitrage scanner (detection + capture).
"""Continuous batched scanner over the pair universe.

See :mod:`arbx.scanner.live_scanner` and ``docs/live_scanner.md``.
"""
from arbx.scanner.edges_writer import EdgesWriter, build_edge_row
from arbx.scanner.live_scanner import (
    ArbScanner,
    OpportunitySink,
    ScannerConfig,
    ScanStats,
)
from arbx.scanner.rotation import (
    RotationPlan,
    RotationScheduler,
    effective_cadence_s,
)

__all__ = [
    "ArbScanner",
    "EdgesWriter",
    "OpportunitySink",
    "build_edge_row",
    "RotationPlan",
    "RotationScheduler",
    "ScannerConfig",
    "ScanStats",
    "effective_cadence_s",
]
