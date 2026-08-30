# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Cross-venue fee engine composing the per-venue models (Phase 2).
"""FeeEngine: one entry point for real per-venue fees.

Composes :class:`~arbx.fees.kalshi.KalshiFeeModel` (pinned formula schedule)
and :class:`~arbx.fees.polymarket.PolymarketFeeModel` (live per-token
``base_fee`` with worst-case fallback). ``round_trip_cost`` is the taker fee
on each leg at the given execution prices and size; ``leg_fee`` prices one
leg. Invariant: an unknown market/series resolves to a conservative
worst-case fee, never zero — Kalshi unknowns take the general formula,
Polymarket unknowns take the configured ``fallback_bps`` with
``source="flat_fallback"``.

Book prices can sit at 0 or 1 exactly; both venue formulas need prices in
(0, 1), so fee inputs are clamped to [0.01, 0.99]. Clamping only ever raises
the computed fee (both formulas vanish toward the extremes) — conservative.
"""
from __future__ import annotations

import math
from pathlib import Path

from arbx.fees.kalshi import KalshiFeeModel, KalshiFeeSchedule
from arbx.fees.polymarket import (
    SOURCE_FALLBACK,
    PolymarketFeeConfig,
    PolymarketFeeModel,
)
from arbx.fees.types import FeeBreakdown

LIQUIDITY_ROLES = {"taker", "maker"}

# Fee-input price clamp; see module docstring.
_PRICE_MIN = 0.01
_PRICE_MAX = 0.99

# Directions as emitted by arbx.analysis.edges. The Polymarket leg of
# "kalshi_yes_poly_no" buys the NO token; the other direction buys YES.
_POLY_TOKEN_FIELD_BY_DIRECTION = {
    "kalshi_yes_poly_no": "polymarket_no_token_id",
    "kalshi_no_poly_yes": "polymarket_yes_token_id",
}


