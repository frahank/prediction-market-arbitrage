# Scope: TEST — P4-T3 equivalence prescreen, audit prompt, verdict parsing.
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbx.pairs.equivalence import (
    build_audit_prompt,
    parse_audit_verdict,
    prescreen,
)
from arbx.pairs.registry import PairSpec
from arbx.pairs.rules_snapshot import RulesSnapshot, _rules_sha256

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


def _pair(raw: dict | None = None) -> PairSpec:
    return PairSpec(
        pair_key="KXTEST-26|0xabc", kalshi_market_id="KXTEST-26",
        polymarket_condition_id="0xabc", polymarket_yes_token_id="1",
        polymarket_no_token_id="2", orientation="same",
        status="approved_for_paper", include_in_strategy_metrics=True,
        raw=raw or {},
    )


def _snapshot(kalshi_rules: str, poly_desc: str,
              k_close: datetime | None, p_end: datetime | None,
              warnings: tuple[str, ...] = ()) -> RulesSnapshot:
    return RulesSnapshot(
        pair_key="KXTEST-26|0xabc", kalshi_market_id="KXTEST-26",
        polymarket_condition_id="0xabc",
        kalshi_rules_text=kalshi_rules, kalshi_close_time=k_close,
        poly_description=poly_desc, poly_resolution_source="",
        poly_end_date=p_end, fetched_at=NOW,
        sha256=_rules_sha256(kalshi_rules, poly_desc, ""),
        warnings=warnings,
    )


def test_date_delta_flagged():
    snap = _snapshot(
        "If X wins the match, resolves Yes.", "Resolves Yes if X wins the match.",
        datetime(2026, 8, 1, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 4, 59, tzinfo=timezone.utc),
    )
    result = prescreen(_pair(), snap, now=NOW)
    assert any(f.startswith("R4:") for f in result.flags)
    assert result.score == "flagged"

    # within ±1h → no R4 flag
    snap2 = _snapshot(
        "If X wins the match, resolves Yes.", "Resolves Yes if X wins the match.",
        datetime(2026, 8, 1, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc),
    )
    result2 = prescreen(_pair(), snap2, now=NOW)
    assert not any(f.startswith("R4:") for f in result2.flags)


def test_long_dated_flagged():
    snap = _snapshot(
        "If candidate wins and accepts the nomination, resolves Yes.",
        "Resolves Yes if the candidate wins and accepts the nomination.",
        datetime(2028, 11, 7, 15, tzinfo=timezone.utc),
        datetime(2028, 11, 7, 15, tzinfo=timezone.utc),
    )
    result = prescreen(_pair(), snap, now=NOW)
    assert any(f.startswith("R5:") for f in result.flags)


def test_subjective_language_flagged_and_registry_fallback():
    # No close times in the snapshot → registry raw fallback used for R4/R5.
    snap = _snapshot(
        "If any federal agency definitively states that aliens exist, resolves Yes.",
        "Resolves Yes if the government definitively states aliens exist.",
        None, None,
    )
    raw = {"kalshi_close_time": "2027-01-01T15:00:00Z",
           "polymarket_close_time": "2026-12-31T00:00:00Z"}
    result = prescreen(_pair(raw), snap, now=NOW)
    assert result.structure == "subjective"
    assert any(f.startswith("R2:") for f in result.flags)
    assert any(f.startswith("R4:") and "delta" in f for f in result.flags)
    assert any(f.startswith("R5:") for f in result.flags)


def test_prompt_contains_both_rule_texts():
    kalshi_rules = "If a CONCACAF country wins the 2026 World Cup, resolves Yes."
    poly_desc = "Resolves to the continent of the winning country."
    snap = _snapshot(kalshi_rules, poly_desc,
                     datetime(2026, 8, 3, tzinfo=timezone.utc), None)
    prompt = build_audit_prompt(_pair(), snap)
    assert kalshi_rules in prompt
    assert poly_desc in prompt
    assert snap.sha256 in prompt
    # calibration anchors + procedure + strict output contract
    assert "KXWCCONTINENT-26-EUR" in prompt and "Guyana/Suriname" in prompt
    assert "GROUPING CHECK" in prompt and "DATE CHECK" in prompt
    assert '"status"' in prompt


CANNED_RESPONSE = """The contracts are equivalent for all realistic winners...

```json
{"status": "verified_equivalent",
 "tail_risks": ["Guyana/Suriname CONCACAF winner -> Poly YES / Kalshi NO"],
 "grouping_check": "CONMEBOL and WPR South America partition realistic winners identically.",
 "date_check": "Cutoffs differ by 14 days after the final; no in-window event flips one venue.",
 "confidence": 0.93}
```
"""


def test_verdict_json_schema_parses():
    verdict = parse_audit_verdict(CANNED_RESPONSE)
    assert verdict["status"] == "verified_equivalent"
    assert verdict["tail_risks"]
    assert 0 <= verdict["confidence"] <= 1

    with pytest.raises(ValueError):
        parse_audit_verdict("no json here")
    with pytest.raises(ValueError):
        parse_audit_verdict('```json\n{"status": "fine", "tail_risks": [], '
                            '"grouping_check": "x", "date_check": "y", '
                            '"confidence": 0.5}\n```')
    with pytest.raises(ValueError):
        parse_audit_verdict(CANNED_RESPONSE.replace("0.93", "1.7"))


def test_prescreen_on_real_lead_pair_snapshot():
    """End-to-end over the committed WC-NA fixtures: grouping structure, flags."""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "rules_snapshot"
    from arbx.pairs.rules_snapshot import fetch_rules

    class Canned:
        def fetch_kalshi_market_json(self, t):
            return json.loads((fixtures / "kalshi_market.json").read_text())

        def fetch_gamma_markets_json(self, c):
            return json.loads((fixtures / "gamma_markets.json").read_text())

    raw = {"kalshi_close_time": "2026-08-03T00:00:00Z",
           "polymarket_close_time": "2026-07-20T00:00:00Z"}
    snap = fetch_rules(_pair(raw), provider=Canned())
    result = prescreen(_pair(raw), snap, now=NOW)
    # The live Poly description says a "consensus of credible reporting may also
    # be used" — real subjective fallback language inside a grouping market.
    # Subjective dominates by design (highest basis risk wins the classification).
    assert result.structure == "subjective"
    assert "continent" in snap.poly_description  # the grouping layer is still there
    assert any(f.startswith("R2:") for f in result.flags)
    assert any(f.startswith("R4:") for f in result.flags)  # 14-day resolution asymmetry
