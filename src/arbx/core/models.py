# Scope: SHARED_CORE — Domain models shared across the arbx analysis modules.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class ModelValidationError(ValueError):
    """Raised when a domain model is constructed with invalid values."""


class Outcome(StrEnum):
    YES = "YES"
    NO = "NO"


class PairOrientation(StrEnum):
    SAME = "same"
    INVERTED = "inverted"


class ApprovalStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED_FOR_PAPER = "approved_for_paper"
    APPROVED_FOR_LIVE = "approved_for_live"


class ConnectorSource(StrEnum):
    MOCK = "mock"
    FAKE_PUBLIC = "fake_public"
    LIVE_PUBLIC = "live_public"
    AUTHENTICATED_READONLY = "authenticated_readonly"
    REPLAY = "replay"


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float

    def __post_init__(self) -> None:
        if not 0 < self.price < 1:
            raise ModelValidationError("price must be greater than 0 and less than 1")
        if self.size <= 0:
            raise ModelValidationError("size must be greater than 0")


@dataclass(frozen=True)
class OrderBook:
    venue: str
    market_id: str
    yes_levels: tuple[OrderBookLevel, ...]
    no_levels: tuple[OrderBookLevel, ...]
    timestamp: datetime
    fetched_at: datetime | None = None
    connector_source: ConnectorSource = ConnectorSource.MOCK
    source_reference: str = ""
    reportable: bool = True

    def __post_init__(self) -> None:
        if not self.venue:
            raise ModelValidationError("venue is required")
        if not self.market_id:
            raise ModelValidationError("market_id is required")
        if not isinstance(self.timestamp, datetime):
            raise ModelValidationError("timestamp must be a datetime")
        if self.fetched_at is not None and not isinstance(self.fetched_at, datetime):
            raise ModelValidationError("fetched_at must be a datetime")
        object.__setattr__(self, "yes_levels", tuple(self.yes_levels))
        object.__setattr__(self, "no_levels", tuple(self.no_levels))

    def is_stale(
        self,
        *,
        max_age: timedelta = timedelta(seconds=5),
        now: datetime | None = None,
    ) -> bool:
        check_time = now or datetime.now(timezone.utc)
        # Venue time is the market-data age when it is available. `fetched_at`
        # records local receipt time and must not make an old venue book fresh.
        timestamp = _as_aware_utc(self.timestamp)
        return check_time - timestamp > max_age


@dataclass(frozen=True)
class Market:
    venue: str
    market_id: str
    question: str
    resolution_url: str
    closes_at: datetime

    def __post_init__(self) -> None:
        if not self.venue:
            raise ModelValidationError("venue is required")
        if not self.market_id:
            raise ModelValidationError("market_id is required")
        if not self.question:
            raise ModelValidationError("question is required")
        if not self.resolution_url:
            raise ModelValidationError("resolution_url is required")
        if not isinstance(self.closes_at, datetime):
            raise ModelValidationError("closes_at must be a datetime")


@dataclass(frozen=True)
class MarketPair:
    kalshi_market: Market
    poly_market: Market
    orientation: PairOrientation
    approved_status: ApprovalStatus

    def __post_init__(self) -> None:
        if self.orientation is None:
            raise ModelValidationError("orientation is required")


@dataclass(frozen=True)
class VenueHealth:
    venue: str
    is_healthy: bool
    last_checked: datetime
    reason: str
    connector_source: ConnectorSource = ConnectorSource.MOCK

    def __post_init__(self) -> None:
        if not self.venue:
            raise ModelValidationError("venue is required")
        if not isinstance(self.last_checked, datetime):
            raise ModelValidationError("last_checked must be a datetime")


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
