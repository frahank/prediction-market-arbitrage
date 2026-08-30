# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Polymarket taker-fee model with per-token TTL cache.
"""Polymarket fee model backed by the public per-token ``/fee-rate`` endpoint.

Formula (see docs/fees_polymarket.md for verification and sources)::

    taker fee = size x (base_fee_bps / 10_000) x min(price, 1 - price)

``base_fee_bps`` is per-token data resolved live from
``GET https://clob.polymarket.com/fee-rate?token_id=...`` — never a constant.
``min(P, 1-P)`` is the on-chain CTFExchange fee base; it dominates the
marketing-page ``P x (1-P)`` curve at every price, so it is also the
conservative choice while the two published formulas disagree. Makers are
never charged; CLOB fills are gasless for the trader; no settlement fee is
documented.

Resolved rates are cached per token for ``cache_ttl_hours``. On provider
failure the model returns the configured worst-case ``fallback_bps`` with
``source="flat_fallback"`` — never zero — and does not cache the failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from arbx.fees.polymarket_source import PolymarketFeeProvider
from arbx.fees.types import FeeBreakdown

VENUE = "polymarket"

SOURCE_API = "api:fee_rate_bps"
SOURCE_FALLBACK = "flat_fallback"


@dataclass(frozen=True)
class PolymarketFeeConfig:
    fallback_bps: int
    cache_ttl_hours: float
    source_urls: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> PolymarketFeeConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"fee config {path} is not a YAML mapping")
        fallback_bps = int(raw["fallback_bps"])
        if fallback_bps <= 0:
            raise ValueError(
                f"fee config {path}: fallback_bps must be > 0 (worst-case, never zero)"
            )
        cache_ttl_hours = float(raw["cache_ttl_hours"])
        if cache_ttl_hours <= 0:
            raise ValueError(f"fee config {path}: cache_ttl_hours must be > 0")
        source_urls = tuple(str(url) for url in raw.get("source_urls") or ())
        if not source_urls:
            raise ValueError(f"fee config {path}: source_urls must be non-empty")
        return cls(
            fallback_bps=fallback_bps,
            cache_ttl_hours=cache_ttl_hours,
            source_urls=source_urls,
        )


@dataclass
class _CacheEntry:
    base_fee_bps: int
    resolved_at: float


@dataclass(frozen=True)
class StaticFeeRateProvider:
    """Offline provider pinning one ``base_fee`` for every token.

    For deterministic re-scoring and tests — historical soaks reference
    resolved markets whose live ``/fee-rate`` lookups would 404 into the
    worst-case fallback and distort the analysis. Public data only; performs
    no I/O at all.
    """

    base_fee_bps: int

    def fetch_market_info_json(self, condition_id: str):
        raise NotImplementedError("static provider serves fee rates only")

    def fetch_fee_rate_json(self, token_id: str):
        from datetime import datetime, timezone

        from arbx.core.models import VenueHealth
        from arbx.venues.http import HttpResult

        return HttpResult(
            payload={"base_fee": self.base_fee_bps},
            status_code=200,
            attempts=1,
            health=VenueHealth(
                venue=VENUE,
                is_healthy=True,
                last_checked=datetime.now(timezone.utc),
                reason="static",
            ),
        )


@dataclass
class PolymarketFeeModel:
    provider: PolymarketFeeProvider
    config: PolymarketFeeConfig
    now: Callable[[], float] = time.monotonic
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)

    def taker_fee(self, *, token_id: str, price: float, size: float) -> FeeBreakdown:
        if not token_id:
            raise ValueError("token_id is required")
        if not 0 < price < 1:
            raise ValueError(f"price must be in (0, 1) dollars, got {price}")
        if size <= 0:
            raise ValueError(f"size must be > 0, got {size}")
        bps, source = self._resolve_bps(token_id)
        fee = size * (bps / 10_000) * min(price, 1 - price)
        return FeeBreakdown(
            venue=VENUE,
            taker_fee_usd=fee,
            maker_fee_usd=0.0,  # makers are never charged (docs/fees_polymarket.md)
            settlement_fee_usd=0.0,
            gas_usd=0.0,  # CLOB fills are gasless for the trader
            total_usd=fee,
            per_unit_usd=fee / size,
            source=source,
        )

    def _resolve_bps(self, token_id: str) -> tuple[int, str]:
        ttl_seconds = self.config.cache_ttl_hours * 3600
        cached = self._cache.get(token_id)
        if cached is not None and self.now() - cached.resolved_at < ttl_seconds:
            return cached.base_fee_bps, SOURCE_API

        result = self.provider.fetch_fee_rate_json(token_id)
        if not result.health.is_healthy or result.payload is None:
            return self.config.fallback_bps, SOURCE_FALLBACK
        try:
            base_fee_bps = int(result.payload["base_fee"])
        except (KeyError, TypeError, ValueError):
            return self.config.fallback_bps, SOURCE_FALLBACK
        if base_fee_bps < 0:
            return self.config.fallback_bps, SOURCE_FALLBACK

        self._cache[token_id] = _CacheEntry(
            base_fee_bps=base_fee_bps, resolved_at=self.now()
        )
        return base_fee_bps, SOURCE_API
