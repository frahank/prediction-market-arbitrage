# Tests targeted public Polymarket event discovery.
from __future__ import annotations

import scripts.discover_polymarket_public_markets as discovery_script


def test_event_slug_fetcher_fetches_once_and_deduplicates(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url: str):
        calls.append(url)
        return [{"id": "event-1", "slug": "same-event"}]

    monkeypatch.setattr(discovery_script, "_fetch_json", fake_fetch)
    fetch_page = discovery_script._event_slug_fetcher(
        ["same-event", " same-event ", "other-event"]
    )

    assert fetch_page(0, 100) == [{"id": "event-1", "slug": "same-event"}]
    assert fetch_page(100, 100) == []
    assert len(calls) == 2


def test_event_slug_fetcher_respects_page_limit(monkeypatch):
    def fake_fetch(url: str):
        slug = url.rsplit("=", 1)[-1]
        return [{"id": slug, "slug": slug}]

    monkeypatch.setattr(discovery_script, "_fetch_json", fake_fetch)
    fetch_page = discovery_script._event_slug_fetcher(["one", "two"])

    assert len(fetch_page(0, 1)) == 1
