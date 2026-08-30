# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Actual Polymarket public-market discovery; no account access.
from __future__ import annotations

import json
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

PolymarketPageFetcher = Callable[[int, int], list[dict[str, Any]]]
PolymarketOrderbookFetcher = Callable[[str], dict[str, Any] | None]


def discover_polymarket_markets(
    fetch_page: PolymarketPageFetcher,
    *,
    orderbook_fetcher: PolymarketOrderbookFetcher | None = None,
    filters: DiscoveryFilters | None = None,
    now: datetime | None = None,
    page_size: int = 100,
    max_pages: int | None = None,
) -> DiscoveryResult:
    active_filters = filters or DiscoveryFilters()
    current_time = _as_utc(now or datetime.now(timezone.utc))
    events, pages_fetched = _fetch_all_pages(fetch_page, page_size, max_pages)

    accepted = []
    rejected: dict[str, int] = {}
    seen_market_ids = set()
    records_seen = 0

    for event in events:
        markets = event.get("markets", [])
        if not isinstance(markets, list):
            increment_reason(rejected, "invalid_event_markets")
            continue
        for raw_market in markets:
            if not isinstance(raw_market, dict):
                continue
            records_seen += 1
            market, reason = _normalize_market(
                event,
                raw_market,
                orderbook_fetcher=orderbook_fetcher,
                filters=active_filters,
                now=current_time,
            )
            if market is None:
                increment_reason(rejected, reason or "unusable")
            elif market.market_id not in seen_market_ids:
                seen_market_ids.add(market.market_id)
                accepted.append(market)

    ranked = rank_markets(accepted)
    return DiscoveryResult(
        venue="polymarket",
        generated_at=current_time,
        markets=ranked,
        stats=DiscoveryStats(
            pages_fetched=pages_fetched,
            records_seen=records_seen,
            accepted=len(ranked),
            rejected_by_reason=dict(sorted(rejected.items())),
        ),
    )


def _fetch_all_pages(
    fetch_page: PolymarketPageFetcher,
    page_size: int,
    max_pages: int | None,
) -> tuple[list[dict[str, Any]], int]:
    events = []
    offset = 0
    pages = 0

    while max_pages is None or pages < max_pages:
        page = fetch_page(offset, page_size)
        pages += 1
        if not isinstance(page, list):
            raise ValueError("Polymarket events response must be a list")
        events.extend(event for event in page if isinstance(event, dict))
        if len(page) < page_size:
            break
        offset += page_size

    return events, pages


