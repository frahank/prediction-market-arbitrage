# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: SHARED_CORE — Fee breakdown type shared by all venue fee models (Phase 2).
"""Common fee types for the arbx fee engine.

:class:`FeeBreakdown` is the single return type of every per-venue fee model
and of the composed :class:`~arbx.fees.engine.FeeEngine`. ``source`` records
where the number came from so every fee is traceable:

- ``"formula:v<N>"`` — computed from a pinned, versioned venue formula
  (e.g. ``configs/fees_kalshi.yaml``).
- ``"api:fee_rate_bps"`` — resolved from live per-market venue data.
- ``"flat_fallback"`` — worst-case fallback for unknown markets; never zero.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeBreakdown:
    venue: str
    taker_fee_usd: float
    maker_fee_usd: float
    settlement_fee_usd: float
    gas_usd: float
    total_usd: float
    per_unit_usd: float
    source: str  # "formula:v1" | "api:fee_rate_bps" | "flat_fallback"
