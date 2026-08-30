# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Actual public-data paper bot operation; no live orders.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.pairs.discovery.models import (  # noqa: E402 — import after sys.path setup
    DiscoveryFilters,
    DiscoveryResult,
    save_discovery_result,
)
from arbx.pairs.discovery.polymarket import (
    discover_polymarket_markets,  # noqa: E402 — import after sys.path setup
)

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
DEFAULT_OUTPUT = ROOT / "logs" / "polymarket_discovered_markets.json"
PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "arbx-polymarket-discovery/0.1",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover usable public Polymarket markets.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--min-volume-24h", type=float, default=1.0)
    parser.add_argument("--min-total-volume", type=float, default=1.0)
    parser.add_argument("--min-depth", type=float, default=1.0)
    parser.add_argument("--max-spread", type=float, default=0.25)
    parser.add_argument("--require-two-sided", action="store_true")
    parser.add_argument("--include-multivariate", action="store_true")
    parser.add_argument("--skip-orderbooks", action="store_true")
    parser.add_argument(
        "--event-slug",
        action="append",
        default=[],
        help=(
            "Fetch an exact Polymarket event slug. Repeat for multiple events. "
            "When supplied, broad event pagination is skipped."
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
        _event_slug_fetcher(args.event_slug)
        if args.event_slug
        else _fetch_event_page
    )
    result = discover_polymarket_markets(
        fetch_page,
        orderbook_fetcher=None if args.skip_orderbooks else _fetch_orderbook,
        filters=filters,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    output_result = _limit_result(result, args.max_markets)
    save_discovery_result(args.output, output_result)

    print(
        "Polymarket discovery complete: "
        f"seen={result.stats.records_seen} "
        f"accepted={result.stats.accepted} "
        f"saved={len(output_result.markets)} "
        f"pages={result.stats.pages_fetched}"
    )
    print(f"Output: {args.output}")
    return 0


def _fetch_event_page(offset: int, limit: int) -> list[dict]:
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume_24hr",
        "ascending": "false",
        "limit": limit,
        "offset": offset,
    }
    payload = _fetch_json(f"{GAMMA_URL}?{urlencode(params)}")
    if not isinstance(payload, list):
        raise RuntimeError("Polymarket Gamma endpoint returned a non-list response")
    return [record for record in payload if isinstance(record, dict)]


def _event_slug_fetcher(slugs: list[str]):
    normalized = tuple(dict.fromkeys(slug.strip() for slug in slugs if slug.strip()))

    def fetch_page(offset: int, limit: int) -> list[dict]:
        if offset:
            return []
        events: list[dict] = []
        seen: set[str] = set()
        for slug in normalized:
            payload = _fetch_json(f"{GAMMA_URL}?{urlencode({'slug': slug})}")
            if not isinstance(payload, list):
                raise RuntimeError("Polymarket Gamma slug endpoint returned a non-list response")
            for record in payload:
                if not isinstance(record, dict):
                    continue
                event_id = str(record.get("id") or record.get("slug") or "")
                if event_id and event_id not in seen:
                    seen.add(event_id)
                    events.append(record)
        return events[:limit]

    return fetch_page


def _fetch_orderbook(token_id: str) -> dict | None:
    payload = _fetch_json(f"{CLOB_URL}/book?token_id={quote(token_id, safe='')}")
    return payload if isinstance(payload, dict) else None


def _fetch_json(url: str):
    request = Request(url, headers=PUBLIC_HEADERS, method="GET")
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


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
