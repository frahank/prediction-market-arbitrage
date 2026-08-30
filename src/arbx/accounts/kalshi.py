# Scope: BOT_RUNTIME — Kalshi read-only account client (GET-only, no order capability).
"""KalshiAccountClient: authenticated *reads* of account state, nothing else.

Endpoints verified against docs.kalshi.com (2026-07-06,
``api-reference/portfolio/*`` and ``api-reference/orders/get-orders``), all
``GET`` on base ``https://external-api.kalshi.com/trade-api/v2``:

- ``/portfolio/balance`` — ``balance`` (cents int) + ``balance_dollars``
  (fixed-point dollar string, preferred here);
- ``/portfolio/positions`` — ``market_positions`` list, cursor-paginated;
- ``/portfolio/orders?status=resting`` — resting orders list, cursor-paginated;
- ``/portfolio/fills`` — ``fills`` list, ``min_ts``/``max_ts`` in Unix
  *seconds*, cursor-paginated.

Signing: every request signs the path without its query string (docs:
"use the path without query parameters") via :class:`KalshiSigner`.

This class is GET-only by construction and read-only by type: it has no
place/cancel/amend surface, enforced by ``tests/test_accounts_readonly.py``.
Error messages carry the endpoint and HTTP status only — never payloads or
headers, which could echo credential material.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from arbx.accounts.kalshi_auth import KALSHI_REST_BASE, KalshiSigner
from arbx.venues.http import MockableHttpClient

# Cursor-pagination hard stop: account reads are dashboards, not dumps.
_MAX_PAGES = 10
_PAGE_LIMIT = 200


class KalshiAccountError(RuntimeError):
    """Account-read failure; message is endpoint+status only, never payload."""


class KalshiAccountClient:
    """Read-only authenticated Kalshi account state (AccountClient shape)."""

    venue = "kalshi"

    def __init__(
        self,
        signer: KalshiSigner,
        *,
        base_url: str = KALSHI_REST_BASE,
        http: MockableHttpClient | None = None,
    ) -> None:
        self._signer = signer
        self._base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self._base_url).path
        self._http = http or MockableHttpClient()

    async def balance_usd(self) -> float:
        payload = await self._get_json("/portfolio/balance")
        dollars = payload.get("balance_dollars")
        if isinstance(dollars, str) and dollars.strip():
            return float(dollars)
        cents = payload.get("balance")
        if isinstance(cents, (int, float)):
            return float(cents) / 100.0
        raise KalshiAccountError("/portfolio/balance response had no balance field")

    async def positions(self) -> list[dict[str, Any]]:
        return await self._paginated("/portfolio/positions", "market_positions")

    async def open_orders(self) -> list[dict[str, Any]]:
        return await self._paginated("/portfolio/orders", "orders", {"status": "resting"})

    async def fills(self, since: datetime | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if since is not None:
            params["min_ts"] = str(int(since.timestamp()))  # docs: Unix seconds
        return await self._paginated("/portfolio/fills", "fills", params)

    # -- transport ----------------------------------------------------------

    async def _paginated(
        self,
        endpoint: str,
        items_key: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page_params = dict(params or {})
            page_params["limit"] = str(_PAGE_LIMIT)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self._get_json(endpoint, page_params)
            page = payload.get(items_key)
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            cursor = payload.get("cursor") or None
            if not cursor or not page:
                break
        return items

    async def _get_json(
        self,
        endpoint: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        signing_path = f"{self._base_path}{endpoint}"
        url = f"{self._base_url}{endpoint}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = self._signer.headers("GET", signing_path)
        # MockableHttpClient is synchronous; keep the event loop responsive.
        result = await asyncio.to_thread(
            self._http.get_json, url, venue=self.venue, headers=headers
        )
        if not 200 <= result.status_code < 300:
            raise KalshiAccountError(
                f"kalshi GET {endpoint} failed with status {result.status_code} "
                f"after {result.attempts} attempt(s)"
            )
        return result.payload or {}
