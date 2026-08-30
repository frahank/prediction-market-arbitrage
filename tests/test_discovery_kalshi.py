import json
from datetime import datetime, timezone

from arbx.pairs.discovery.kalshi import discover_kalshi_markets
from arbx.pairs.discovery.models import DiscoveryFilters, save_discovery_result

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def kalshi_market(
    ticker: str,
    *,
    title: str = "Will the discovery test pass?",
    rules: str = "Resolves Yes if the discovery test passes under the published rules.",
    status: str = "active",
    close_time: str = "2026-12-31T23:00:00Z",
    volume_24h: str = "25.00",
    volume_total: str = "100.00",
    yes_bid: str = "0.4000",
    yes_ask: str = "0.4500",
    no_bid: str = "0.5500",
    no_ask: str = "0.6000",
    multivariate: bool = False,
):
    return {
        "ticker": ticker,
        "event_ticker": f"EVENT-{ticker}",
        "title": title,
        "yes_sub_title": "Yes",
        "no_sub_title": "No",
        "status": status,
        "close_time": close_time,
        "updated_time": "2026-06-23T10:00:00Z",
        "rules_primary": rules,
        "rules_secondary": "",
        "volume_24h_fp": volume_24h,
        "volume_fp": volume_total,
        "open_interest_fp": "50.00",
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
        "yes_ask_size_fp": "3.00",
        "mve_collection_ticker": "KXMVE-TEST" if multivariate else "",
    }


def orderbook(yes_size=4.0, no_size=5.0):
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", str(no_size)]],
            "no_dollars": [["0.5500", str(yes_size)]],
        }
    }


def test_kalshi_discovery_paginates_filters_enriches_and_ranks_with_public_trades():
    page_calls = []
    trade_calls = []
    pages = {
        None: {
            "markets": [
                kalshi_market("KXGOOD-A", volume_24h="10"),
                kalshi_market("KXMVE-BAD", multivariate=True),
                kalshi_market("KXEMPTY", yes_bid="0", yes_ask="0", no_bid="0", no_ask="0"),
            ],
            "cursor": "page-2",
        },
        "page-2": {
            "markets": [
                kalshi_market("KXGOOD-B", volume_24h="30"),
                kalshi_market("KXSTALE", close_time="2026-01-01T00:00:00Z"),
                kalshi_market("KXUNCLEAR", rules="short"),
            ],
            "cursor": "",
        },
    }
    trade_pages = {
        None: {
            "trades": [
                {"ticker": "KXGOOD-A", "count_fp": "100.00"},
                {"ticker": "KXGOOD-A", "count_fp": "50.00"},
            ],
            "cursor": "trades-2",
        },
        "trades-2": {
            "trades": [{"ticker": "KXGOOD-B", "count_fp": "1.00"}],
            "cursor": "",
        },
    }

    def fetch_page(cursor, limit):
        page_calls.append((cursor, limit))
        return pages[cursor]

    def fetch_trades(cursor, limit):
        trade_calls.append((cursor, limit))
        return trade_pages[cursor]

    result = discover_kalshi_markets(
        fetch_page,
        orderbook_fetcher=lambda _ticker: orderbook(),
        trade_page_fetcher=fetch_trades,
        filters=DiscoveryFilters(min_volume_24h=1, min_depth=1, max_spread=0.10),
        now=NOW,
        page_size=3,
    )

    assert page_calls == [(None, 3), ("page-2", 3)]
    assert trade_calls == [(None, 1000), ("trades-2", 1000)]
    assert result.stats.pages_fetched == 2
    assert result.stats.records_seen == 6
    assert result.stats.accepted == 2
    assert result.stats.rejected_by_reason == {
        "empty_quotes": 1,
        "multivariate": 1,
        "stale_or_expired": 1,
        "unclear_rules": 1,
    }

    assert [market.market_id for market in result.markets] == ["KXGOOD-A", "KXGOOD-B"]
    first = result.markets[0]
    assert first.yes_depth == 4.0
    assert first.no_depth == 5.0
    assert first.spread == 0.05
    assert first.recent_trade_count == 2
    assert first.recent_trade_volume == 150.0
    assert first.identifiers["event_ticker"] == "EVENT-KXGOOD-A"


def test_kalshi_discovery_rejects_repeated_cursor():
    def fetch_page(_cursor, _limit):
        return {"markets": [], "cursor": "same"}

    try:
        discover_kalshi_markets(fetch_page, now=NOW)
    except ValueError as exc:
        assert "cursor repeated" in str(exc)
    else:
        raise AssertionError("repeated pagination cursor should fail")


def test_kalshi_discovery_result_saves_machine_readable_json(tmp_path):
    result = discover_kalshi_markets(
        lambda _cursor, _limit: {"markets": [kalshi_market("KXSAVE")], "cursor": ""},
        orderbook_fetcher=lambda _ticker: orderbook(),
        filters=DiscoveryFilters(max_spread=0.10),
        now=NOW,
    )
    path = tmp_path / "kalshi.json"

    save_discovery_result(path, result)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["venue"] == "kalshi"
    assert payload["stats"]["accepted"] == 1
    assert payload["markets"][0]["market_id"] == "KXSAVE"
    assert payload["markets"][0]["close_time"] == "2026-12-31T23:00:00+00:00"


def test_kalshi_discovery_can_use_metadata_only_when_depth_filter_is_disabled():
    result = discover_kalshi_markets(
        lambda _cursor, _limit: {"markets": [kalshi_market("KXMETA")], "cursor": ""},
        filters=DiscoveryFilters(min_depth=0, max_spread=0.10),
        now=NOW,
    )

    assert result.stats.accepted == 1
    assert result.markets[0].yes_depth == 3.0
    assert result.markets[0].no_depth == 0.0


def test_kalshi_discovery_degrades_one_orderbook_failure_without_aborting():
    result = discover_kalshi_markets(
        lambda _cursor, _limit: {
            "markets": [kalshi_market("GOOD"), kalshi_market("BROKEN")],
            "cursor": "",
        },
        orderbook_fetcher=lambda ticker: orderbook() if ticker == "GOOD" else None,
        filters=DiscoveryFilters(max_spread=0.10),
        now=NOW,
    )

    assert [market.market_id for market in result.markets] == ["GOOD"]
    assert result.stats.rejected_by_reason == {"orderbook_unavailable": 1}
