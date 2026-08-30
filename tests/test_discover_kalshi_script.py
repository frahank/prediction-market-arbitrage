# Scope: BOT_RUNTIME — Tests targeted public Kalshi series discovery.
from __future__ import annotations

import scripts.discover_kalshi_public_markets as discovery_script


def test_series_ticker_fetcher_fetches_once_and_deduplicates(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url: str):
        calls.append(url)
        return {"markets": [{"ticker": "KX-ONE"}], "cursor": ""}

    monkeypatch.setattr(discovery_script, "_fetch_json", fake_fetch)
    fetch_page = discovery_script._series_ticker_fetcher(
        ["KXBTCMAXY", " KXBTCMAXY ", "KXHIGHNY"]
    )

    assert fetch_page(None, 100) == {
        "markets": [{"ticker": "KX-ONE"}],
        "cursor": "",
    }
    assert fetch_page("next", 100) == {"markets": [], "cursor": ""}
    assert len(calls) == 2


def test_series_ticker_fetcher_respects_page_limit(monkeypatch):
    def fake_fetch(url: str):
        return {
            "markets": [
                {"ticker": "KX-ONE"},
                {"ticker": "KX-TWO"},
            ],
            "cursor": "",
        }

    monkeypatch.setattr(discovery_script, "_fetch_json", fake_fetch)
    fetch_page = discovery_script._series_ticker_fetcher(["KXBTCMAXY"])

    assert len(fetch_page(None, 1)["markets"]) == 1
