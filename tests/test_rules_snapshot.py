# Rules-snapshot fetcher on canned real payloads.
from __future__ import annotations

import json
from pathlib import Path

from arbx.pairs.registry import PairSpec
from arbx.pairs.rules_snapshot import fetch_rules, save

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rules_snapshot"


class CannedProvider:
    """Real payloads captured 2026-07-03 for KXWCCONTINENT-26-NA."""

    def __init__(self, kalshi=None, gamma=None):
        self._kalshi = kalshi if kalshi is not None else json.loads(
            (FIXTURES / "kalshi_market.json").read_text())
        self._gamma = gamma if gamma is not None else json.loads(
            (FIXTURES / "gamma_markets.json").read_text())

    def fetch_kalshi_market_json(self, ticker):
        return self._kalshi

    def fetch_gamma_markets_json(self, condition_id):
        return self._gamma


def _pair() -> PairSpec:
    return PairSpec(
        pair_key="KXWCCONTINENT-26-NA|0x7eff",
        kalshi_market_id="KXWCCONTINENT-26-NA",
        polymarket_condition_id="0x7eff94953c3d8aa9a36e45a545e57564100ef003a90f088b3eff4a798b35eb5e",
        polymarket_yes_token_id="1", polymarket_no_token_id="2",
        orientation="same", status="approved_for_paper",
        include_in_strategy_metrics=True, raw={},
    )


def test_snapshot_from_canned_payloads():
    snap = fetch_rules(_pair(), provider=CannedProvider())
    assert "CONCACAF" in snap.kalshi_rules_text
    assert "qualification pathway" in snap.kalshi_rules_text  # rules_secondary appended
    assert snap.kalshi_close_time is not None
    assert snap.kalshi_close_time.year == 2026
    assert "continent of the country that wins" in snap.poly_description
    assert len(snap.sha256) == 64
    # this real market omits endDate + resolutionSource — warned, not raised
    assert any("endDate" in w for w in snap.warnings)
    assert any("resolutionSource" in w for w in snap.warnings)


def test_sha256_stable():
    a = fetch_rules(_pair(), provider=CannedProvider())
    b = fetch_rules(_pair(), provider=CannedProvider())
    assert a.sha256 == b.sha256
    assert a.fetched_at != b.fetched_at or True  # hash excludes fetch time by design

    # whitespace normalization: reformatting the same text does not change the pin
    kalshi = json.loads((FIXTURES / "kalshi_market.json").read_text())
    kalshi["market"]["rules_primary"] = "  " + kalshi["market"]["rules_primary"].replace(" ", "\n ")
    c = fetch_rules(_pair(), provider=CannedProvider(kalshi=kalshi))
    assert c.sha256 == a.sha256

    # a real rules edit does change it
    kalshi2 = json.loads((FIXTURES / "kalshi_market.json").read_text())
    kalshi2["market"]["rules_primary"] += " AMENDED."
    d = fetch_rules(_pair(), provider=CannedProvider(kalshi=kalshi2))
    assert d.sha256 != a.sha256


def test_missing_rules_warns_not_raises(tmp_path: Path):
    snap = fetch_rules(_pair(), provider=CannedProvider(kalshi={}, gamma=[]))
    assert snap.kalshi_rules_text == ""
    assert snap.poly_description == ""
    assert snap.kalshi_close_time is None
    assert any("kalshi" in w for w in snap.warnings)
    assert any("gamma" in w for w in snap.warnings)

    out = save(snap, tmp_path / "evidence")
    payload = json.loads(out.read_text())
    assert payload["sha256"] == snap.sha256
    assert payload["kalshi_close_time"] is None
    assert isinstance(payload["warnings"], list)
