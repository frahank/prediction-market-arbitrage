# Scope: BOT_RUNTIME — Actual public-data paper bot operation; no live orders.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.pairs.discovery.kalshi import (
    discover_kalshi_markets,  # noqa: E402 — import after sys.path setup
)
from arbx.pairs.discovery.models import (  # noqa: E402 — import after sys.path setup
    DiscoveryFilters,
    DiscoveryResult,
    save_discovery_result,
)

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_OUTPUT = ROOT / "logs" / "kalshi_discovered_markets.json"
PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "arbx-kalshi-discovery/0.1",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover usable public Kalshi markets.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--min-volume-24h", type=float, default=1.0)
    parser.add_argument("--min-total-volume", type=float, default=1.0)
    parser.add_argument("--min-depth", type=float, default=1.0)
    parser.add_argument("--max-spread", type=float, default=0.25)
    parser.add_argument("--require-two-sided", action="store_true")
    parser.add_argument("--include-multivariate", action="store_true")
    parser.add_argument("--include-trades", action="store_true")
    parser.add_argument("--max-trade-pages", type=int, default=5)
    parser.add_argument("--skip-orderbooks", action="store_true")
    parser.add_argument(
        "--series-ticker",
        action="append",
        default=[],
        help=(
            "Fetch an exact Kalshi series ticker. Repeat for multiple series. "
            "When supplied, broad market pagination is skipped."
        ),
    )
    args = parser.parse_args(argv)

    filters = DiscoveryFilters(
        min_volume_24h=args.min_volume_24h,
        min_total_volume=args.min_total_volume,
        min_depth=0.0 if args.skip_orderbooks else args.min_depth,
        max_spread=args.max_spread,
        require_two_sided=args.require_two_sided,
        exclude_multivariate=not args.include_multivariate,
    )
    fetch_page = (
        _series_ticker_fetcher(args.series_ticker)
        if args.series_ticker
        else _fetch_market_page
    )
    result = discover_kalshi_markets(
        fetch_page,
        orderbook_fetcher=None if args.skip_orderbooks else _fetch_orderbook,
        trade_page_fetcher=_fetch_trade_page if args.include_trades else None,
        filters=filters,
        page_size=args.page_size,
        max_pages=args.max_pages,
        max_trade_pages=args.max_trade_pages,
    )
    output_result = _limit_result(result, args.max_markets)
    save_discovery_result(args.output, output_result)

    print(
        "Kalshi discovery complete: "
        f"seen={result.stats.records_seen} "
        f"accepted={result.stats.accepted} "
        f"saved={len(output_result.markets)} "
        f"pages={result.stats.pages_fetched}"
    )
    print(f"Output: {args.output}")
    return 0


def _fetch_market_page(cursor: str | None, limit: int) -> dict:
    params = {
        "status": "open",
        "limit": limit,
        "mve_filter": "exclude",
    }
    if cursor:
        params["cursor"] = cursor
    return _fetch_json(f"{BASE_URL}/markets?{urlencode(params)}")


def _series_ticker_fetcher(series_tickers: list[str]):
    normalized = tuple(
        dict.fromkeys(ticker.strip() for ticker in series_tickers if ticker.strip())
    )

    def fetch_page(cursor: str | None, limit: int) -> dict:
        if cursor:
            return {"markets": [], "cursor": ""}
        markets: list[dict] = []
        seen: set[str] = set()
        for series_ticker in normalized:
            params = {
                "status": "open",
                "limit": min(limit, 1000),
                "mve_filter": "exclude",
                "series_ticker": series_ticker,
            }
            payload = _fetch_json(f"{BASE_URL}/markets?{urlencode(params)}")
            raw_markets = payload.get("markets", [])
            if not isinstance(raw_markets, list):
                raise RuntimeError("Kalshi series response must contain a markets list")
            for record in raw_markets:
                if not isinstance(record, dict):
                    continue
                ticker = str(record.get("ticker", ""))
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    markets.append(record)
        return {"markets": markets[:limit], "cursor": ""}

    return fetch_page


def _fetch_trade_page(cursor: str | None, limit: int) -> dict:
    params = {"limit": limit, "is_block_trade": "false"}
    if cursor:
        params["cursor"] = cursor
    return _fetch_json(f"{BASE_URL}/markets/trades?{urlencode(params)}")


def _fetch_orderbook(ticker: str) -> dict | None:
    return _fetch_json(f"{BASE_URL}/markets/{ticker}/orderbook?depth=100")


def _fetch_json(url: str) -> dict:
    request = Request(url, headers=PUBLIC_HEADERS, method="GET")
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Kalshi public endpoint returned a non-object response")
    return payload


def _limit_result(result: DiscoveryResult, max_markets: int) -> DiscoveryResult:
    if max_markets <= 0:
        return result
    return DiscoveryResult(
        venue=result.venue,
        generated_at=result.generated_at,
        markets=result.markets[:max_markets],
        stats=result.stats,
    )


if __name__ == "__main__":
    raise SystemExit(main())
