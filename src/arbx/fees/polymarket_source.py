# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Public Polymarket fee-metadata resolution.
"""Polymarket public fee-metadata resolution.

Port of ``arb_bot/src/arb_bot/market_fees.py`` with imports rewritten for
arbx (P2-T3). Paper-runtime residue was stripped in the port: the
``FeeConfig`` schedule object (arb_bot paper engine) and the
``ConnectorSource``/``ValueClassification`` reporting fields are gone;
:class:`PublicFeeResult` carries the resolved numbers directly. The
resolution logic — cross-checking the ``/clob-markets`` and ``/fee-rate``
endpoints and failing closed on any mismatch or malformed payload — is
unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from arbx.venues.http import HttpResult

POLYMARKET_FEE_SOURCE = "https://docs.polymarket.com/trading/fees"


class MarketFeeError(ValueError):
    """Raised when public fee metadata cannot be converted safely."""


class PolymarketFeeProvider(Protocol):
    def fetch_market_info_json(self, condition_id: str) -> HttpResult: ...

    def fetch_fee_rate_json(self, token_id: str) -> HttpResult: ...


@dataclass(frozen=True)
class PublicFeeResult:
    venue: str
    market_id: str
    token_id: str
    verified: bool
    reason: str
    checked_at: datetime
    base_fee_bps: int | None
    fee_rate: float | None
    taker_only: bool | None
    formula: str = ""
    effective_date: str = ""
    source_reference: str = POLYMARKET_FEE_SOURCE


def resolve_polymarket_fee_config(
    provider: PolymarketFeeProvider,
    *,
    condition_id: str,
    token_id: str,
    checked_at: datetime | None = None,
) -> PublicFeeResult:
    if not condition_id or not token_id:
        raise MarketFeeError("condition_id and token_id are required")

    market_info = provider.fetch_market_info_json(condition_id)
    fee_rate = provider.fetch_fee_rate_json(token_id)
    timestamp = _as_utc(checked_at or datetime.now(timezone.utc))
    if not market_info.health.is_healthy or market_info.payload is None:
        return _unverified(
            condition_id,
            token_id,
            timestamp,
            f"market_info_unavailable:{market_info.status_code}",
        )
    if not fee_rate.health.is_healthy or fee_rate.payload is None:
        return _unverified(
            condition_id,
            token_id,
            timestamp,
            f"fee_rate_unavailable:{fee_rate.status_code}",
        )

    try:
        base_fee_bps = int(fee_rate.payload["base_fee"])
        taker_base_fee_bps = int(market_info.payload.get("tbf", base_fee_bps))
        details = market_info.payload.get("fd") or {}
        if not isinstance(details, dict):
            raise TypeError("fee details must be a mapping")
        curve_rate = details.get("r")
        taker_only = bool(details.get("to", True))
        parsed_rate = None if curve_rate is None else float(curve_rate)
    except (KeyError, TypeError, ValueError) as exc:
        return _unverified(
            condition_id,
            token_id,
            timestamp,
            f"invalid_fee_payload:{exc}",
        )

    if base_fee_bps < 0 or taker_base_fee_bps < 0:
        return _unverified(
            condition_id,
            token_id,
            timestamp,
            "negative_fee_metadata",
        )
    if base_fee_bps != taker_base_fee_bps:
        return _unverified(
            condition_id,
            token_id,
            timestamp,
            "fee_endpoint_mismatch",
            base_fee_bps=base_fee_bps,
            fee_rate=parsed_rate,
            taker_only=taker_only,
        )

    if base_fee_bps == 0:
        effective_rate = 0.0
    else:
        if parsed_rate is None or parsed_rate <= 0:
            return _unverified(
                condition_id,
                token_id,
                timestamp,
                "missing_fee_curve_rate",
                base_fee_bps=base_fee_bps,
                fee_rate=parsed_rate,
                taker_only=taker_only,
            )
        effective_rate = parsed_rate

    return PublicFeeResult(
        venue="polymarket",
        market_id=condition_id,
        token_id=token_id,
        verified=True,
        reason="public_fee_metadata_verified",
        checked_at=timestamp,
        base_fee_bps=base_fee_bps,
        fee_rate=effective_rate,
        taker_only=taker_only,
        effective_date=timestamp.date().isoformat(),
    )


def _unverified(
    condition_id: str,
    token_id: str,
    checked_at: datetime,
    reason: str,
    *,
    base_fee_bps: int | None = None,
    fee_rate: float | None = None,
    taker_only: bool | None = None,
) -> PublicFeeResult:
    return PublicFeeResult(
        venue="polymarket",
        market_id=condition_id,
        token_id=token_id,
        verified=False,
        reason=reason,
        checked_at=checked_at,
        base_fee_bps=base_fee_bps,
        fee_rate=fee_rate,
        taker_only=taker_only,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
