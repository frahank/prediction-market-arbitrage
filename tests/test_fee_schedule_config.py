"""Pinned Kalshi fee-schedule config sanity.

The numbers themselves are verified against the official sources cited in
configs/fees_kalshi.yaml and docs/fees_kalshi.md; this test guards the
config's shape and traceability so a later edit cannot silently drop the
sources or zero out the fee.
"""

from pathlib import Path

import yaml

FEES_KALSHI_YAML = Path(__file__).resolve().parents[1] / "configs" / "fees_kalshi.yaml"


def test_schedule_loads_and_has_sources():
    config = yaml.safe_load(FEES_KALSHI_YAML.read_text())
    assert config["version"] == 1
    assert config["retrieved_at"]
    assert config["source_urls"], "every fee number must be traceable to source_urls"
    assert all(url.startswith("http") for url in config["source_urls"])
    assert config["general"]["taker_multiplier"] > 0
    assert config["general"]["maker_multiplier"] >= 0
    assert config["general"]["rounding"] == "ceil_cent_per_order"
    for ticker, override in config.get("series_overrides", {}).items():
        assert override["taker_multiplier"] > 0, f"{ticker}: fee must never pin to zero"
