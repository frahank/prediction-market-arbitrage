# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Actual Kalshi public-market discovery; no account access.
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from arbx.pairs.discovery.models import (
    DiscoveredMarket,
    DiscoveryFilters,
    DiscoveryResult,
    DiscoveryStats,
    float_value,
    increment_reason,
    parse_datetime,
    rank_markets,
)

KalshiPageFetcher = Callable[[str | None, int], dict[str, Any]]
KalshiOrderbookFetcher = Callable[[str], dict[str, Any] | None]
KalshiTradePageFetcher = Callable[[str | None, int], dict[str, Any]]


def discover_kalshi_markets(
    fetch_page: KalshiPageFetcher,
    *,
    orderbook_fetcher: KalshiOrderbookFetcher | None = None,
    trade_page_fetcher: KalshiTradePageFetcher | None = None,
    filters: DiscoveryFilters | None = None,
    now: datetime | None = None,
    page_size: int = 1000,
    max_pages: int | None = None,
    trade_page_size: int = 1000,
    max_trade_pages: int = 5,
) -> DiscoveryResult:
    active_filters = filters or DiscoveryFilters()
    current_time = _as_utc(now or datetime.now(timezone.utc))
    raw_markets, pages_fetched = _fetch_all_pages(fetch_page, page_size, max_pages)
    trade_activity = (
        _fetch_trade_activity(trade_page_fetcher, trade_page_size, max_trade_pages)
        if trade_page_fetcher is not None
        else {}
    )

    accepted = []
    rejected: dict[str, int] = {}
    for raw_market in raw_markets:
        market, reason = _normalize_market(
            raw_market,
            orderbook_fetcher=orderbook_fetcher,
            trade_activity=trade_activity,
            filters=active_filters,
            now=current_time,
        )
        if market is None:
            increment_reason(rejected, reason or "unusable")
        else:
            accepted.append(market)

    ranked = rank_markets(accepted)
    return DiscoveryResult(
        venue="kalshi",
        generated_at=current_time,
        markets=ranked,
        stats=DiscoveryStats(
            pages_fetched=pages_fetched,
            records_seen=len(raw_markets),
            accepted=len(ranked),
            rejected_by_reason=dict(sorted(rejected.items())),
        ),
    )