class FeeEngine:
    def __init__(self, kalshi: KalshiFeeModel, polymarket: PolymarketFeeModel) -> None:
        self.kalshi = kalshi
        self.polymarket = polymarket

    @classmethod
    def from_configs(
        cls,
        kalshi_config: Path,
        polymarket_config: Path,
        *,
        provider=None,
    ) -> FeeEngine:
        """Build the engine from the pinned config files.

        ``provider`` defaults to the real public-data provider; inject a stub
        for tests. Public GETs only — no order-mutation capability exists here.
        """
        if provider is None:
            from arbx.venues.polymarket_public import PolymarketApiProvider

            provider = PolymarketApiProvider()
        return cls(
            kalshi=KalshiFeeModel(KalshiFeeSchedule.load(kalshi_config)),
            polymarket=PolymarketFeeModel(
                provider=provider,
                config=PolymarketFeeConfig.load(polymarket_config),
            ),
        )

    @property
    def fee_model_version(self) -> str:
        """Version stamp for derived rows, e.g. ``"kalshi:v1+poly:api"``."""
        return f"kalshi:v{self.kalshi.schedule.version}+poly:api"

    def leg_fee(
        self,
        venue: str,
        *,
        market_id: str,
        price: float,
        size: float,
        liquidity: str,
    ) -> FeeBreakdown:
        if liquidity not in LIQUIDITY_ROLES:
            raise ValueError(f"liquidity must be one of {LIQUIDITY_ROLES}, got {liquidity!r}")
        if size <= 0:
            raise ValueError(f"size must be > 0, got {size}")
        clamped = min(max(price, _PRICE_MIN), _PRICE_MAX)
        if venue == "kalshi":
            # Kalshi fees are per whole contract; round fractional sizes up.
            count = max(1, math.ceil(size))
            series = market_id or None
            if liquidity == "taker":
                fee = self.kalshi.taker_fee(price=clamped, count=count, series_ticker=series)
            else:
                fee = self.kalshi.maker_fee(price=clamped, count=count, series_ticker=series)
            # Re-normalize per-unit to the requested (possibly fractional) size.
            return _with_per_unit(fee, size)
        if venue == "polymarket":
            if not market_id:
                return self._polymarket_fallback(price=clamped, size=size)
            if liquidity == "maker":
                # Makers are never charged (docs/fees_polymarket.md).
                return FeeBreakdown(
                    venue="polymarket",
                    taker_fee_usd=0.0,
                    maker_fee_usd=0.0,
                    settlement_fee_usd=0.0,
                    gas_usd=0.0,
                    total_usd=0.0,
                    per_unit_usd=0.0,
                    source="formula:v1",
                )
            return self.polymarket.taker_fee(token_id=market_id, price=clamped, size=size)
        raise ValueError(f"unknown venue {venue!r}")

    def round_trip_cost(
        self,
        pair,
        direction: str,
        *,
        kalshi_price: float,
        polymarket_price: float,
        size: float,
    ) -> FeeBreakdown:
        """Taker fee on both legs at the given execution prices and size.

        ``kalshi_price``/``polymarket_price`` are the prices actually paid per
        share on each venue for ``direction`` (e.g. the NO-token price on the
        Polymarket leg of ``kalshi_yes_poly_no``). ``pair`` is a
        :class:`~arbx.pairs.registry.PairSpec`; its per-direction Polymarket
        token id keys the live fee-rate lookup.
        """
        token_field = _POLY_TOKEN_FIELD_BY_DIRECTION.get(direction)
        if token_field is None:
            raise ValueError(f"unknown direction {direction!r}")
        kalshi_fee = self.leg_fee(
            "kalshi",
            market_id=getattr(pair, "kalshi_market_id", "") or "",
            price=kalshi_price,
            size=size,
            liquidity="taker",
        )
        poly_fee = self.leg_fee(
            "polymarket",
            market_id=getattr(pair, token_field, "") or "",
            price=polymarket_price,
            size=size,
            liquidity="taker",
        )
        return combine_legs(kalshi_fee, poly_fee, size=size)

    def _polymarket_fallback(self, *, price: float, size: float) -> FeeBreakdown:
        # Unknown market: worst-case configured bps, never zero.
        bps = self.polymarket.config.fallback_bps
        fee = size * (bps / 10_000) * min(price, 1 - price)
        return FeeBreakdown(
            venue="polymarket",
            taker_fee_usd=fee,
            maker_fee_usd=0.0,
            settlement_fee_usd=0.0,
            gas_usd=0.0,
            total_usd=fee,
            per_unit_usd=fee / size,
            source=SOURCE_FALLBACK,
        )


def combine_legs(*legs: FeeBreakdown, size: float) -> FeeBreakdown:
    """Sum per-leg breakdowns into one cross-venue round-trip breakdown."""
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size}")
    total = sum(leg.total_usd for leg in legs)
    return FeeBreakdown(
        venue="+".join(leg.venue for leg in legs),
        taker_fee_usd=sum(leg.taker_fee_usd for leg in legs),
        maker_fee_usd=sum(leg.maker_fee_usd for leg in legs),
        settlement_fee_usd=sum(leg.settlement_fee_usd for leg in legs),
        gas_usd=sum(leg.gas_usd for leg in legs),
        total_usd=total,
        per_unit_usd=total / size,
        source="+".join(leg.source for leg in legs),
    )


def _with_per_unit(fee: FeeBreakdown, size: float) -> FeeBreakdown:
    if fee.per_unit_usd == fee.total_usd / size:
        return fee
    return FeeBreakdown(
        venue=fee.venue,
        taker_fee_usd=fee.taker_fee_usd,
        maker_fee_usd=fee.maker_fee_usd,
        settlement_fee_usd=fee.settlement_fee_usd,
        gas_usd=fee.gas_usd,
        total_usd=fee.total_usd,
        per_unit_usd=fee.total_usd / size,
        source=fee.source,
    )
