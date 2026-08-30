# EV model with carry, survival, and failed-leg costs.
from __future__ import annotations

import pytest

from arbx.analysis.episodes import Episode
from arbx.modeling.ev import (
    VERDICT_MARGINAL,
    VERDICT_NOT_VIABLE,
    VERDICT_VIABLE,
    EVParams,
    ViabilityThresholds,
    pair_ev,
    verdict,
)
from arbx.modeling.executable import ExecutableEdgeParams
from arbx.pairs.registry import PairSpec, PairTaxonomy

PAIR_KEY = "KXTEST-26|0xabc"


def _pair(ttr_days: float) -> PairSpec:
    return PairSpec(
        pair_key=PAIR_KEY,
        kalshi_market_id="KXTEST-26",
        polymarket_condition_id="0xcond",
        polymarket_yes_token_id="0xyes",
        polymarket_no_token_id="0xno",
        orientation="yes_yes",
        status="approved_for_paper",
        include_in_strategy_metrics=True,
        raw={},
        taxonomy=PairTaxonomy(time_to_resolution_days=ttr_days),
    )


def _episode(
    *,
    snapshots: int = 3,
    median_depth_edge: float = 0.03,
    max_fill_size: float = 100.0,
    skew: float = 10.0,
    tier: str | None = None,
    through: int | None = None,
    basis: bool = False,
) -> Episode:
    return Episode(
        pair_key=PAIR_KEY,
        kalshi_market="KXTEST-26",
        direction="kalshi_yes_poly_no",
        start_ts="2026-07-01T00:00:00+00:00",
        end_ts="2026-07-01T00:01:00+00:00",
        duration_s=60.0,
        snapshots=snapshots,
        peak_edge=median_depth_edge + 0.01,
        median_edge=median_depth_edge,
        median_depth_edge=median_depth_edge,
        max_fill_size=max_fill_size,
        max_abs_skew_ms=skew,
        is_basis_suspect=basis,
        best_survival_tier=tier,
        survived_through_ms=through,
    )


PARAMS = EVParams(executable=ExecutableEdgeParams())


def _ev(episodes, ttr_days=30.0, params=PARAMS, **kwargs):
    return pair_ev(
        episodes,
        _pair(ttr_days),
        params=params,
        soak_days=1.0,
        direction="kalshi_yes_poly_no",
        **kwargs,
    )


def test_carry_scales_with_horizon():
    episodes = [_episode(), _episode()]
    short = _ev(episodes, ttr_days=30.0)
    long = _ev(episodes, ttr_days=858.0)
    # same edge, same fills — only the lockup horizon differs
    assert long.carry_cost_per_unit == pytest.approx(
        short.carry_cost_per_unit * 858.0 / 30.0
    )
    assert long.ev_per_day_usd < short.ev_per_day_usd
    # a 2028 pair at 5% APR carries >10c/unit — dwarfs a 3c edge
    assert long.carry_cost_per_unit > 0.10
    assert long.ev_per_day_usd < 0 < short.ev_per_day_usd


def test_leg_failure_cost_reduces_ev():
    episodes = [_episode(), _episode()]
    no_failure = _ev(episodes, params=EVParams(leg_failure_prob=0.0))
    with_failure = _ev(episodes, params=EVParams(leg_failure_prob=0.10))
    assert with_failure.ev_per_day_usd < no_failure.ev_per_day_usd
    assert no_failure.failed_leg_expected_cost_per_unit == 0.0
    assert with_failure.failed_leg_expected_cost_per_unit == pytest.approx(
        0.10 * with_failure.unwind_cost_per_unit
    )
    # cross_spread unwind uses the measured spread when provided
    spread = _ev(episodes, unwind_spread=0.05)
    assert spread.unwind_cost_per_unit == 0.05


def test_ev_zero_when_no_qualifying_episodes():
    assert _ev([]).ev_per_day_usd == 0.0
    # blips, over-skew captures, and basis-suspect episodes never qualify
    blip = _episode(snapshots=1)
    skewed = _episode(skew=500.0)
    basis = _episode(basis=True)
    out = _ev([blip, skewed, basis])
    assert out.qualifying_episodes == 0
    assert out.ev_per_opportunity_usd == 0.0
    assert out.ev_per_day_usd == 0.0
    assert out.fill_probability_basis == "no_episodes"


def test_breakdown_fields_sum_consistently():
    episodes = [
        _episode(tier="survived_500ms", through=500),
        _episode(median_depth_edge=0.02),
    ]
    b = _ev(episodes, fees_per_unit=0.015, unwind_spread=0.03)
    assert b.survival_tier == "survived_500ms"
    assert b.survival_probability == PARAMS.fill_probability_tiers["survived_500ms"]
    recomputed_per_unit = (
        b.survival_probability * (b.gross_edge_per_unit - b.fees_per_unit - b.carry_cost_per_unit)
        - b.failed_leg_expected_cost_per_unit
    )
    assert b.ev_per_opportunity_usd == pytest.approx(recomputed_per_unit * b.expected_fill_size)
    assert b.ev_per_day_usd == pytest.approx(
        b.ev_per_opportunity_usd * b.opportunities_per_day
    )
    assert b.net_edge_per_unit == pytest.approx(
        b.gross_edge_per_unit - b.fees_per_unit - b.carry_cost_per_unit
    )
    assert b.opportunities_per_day == 2.0  # 2 episodes / 1 soak day


def test_unprobed_pairs_take_the_unprobed_knob():
    b = _ev([_episode(), _episode()])
    assert b.fill_probability_basis == "unprobed"
    assert b.survival_probability == PARAMS.unprobed_fill_probability


def test_verdict_thresholds():
    thresholds = ViabilityThresholds(min_ev_per_day_usd=5.0, min_opportunities_per_week=5.0)
    strong = _ev([_episode(tier="survived_1000ms", through=1000, max_fill_size=500.0)] * 3)
    assert strong.ev_per_day_usd >= 5.0
    assert verdict(strong, thresholds) == VERDICT_VIABLE
    weak = _ev([_episode(max_fill_size=2.0)])
    assert 0 < weak.ev_per_day_usd < 5.0
    assert verdict(weak, thresholds) == VERDICT_MARGINAL
    assert verdict(_ev([]), thresholds) == VERDICT_NOT_VIABLE
