# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Public-market discovery records for the actual paper bot.
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class DiscoveryFilters:
    min_volume_24h: float = 1.0
    min_total_volume: float = 1.0
    min_depth: float = 1.0
    max_spread: float = 0.25
    require_rules: bool = True
    require_two_sided: bool = False
    exclude_multivariate: bool = True

    def __post_init__(self) -> None:
        if self.min_volume_24h < 0 or self.min_total_volume < 0 or self.min_depth < 0:
            raise ValueError("discovery minimums cannot be negative")
        if not 0 <= self.max_spread <= 1:
            raise ValueError("max_spread must be between 0 and 1")


@dataclass(frozen=True)
class DiscoveredMarket:
    venue: str
    market_id: str
    event_id: str
    series_id: str
    slug: str
    question: str
    yes_label: str
    no_label: str
    close_time: datetime
    updated_at: datetime | None
    rules: str
    status: str
    volume_24h: float
    volume_total: float
    open_interest: float
    liquidity: float
    spread: float | None
    best_yes_bid: float | None
    best_yes_ask: float | None
    best_no_bid: float | None
    best_no_ask: float | None
    yes_depth: float
    no_depth: float
    identifiers: dict[str, str]
    is_multivariate: bool = False
    recent_trade_count: int = 0
    recent_trade_volume: float = 0.0

    @property
    def activity_score(self) -> float:
        return (
            self.volume_24h
            + (self.recent_trade_volume * 2)
            + min(self.open_interest, 100_000) * 0.01
            + min(self.liquidity, 100_000) * 0.01
        )

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["close_time"] = _as_utc(self.close_time).isoformat()
        record["updated_at"] = None if self.updated_at is None else _as_utc(self.updated_at).isoformat()
        record["activity_score"] = round(self.activity_score, 6)
        return record


@dataclass(frozen=True)
class DiscoveryStats:
    pages_fetched: int
    records_seen: int
    accepted: int
    rejected_by_reason: dict[str, int] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    venue: str
    generated_at: datetime
    markets: tuple[DiscoveredMarket, ...]
    stats: DiscoveryStats

    def to_record(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "generated_at": _as_utc(self.generated_at).isoformat(),
            "stats": self.stats.to_record(),
            "markets": [market.to_record() for market in self.markets],
        }


def save_discovery_result(path: str | Path, result: DiscoveryResult) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result.to_record(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def rank_markets(markets: Iterable[DiscoveredMarket]) -> tuple[DiscoveredMarket, ...]:
    return tuple(
        sorted(
            markets,
            key=lambda market: (
                market.activity_score,
                market.volume_24h,
                market.volume_total,
                market.market_id,
            ),
            reverse=True,
        )
    )


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, int | float):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def float_value(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def increment_reason(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
