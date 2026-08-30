"""KalshiFeeModel against the pinned schedule.

The worked-example assertions mirror docs/fees_kalshi.md exactly; those
values are in turn traced to the official Kalshi fee schedule (see the
config's source_urls). Rounding rule under test: ceil to whole cent per
ORDER (`ceil_cent_per_order`), not per contract.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from arbx.fees.kalshi import KalshiFeeModel, KalshiFeeSchedule, SeriesFeeOverride

FEES_KALSHI_YAML = Path(__file__).resolve().parents[1] / "configs" / "fees_kalshi.yaml"


@pytest.fixture(scope="module")
def model() -> KalshiFeeModel:
    return KalshiFeeModel(KalshiFeeSchedule.load(FEES_KALSHI_YAML))


def test_general_formula_worked_examples(model: KalshiFeeModel):
    # docs/fees_kalshi.md example 1: 100 @ $0.50 taker -> $1.75 (official PDF row).
    fee = model.taker_fee(price=0.50, count=100, series_ticker="KXHIGHNY-26JUL02-B85")
    assert fee.taker_fee_usd == 1.75
    assert fee.total_usd == 1.75
    assert fee.per_unit_usd == 0.0175
    assert fee.maker_fee_usd == 0.0
    assert fee.settlement_fee_usd == 0.0
    assert fee.gas_usd == 0.0
    assert fee.venue == "kalshi"
    assert fee.source == "formula:v1"

    # Example 2: 100 @ $0.05 taker -> raw $0.3325 -> $0.34 (official PDF row).
    assert model.taker_fee(price=0.05, count=100).taker_fee_usd == 0.34

    # Example 3: 1 @ $0.50 taker -> raw $0.0175 -> $0.02 (official PDF row).
    assert model.taker_fee(price=0.50, count=1).taker_fee_usd == 0.02

    # Example 4: 100 @ $0.50 maker on KXINXY (maker-fee series) -> raw $0.4375 -> $0.44.
    maker = model.maker_fee(price=0.50, count=100, series_ticker="KXINXY-26DEC31-B5000")
    assert maker.maker_fee_usd == 0.44
    assert maker.taker_fee_usd == 0.0
    assert maker.total_usd == 0.44


def test_ceil_rounding_per_order(model: KalshiFeeModel):
    # Any fractional cent rounds UP: 1 @ $0.40 -> raw $0.0168 -> $0.02.
    assert model.taker_fee(price=0.40, count=1).taker_fee_usd == 0.02
    # An exact-cent raw fee does not get bumped: 100 @ $0.50 -> raw $1.75 -> $1.75.
    assert model.taker_fee(price=0.50, count=100).taker_fee_usd == 1.75
    # Rounding is per ORDER, not per contract: one 100-lot order at $0.50 pays
    # $1.75, not 100 x ceil($0.0175) = $2.00.
    single = model.taker_fee(price=0.50, count=1).taker_fee_usd
    assert model.taker_fee(price=0.50, count=100).taker_fee_usd < 100 * single


def test_series_override_applies(model: KalshiFeeModel):
    # KXINXY is a maker-fee series (0.0175 maker multiplier); a general series
    # pays zero maker fee. Both the full market ticker and the bare series
    # ticker resolve the override.
    assert model.maker_fee(price=0.50, count=100, series_ticker="KXINXY").maker_fee_usd == 0.44
    assert (
        model.maker_fee(price=0.50, count=100, series_ticker="KXINXY-26DEC31-B5000").maker_fee_usd
        == 0.44
    )
    assert model.maker_fee(price=0.50, count=100, series_ticker="KXHIGHNY").maker_fee_usd == 0.0
    # The override does not change the taker side (0.07 for KXINXY too).
    assert (
        model.taker_fee(price=0.50, count=100, series_ticker="KXINXY-26DEC31-B5000").taker_fee_usd
        == 1.75
    )


def test_override_prefix_matching_rules():
    # Pinned matching rule: derive the series as the substring before the first
    # `-` unless the override key itself contains `-`, in which case it
    # prefix-matches the full market ticker.
    schedule = KalshiFeeSchedule(
        version=1,
        retrieved_at="2026-07-02",
        source_urls=("https://example.test/fees",),
        taker_multiplier=Decimal("0.07"),
        maker_multiplier=Decimal("0"),
        rounding="ceil_cent_per_order",
        series_overrides={
            "KXINX": SeriesFeeOverride(Decimal("0.035"), Decimal("0")),
            "KXF1-26AUG": SeriesFeeOverride(Decimal("0.14"), Decimal("0")),
        },
    )
    model = KalshiFeeModel(schedule)
    # Bare-series key: exact match on the derived series...
    assert model.taker_fee(price=0.50, count=100, series_ticker="KXINX-26DEC31").taker_fee_usd == 0.88
    # ...and no false prefix hit from a longer series name (KXINXY != KXINX).
    assert (
        model.taker_fee(price=0.50, count=100, series_ticker="KXINXY-26DEC31").taker_fee_usd == 1.75
    )
    # Dashed key: prefix match against the full market ticker at a `-` boundary.
    assert model.taker_fee(price=0.50, count=100, series_ticker="KXF1-26AUG-BVER").taker_fee_usd == 3.50
    assert model.taker_fee(price=0.50, count=100, series_ticker="KXF1-26SEP-BVER").taker_fee_usd == 1.75


def test_price_extremes(model: KalshiFeeModel):
    # Small but NONZERO at both price extremes (worst-case invariant: a real
    # trade never models a zero taker fee).
    low = model.taker_fee(price=0.01, count=1)
    high = model.taker_fee(price=0.99, count=1)
    assert low.taker_fee_usd == 0.01  # raw 0.07 x 0.01 x 0.99 = $0.000693 -> ceil
    assert high.taker_fee_usd == 0.01
    assert low.per_unit_usd > 0
    assert high.per_unit_usd > 0


def test_unknown_series_uses_general(model: KalshiFeeModel):
    unknown = model.taker_fee(price=0.50, count=100, series_ticker="KXNOSUCHSERIES-26DEC31-T1")
    baseline = model.taker_fee(price=0.50, count=100, series_ticker=None)
    assert unknown.taker_fee_usd == baseline.taker_fee_usd == 1.75
    assert unknown.source == "formula:v1"
    # General maker multiplier is 0 -> unknown series pays no maker fee.
    assert (
        model.maker_fee(price=0.50, count=100, series_ticker="KXNOSUCHSERIES-26DEC31-T1").maker_fee_usd
        == 0.0
    )
