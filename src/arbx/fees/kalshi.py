# Scope: BOT_RUNTIME — Kalshi trading-fee model against the pinned schedule (Phase 2).
"""Kalshi fee model backed by the pinned, versioned ``configs/fees_kalshi.yaml``.

Formula (see docs/fees_kalshi.md for the derivation and worked examples)::

    fee per order = ceil_to_cent(multiplier x C x P x (1 - P))

where ``C`` is the contract count, ``P`` the price in dollars (0 < P < 1),
and the multiplier is the general taker/maker multiplier unless the market's
series appears in ``series_overrides``. Kalshi tickers are
``SERIES-EVENT-STRIKE``; the series is the substring before the first ``-``
unless an override key itself contains ``-``, in which case it prefix-matches
the full market ticker. Unknown series fall back to the general multipliers —
never to zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import yaml

from arbx.fees.types import FeeBreakdown

VENUE = "kalshi"

# The only rounding rule the pinned schedule defines; loading any other value
# fails loudly rather than silently mis-rounding fees.
ROUNDING_CEIL_CENT_PER_ORDER = "ceil_cent_per_order"


@dataclass(frozen=True)
class SeriesFeeOverride:
    taker_multiplier: Decimal
    maker_multiplier: Decimal


@dataclass(frozen=True)
class KalshiFeeSchedule:
    version: int
    retrieved_at: str
    source_urls: tuple[str, ...]
    taker_multiplier: Decimal
    maker_multiplier: Decimal
    rounding: str
    series_overrides: dict[str, SeriesFeeOverride] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> KalshiFeeSchedule:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"fee schedule {path} is not a YAML mapping")
        general = raw.get("general")
        if not isinstance(general, dict):
            raise ValueError(f"fee schedule {path} is missing the 'general' block")
        taker = Decimal(str(general["taker_multiplier"]))
        maker = Decimal(str(general["maker_multiplier"]))
        if taker <= 0:
            raise ValueError(f"fee schedule {path}: general.taker_multiplier must be > 0")
        rounding = str(general.get("rounding", ""))
        if rounding != ROUNDING_CEIL_CENT_PER_ORDER:
            raise ValueError(
                f"fee schedule {path}: unsupported rounding {rounding!r}; "
                f"only {ROUNDING_CEIL_CENT_PER_ORDER!r} is implemented"
            )
        overrides: dict[str, SeriesFeeOverride] = {}
        for ticker, override in (raw.get("series_overrides") or {}).items():
            override_taker = Decimal(str(override["taker_multiplier"]))
            if override_taker <= 0:
                raise ValueError(
                    f"fee schedule {path}: series_overrides.{ticker} pins a zero fee"
                )
            overrides[str(ticker)] = SeriesFeeOverride(
                taker_multiplier=override_taker,
                maker_multiplier=Decimal(str(override.get("maker_multiplier", 0))),
            )
        source_urls = tuple(str(url) for url in raw.get("source_urls") or ())
        if not source_urls:
            raise ValueError(f"fee schedule {path}: source_urls must be non-empty")
        return cls(
            version=int(raw["version"]),
            retrieved_at=str(raw["retrieved_at"]),
            source_urls=source_urls,
            taker_multiplier=taker,
            maker_multiplier=maker,
            rounding=rounding,
            series_overrides=overrides,
        )


def _series_of(market_ticker: str) -> str:
    """Series component of a ``SERIES-EVENT-STRIKE`` Kalshi ticker."""
    return market_ticker.split("-", 1)[0]


class KalshiFeeModel:
    def __init__(self, schedule: KalshiFeeSchedule) -> None:
        self._schedule = schedule
        self._source = f"formula:v{schedule.version}"

    @property
    def schedule(self) -> KalshiFeeSchedule:
        return self._schedule

    def taker_fee(
        self, *, price: float, count: int, series_ticker: str | None = None
    ) -> FeeBreakdown:
        fee = self._order_fee(
            multiplier=self._multipliers(series_ticker).taker_multiplier,
            price=price,
            count=count,
        )
        return self._breakdown(taker_fee_usd=fee, maker_fee_usd=0.0, count=count)

    def maker_fee(
        self, *, price: float, count: int, series_ticker: str | None = None
    ) -> FeeBreakdown:
        fee = self._order_fee(
            multiplier=self._multipliers(series_ticker).maker_multiplier,
            price=price,
            count=count,
        )
        return self._breakdown(taker_fee_usd=0.0, maker_fee_usd=fee, count=count)

    def _multipliers(self, series_ticker: str | None) -> SeriesFeeOverride:
        override = self._override_for(series_ticker)
        if override is not None:
            return override
        return SeriesFeeOverride(
            taker_multiplier=self._schedule.taker_multiplier,
            maker_multiplier=self._schedule.maker_multiplier,
        )

    def _override_for(self, series_ticker: str | None) -> SeriesFeeOverride | None:
        if not series_ticker:
            return None
        derived_series = _series_of(series_ticker)
        for key, override in self._schedule.series_overrides.items():
            if "-" in key:
                # Override pinned to a specific event/market: prefix match on
                # the full ticker at a `-` boundary.
                if series_ticker == key or series_ticker.startswith(key + "-"):
                    return override
            elif derived_series == key:
                return override
        return None

    def _order_fee(self, *, multiplier: Decimal, price: float, count: int) -> float:
        if count <= 0:
            raise ValueError(f"count must be a positive contract count, got {count}")
        if not 0 < price < 1:
            raise ValueError(f"price must be in (0, 1) dollars, got {price}")
        p = Decimal(str(price))
        raw = multiplier * Decimal(count) * p * (Decimal(1) - p)
        # ceil_cent_per_order: round the whole order's fee UP to the next cent.
        return float(raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING))

    def _breakdown(
        self, *, taker_fee_usd: float, maker_fee_usd: float, count: int
    ) -> FeeBreakdown:
        total = taker_fee_usd + maker_fee_usd
        return FeeBreakdown(
            venue=VENUE,
            taker_fee_usd=taker_fee_usd,
            maker_fee_usd=maker_fee_usd,
            settlement_fee_usd=0.0,  # no settlement fee (docs/fees_kalshi.md)
            gas_usd=0.0,
            total_usd=total,
            per_unit_usd=total / count,
            source=self._source,
        )