def _normalize_market(
    event: dict[str, Any],
    raw: dict[str, Any],
    *,
    orderbook_fetcher: PolymarketOrderbookFetcher | None,
    filters: DiscoveryFilters,
    now: datetime,
) -> tuple[DiscoveredMarket | None, str | None]:
    market_id = str(raw.get("conditionId", "")).strip()
    event_id = str(event.get("id") or event.get("slug") or "").strip()
    market_slug = str(raw.get("slug", "")).strip()
    question = str(raw.get("question", "")).strip()
    active = bool(raw.get("active"))
    closed = bool(raw.get("closed"))
    accepting_orders = bool(raw.get("acceptingOrders"))
    close_time = parse_datetime(raw.get("endDate") or event.get("endDate"))
    updated_at = parse_datetime(raw.get("updatedAt") or event.get("updatedAt"))
    outcomes = _json_field(raw.get("outcomes"))
    token_ids = _json_field(raw.get("clobTokenIds"))
    rules = "\n".join(
        text.strip()
        for text in (
            str(raw.get("description", "")),
            str(raw.get("resolutionSource", "")),
        )
        if text.strip()
    )
    is_multivariate = bool(raw.get("negRisk") or event.get("negRisk"))

    if not market_id or not event_id or not market_slug:
        return None, "missing_identifiers"
    if not active or closed or not accepting_orders:
        return None, "not_open"
    if close_time is None or close_time <= now:
        return None, "stale_or_expired"
    if filters.exclude_multivariate and is_multivariate:
        return None, "multivariate"
    if len(question) < 8:
        return None, "unclear_question"
    if filters.require_rules and len(rules) < 20:
        return None, "unclear_rules"
    if not isinstance(outcomes, list) or [str(value).lower() for value in outcomes] != ["yes", "no"]:
        return None, "unsupported_outcomes"
    if not isinstance(token_ids, list) or len(token_ids) != 2 or not all(str(value) for value in token_ids):
        return None, "missing_token_ids"
    if not raw.get("enableOrderBook"):
        return None, "orderbook_disabled"

    volume_24h = float_value(raw.get("volume24hr", event.get("volume24hr")))
    volume_total = float_value(raw.get("volumeNum", raw.get("volume")))
    liquidity = float_value(raw.get("liquidityNum", raw.get("liquidity", event.get("liquidity"))))
    if volume_24h < filters.min_volume_24h:
        return None, "low_24h_volume"
    if volume_total < filters.min_total_volume:
        return None, "low_total_volume"

    best_yes_bid = _valid_price(raw.get("bestBid"))
    best_yes_ask = _valid_price(raw.get("bestAsk"))
    best_no_bid = _complement(best_yes_ask)
    best_no_ask = _complement(best_yes_bid)
    yes_depth = 0.0
    no_depth = 0.0

    if orderbook_fetcher is not None:
        try:
            yes_payload = orderbook_fetcher(str(token_ids[0]))
            no_payload = orderbook_fetcher(str(token_ids[1]))
        except (OSError, RuntimeError, ValueError):
            return None, "orderbook_unavailable"
        if yes_payload is None or no_payload is None:
            return None, "orderbook_unavailable"
        yes_metrics = _book_metrics(yes_payload)
        no_metrics = _book_metrics(no_payload)
        best_yes_bid = yes_metrics["best_bid"]
        best_yes_ask = yes_metrics["best_ask"]
        best_no_bid = no_metrics["best_bid"]
        best_no_ask = no_metrics["best_ask"]
        yes_depth = yes_metrics["ask_depth"]
        no_depth = no_metrics["ask_depth"]

    spread = _spread(best_yes_bid, best_yes_ask)
    if spread is None:
        return None, "empty_quotes"
    if spread > filters.max_spread:
        return None, "wide_spread"
    if filters.require_two_sided:
        if yes_depth < filters.min_depth or no_depth < filters.min_depth:
            return None, "insufficient_depth"
    elif max(yes_depth, no_depth) < filters.min_depth:
        return None, "insufficient_depth"

    return (
        DiscoveredMarket(
            venue="polymarket",
            market_id=market_id,
            event_id=event_id,
            series_id=str(event.get("seriesSlug") or event.get("ticker") or ""),
            slug=market_slug,
            question=question,
            yes_label=str(outcomes[0]),
            no_label=str(outcomes[1]),
            close_time=close_time,
            updated_at=updated_at,
            rules=rules,
            status="active",
            volume_24h=volume_24h,
            volume_total=volume_total,
            open_interest=float_value(raw.get("openInterest", event.get("openInterest"))),
            liquidity=liquidity,
            spread=spread,
            best_yes_bid=best_yes_bid,
            best_yes_ask=best_yes_ask,
            best_no_bid=best_no_bid,
            best_no_ask=best_no_ask,
            yes_depth=yes_depth,
            no_depth=no_depth,
            identifiers={
                "condition_id": market_id,
                "event_id": event_id,
                "event_slug": str(event.get("slug", "")),
                "market_slug": market_slug,
                "yes_token_id": str(token_ids[0]),
                "no_token_id": str(token_ids[1]),
            },
            is_multivariate=is_multivariate,
        ),
        None,
    )


def _book_metrics(payload: dict[str, Any] | None) -> dict[str, float | None]:
    if not payload:
        return {"best_bid": None, "best_ask": None, "ask_depth": 0.0}
    bids = _levels(payload.get("bids", []), reverse=True)
    asks = _levels(payload.get("asks", []), reverse=False)
    return {
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "ask_depth": sum(size for _, size in asks),
    }


def _levels(raw_levels: Any, *, reverse: bool) -> list[tuple[float, float]]:
    if not isinstance(raw_levels, list):
        return []
    levels = []
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        price = _valid_price(level.get("price"))
        size = float_value(level.get("size"))
        if price is not None and size > 0:
            levels.append((price, size))
    return sorted(levels, key=lambda value: value[0], reverse=reverse)


def _json_field(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _valid_price(value: Any) -> float | None:
    price = float_value(value, default=-1)
    if 0 < price < 1:
        return price
    return None


def _complement(value: float | None) -> float | None:
    if value is None:
        return None
    return round(1 - value, 10)


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or ask < bid:
        return None
    return round(ask - bid, 10)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
