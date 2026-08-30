import json
from datetime import datetime, timezone

from arbx.pairs.discovery.models import DiscoveryFilters, save_discovery_result
from arbx.pairs.discovery.polymarket import discover_polymarket_markets

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def poly_market(
    condition_id: str,
    *,
    question: str = "Will the discovery test pass?",
    description: str = "Resolves Yes if the discovery test passes under the published rules.",
    active: bool = True,
    closed: bool = False,
    accepting_orders: bool = True,
    end_date: str = "2026-12-31T23:00:00Z",
    volume_24h: float = 25.0,
    volume_total: float = 100.0,
    best_bid: float = 0.40,
    best_ask: float = 0.45,
    neg_risk: bool = False,
):
    return {
        "conditionId": condition_id,
        "slug": f"market-{condition_id}",
        "question": question,
        "description": description,
        "resolutionSource": "https://example.com/resolution",
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "enableOrderBook": True,
        "endDate": end_date,
        "updatedAt": "2026-06-23T10:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([f"{condition_id}-yes", f"{condition_id}-no"]),
        "volume24hr": volume_24h,
        "volumeNum": volume_total,
        "liquidityNum": 500.0,
        "openInterest": 50.0,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "negRisk": neg_risk,
    }


def event(event_id: str, markets):
    return {
        "id": event_id,
        "slug": f"event-{event_id}",
        "ticker": f"TICKER-{event_id}",
        "markets": markets,
    }


def book(token_id: str):
    if token_id.endswith("-yes"):
        bid, ask = "0.40", "0.45"
    else:
        bid, ask = "0.55", "0.60"
    return {
        "asset_id": token_id,
        "bids": [{"price": bid, "size": "4"}],
        "asks": [{"price": ask, "size": "5"}],
    }


def test_polymarket_discovery_paginates_filters_tokens_and_enriches_depth():
    page_calls = []
    pages = {
        0: [
            event("one", [poly_market("GOOD-A", volume_24h=10)]),
            event("two", [poly_market("CLOSED", closed=True)]),
        ],
        2: [
            event(
                "three",
                [
                    poly_market("GOOD-B", volume_24h=30),
                    poly_market("MULTI", neg_risk=True),
                    poly_market("UNCLEAR", description="", question="Bad"),
                    poly_market("STALE", end_date="2026-01-01T00:00:00Z"),
                ],
            )
        ],
    }

    def fetch_page(offset, limit):
        page_calls.append((offset, limit))
        return pages[offset]

    result = discover_polymarket_markets(
        fetch_page,
        orderbook_fetcher=book,
        filters=DiscoveryFilters(min_volume_24h=1, min_depth=1, max_spread=0.10),
        now=NOW,
        page_size=2,
    )

    assert page_calls == [(0, 2), (2, 2)]
    assert result.stats.pages_fetched == 2
    assert result.stats.records_seen == 6
    assert result.stats.accepted == 2
    assert result.stats.rejected_by_reason == {
        "multivariate": 1,
        "not_open": 1,
        "stale_or_expired": 1,
        "unclear_question": 1,
    }
    assert [market.market_id for market in result.markets] == ["GOOD-B", "GOOD-A"]

    first = result.markets[0]
    assert first.identifiers["yes_token_id"] == "GOOD-B-yes"
    assert first.identifiers["no_token_id"] == "GOOD-B-no"
    assert first.yes_depth == 5.0
    assert first.no_depth == 5.0
    assert first.best_yes_ask == 0.45
    assert first.best_no_ask == 0.60
    assert first.spread == 0.05


def test_polymarket_discovery_rejects_missing_tokens_and_empty_books():
    missing_tokens = poly_market("MISSING")
    missing_tokens["clobTokenIds"] = "[]"

    result = discover_polymarket_markets(
        lambda _offset, _limit: [event("one", [missing_tokens, poly_market("EMPTY")])],
        orderbook_fetcher=lambda _token_id: {"bids": [], "asks": []},
        filters=DiscoveryFilters(min_depth=1, max_spread=0.10),
        now=NOW,
        page_size=100,
    )

    assert result.stats.accepted == 0
    assert result.stats.rejected_by_reason == {
        "empty_quotes": 1,
        "missing_token_ids": 1,
    }


def test_polymarket_discovery_result_saves_machine_readable_json(tmp_path):
    result = discover_polymarket_markets(
        lambda _offset, _limit: [event("one", [poly_market("SAVE")])],
        orderbook_fetcher=book,
        filters=DiscoveryFilters(max_spread=0.10),
        now=NOW,
    )
    path = tmp_path / "polymarket.json"

    save_discovery_result(path, result)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["venue"] == "polymarket"
    assert payload["stats"]["accepted"] == 1
    assert payload["markets"][0]["identifiers"]["condition_id"] == "SAVE"
    assert payload["markets"][0]["activity_score"] > 0


def test_polymarket_discovery_metadata_only_requires_depth_filter_disabled():
    result = discover_polymarket_markets(
        lambda _offset, _limit: [event("one", [poly_market("META")])],
        filters=DiscoveryFilters(min_depth=0, max_spread=0.10),
        now=NOW,
    )

    assert result.stats.accepted == 1
    assert result.markets[0].yes_depth == 0.0
    assert result.markets[0].no_depth == 0.0


def test_polymarket_discovery_degrades_one_orderbook_failure_without_aborting():
    def fetch_book(token_id):
        if token_id.startswith("GOOD"):
            return book(token_id)
        return None

    result = discover_polymarket_markets(
        lambda _offset, _limit: [
            event("one", [poly_market("GOOD"), poly_market("BROKEN")])
        ],
        orderbook_fetcher=fetch_book,
        filters=DiscoveryFilters(max_spread=0.10),
        now=NOW,
    )

    assert [market.market_id for market in result.markets] == ["GOOD"]
    assert result.stats.rejected_by_reason == {"orderbook_unavailable": 1}