def _fetch_all_pages(
    fetch_page: KalshiPageFetcher,
    page_size: int,
    max_pages: int | None,
) -> tuple[list[dict[str, Any]], int]:
    records = []
    cursor = None
    pages = 0
    seen_cursors = set()

    while max_pages is None or pages < max_pages:
        payload = fetch_page(cursor, page_size)
        pages += 1
        markets = payload.get("markets", [])
        if not isinstance(markets, list):
            raise ValueError("Kalshi markets response must contain a markets list")
        records.extend(record for record in markets if isinstance(record, dict))

        next_cursor = str(payload.get("cursor", "") or "")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise ValueError("Kalshi pagination cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return records, pages


def _fetch_trade_activity(
    fetch_page: KalshiTradePageFetcher,
    page_size: int,
    max_pages: int,
) -> dict[str, tuple[int, float]]:
    activity: dict[str, tuple[int, float]] = {}
    cursor = None
    seen_cursors = set()

    for _ in range(max_pages):
        payload = fetch_page(cursor, page_size)
        trades = payload.get("trades", [])
        if not isinstance(trades, list):
            raise ValueError("Kalshi trades response must contain a trades list")
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            ticker = str(trade.get("ticker", ""))
            if not ticker:
                continue
            count, volume = activity.get(ticker, (0, 0.0))
            activity[ticker] = (count + 1, volume + float_value(trade.get("count_fp")))

        next_cursor = str(payload.get("cursor", "") or "")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise ValueError("Kalshi trade pagination cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return activity


def _normalize_market(
    raw: dict[str, Any],
    *,
    orderbook_fetcher: KalshiOrderbookFetcher | None,
    trade_activity: dict[str, tuple[int, float]],
    filters: DiscoveryFilters,
    now: datetime,
) -> tuple[DiscoveredMarket | None, str | None]:
    ticker = str(raw.get("ticker", "")).strip()
    event_ticker = str(raw.get("event_ticker", "")).strip()
    title = str(raw.get("title") or raw.get("yes_sub_title") or "").strip()
    status = str(raw.get("status", "")).lower()
    close_time = parse_datetime(raw.get("close_time") or raw.get("expected_expiration_time"))
    updated_at = parse_datetime(raw.get("updated_time"))
    rules = "\n".join(
        text.strip()
        for text in (
            str(raw.get("rules_primary", "")),
            str(raw.get("rules_secondary", "")),
        )
        if text.strip()
    )
    is_multivariate = bool(
        raw.get("mve_collection_ticker")
        or raw.get("mve_selected_legs")
        or ticker.startswith("KXMVE")
    )

    if not ticker or not event_ticker:
        return None, "missing_identifiers"
    if status not in {"active", "open"}:
        return None, "not_open"
    if close_time is None or close_time <= now:
        return None, "stale_or_expired"
    if filters.exclude_multivariate and is_multivariate:
        return None, "multivariate"
    if len(title) < 8:
        return None, "unclear_question"
    if filters.require_rules and len(rules) < 20:
        return None, "unclear_rules"

    volume_24h = float_value(raw.get("volume_24h_fp"))
    volume_total = float_value(raw.get("volume_fp"))
    if volume_24h < filters.min_volume_24h:
        return None, "low_24h_volume"
    if volume_total < filters.min_total_volume:
        return None, "low_total_volume"

    best_yes_bid = _valid_price(raw.get("yes_bid_dollars"))
    best_yes_ask = _valid_price(raw.get("yes_ask_dollars"))
    best_no_bid = _valid_price(raw.get("no_bid_dollars"))
    best_no_ask = _valid_price(raw.get("no_ask_dollars"))
    spread = _smallest_spread(
        _spread(best_yes_bid, best_yes_ask),
        _spread(best_no_bid, best_no_ask),
    )
    if spread is None:
        return None, "empty_quotes"
    if spread > filters.max_spread:
        return None, "wide_spread"

    yes_depth = float_value(raw.get("yes_ask_size_fp"))
    no_depth = float_value(raw.get("no_ask_size_fp"))
    if orderbook_fetcher is not None:
        try:
            orderbook = orderbook_fetcher(ticker)
        except (OSError, RuntimeError, ValueError):
            return None, "orderbook_unavailable"
        if orderbook is None:
            return None, "orderbook_unavailable"
        yes_depth, no_depth = _orderbook_depth(orderbook)

    if filters.require_two_sided:
        if yes_depth < filters.min_depth or no_depth < filters.min_depth:
            return None, "insufficient_depth"
    elif max(yes_depth, no_depth) < filters.min_depth:
        return None, "insufficient_depth"

    recent_trade_count, recent_trade_volume = trade_activity.get(ticker, (0, 0.0))
    series_id = event_ticker.split("-")[0] if event_ticker else ""
    return (
        DiscoveredMarket(
            venue="kalshi",
            market_id=ticker,
            event_id=event_ticker,
            series_id=series_id,
            slug=ticker.lower(),
            question=title,
            yes_label=str(raw.get("yes_sub_title", "Yes") or "Yes"),
            no_label=str(raw.get("no_sub_title", "No") or "No"),
            close_time=close_time,
            updated_at=updated_at,
            rules=rules,
            status=status,
            volume_24h=volume_24h,
            volume_total=volume_total,
            open_interest=float_value(raw.get("open_interest_fp")),
            liquidity=0.0,
            spread=spread,
            best_yes_bid=best_yes_bid,
            best_yes_ask=best_yes_ask,
            best_no_bid=best_no_bid,
            best_no_ask=best_no_ask,
            yes_depth=yes_depth,
            no_depth=no_depth,
            identifiers={
                "ticker": ticker,
                "event_ticker": event_ticker,
                "series_ticker": series_id,
            },
            is_multivariate=is_multivariate,
            recent_trade_count=recent_trade_count,
            recent_trade_volume=recent_trade_volume,
        ),
        None,
    )


def _orderbook_depth(payload: dict[str, Any] | None) -> tuple[float, float]:
    if not payload:
        return 0.0, 0.0
    orderbook = payload.get("orderbook_fp", payload.get("orderbook", {}))
    if not isinstance(orderbook, dict):
        return 0.0, 0.0
    yes_depth = _level_size(orderbook.get("no_dollars", orderbook.get("no", [])))
    no_depth = _level_size(orderbook.get("yes_dollars", orderbook.get("yes", [])))
    return yes_depth, no_depth


def _level_size(levels: Any) -> float:
    if not isinstance(levels, list):
        return 0.0
    total = 0.0
    for level in levels:
        if isinstance(level, dict):
            total += float_value(level.get("size"))
        elif isinstance(level, list | tuple) and len(level) >= 2:
            total += float_value(level[1])
    return total


def _valid_price(value: Any) -> float | None:
    price = float_value(value, default=-1)
    if 0 < price < 1:
        return price
    return None


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or ask < bid:
        return None
    return round(ask - bid, 10)


def _smallest_spread(*values: float | None) -> float | None:
    available = [value for value in values if value is not None]
    return min(available) if available else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
