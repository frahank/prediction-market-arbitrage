# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Modules B/D/E/F/G: survival-, carry-, and failure-adjusted EV.
"""Per-pair expected-value model:

    EV_per_opportunity =
        P(both_legs_fill) x (depth_adjusted_edge - real_fees - carry_cost)
      - P(one_leg_fails)  x unwind_cost
    EV_per_day = EV_per_opportunity x opportunities_per_day

Everything reads from episode lists + the pair registry; no I/O. All knobs come
from ``configs/modeling.yaml`` (see :func:`EVParams.from_config`):

* **Fill probability** — ``P(both_legs_fill)`` maps the best observed survival tier through
  the ``fill_probability.tiers`` table (survival in a public re-fetch probe is a
  PROXY for fillability, not a fill guarantee; un-probed pairs take the
  ``unprobed`` knob). Every number is provisional until replay and live-fill evidence exist.
* **Carry** — both legs post full collateral released at resolution, so
  ``carry = (kalshi_price + poly_price) x capital_apr x days/365`` with the
  price sum recovered as ``1 - gross_edge``.
* **Failed leg** — with probability ``leg_failure_prob`` one leg fills
  alone; ``cross_spread`` prices the unwind as crossing the filled leg's spread,
  ``hold_to_resolution`` as carrying that leg's collateral to resolution.
* **Opportunity rate** — ``opportunities_per_day = qualifying_episodes / soak_days``.
* **Verdict** — the viable/marginal/not_viable call against operator thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable, Mapping

from arbx.analysis.episodes import Episode
from arbx.modeling.executable import ExecutableEdgeParams

VERDICT_VIABLE = "viable"
VERDICT_MARGINAL = "marginal"
VERDICT_NOT_VIABLE = "not_viable"

UNWIND_CROSS_SPREAD = "cross_spread"
UNWIND_HOLD_TO_RESOLUTION = "hold_to_resolution"

# Placeholder fill-probability table; overridden by configs/modeling.yaml at load time.
DEFAULT_FILL_PROBABILITY_TIERS: Mapping[str, float] = {
    "survived_1000ms": 0.80,
    "survived_500ms": 0.50,
    "survived_250ms": 0.25,
    "survived_100ms": 0.05,
    "survived_25ms": 0.0,
    "expired_lt_250ms": 0.0,
}


@dataclass(frozen=True)
class EVParams:
    capital_apr: float = 0.05
    leg_failure_prob: float = 0.10
    unwind_cost_model: str = UNWIND_CROSS_SPREAD
    unwind_fallback_spread: float = 0.02
    depth_fee_base: float = 0.02
    fill_probability_tiers: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FILL_PROBABILITY_TIERS)
    )
    unprobed_fill_probability: float = 0.25
    executable: ExecutableEdgeParams = ExecutableEdgeParams()

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], executable: ExecutableEdgeParams
    ) -> EVParams:
        """Bind the modeling.yaml expected-value knobs to one skew scenario's
        executable-edge params (``arbx.modeling.executable.load_scenarios``)."""
        carry = config.get("carry") or {}
        failed = config.get("failed_leg") or {}
        fees = config.get("fees") or {}
        fill = config.get("fill_probability") or {}
        model = str(failed.get("unwind_cost_model", UNWIND_CROSS_SPREAD))
        if model not in {UNWIND_CROSS_SPREAD, UNWIND_HOLD_TO_RESOLUTION}:
            raise ValueError(f"unknown unwind_cost_model {model!r}")
        return cls(
            capital_apr=float(carry.get("capital_apr", 0.05)),
            leg_failure_prob=float(failed.get("leg_failure_prob", 0.10)),
            unwind_cost_model=model,
            unwind_fallback_spread=float(failed.get("unwind_fallback_spread", 0.02)),
            depth_fee_base=float(fees.get("depth_fee_base", 0.02)),
            fill_probability_tiers={
                str(k): float(v) for k, v in (fill.get("tiers") or {}).items()
            } or dict(DEFAULT_FILL_PROBABILITY_TIERS),
            unprobed_fill_probability=float(fill.get("unprobed", 0.25)),
            executable=executable,
        )


@dataclass(frozen=True)
class ViabilityThresholds:
    min_ev_per_day_usd: float = 5.00
    min_opportunities_per_week: float = 5.0

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ViabilityThresholds:
        block = config.get("viability") or {}
        return cls(
            min_ev_per_day_usd=float(block.get("viable_min_ev_per_day_usd", 5.00)),
            min_opportunities_per_week=float(
                block.get("viable_min_opportunities_per_week", 5.0)
            ),
        )


@dataclass(frozen=True)
class EVBreakdown:
    pair_key: str
    direction: str
    scenario: str
    validity: str
    qualifying_episodes: int
    soak_days: float
    gross_edge_per_unit: float
    fees_per_unit: float
    survival_probability: float          # P(both_legs_fill); provisional
    survival_tier: str | None
    fill_probability_basis: str          # "tier:<name>" | "unprobed" | "no_episodes"
    expected_fill_size: float            # haircut depth, thinner-leg constrained
    carry_cost_per_unit: float
    unwind_cost_per_unit: float
    failed_leg_expected_cost_per_unit: float
    ev_per_opportunity_usd: float
    opportunities_per_day: float
    ev_per_day_usd: float

    @property
    def opportunities_per_week(self) -> float:
        return self.opportunities_per_day * 7.0

    @property
    def net_edge_per_unit(self) -> float:
        """The fill-probability-weighted term: gross - fees - carry."""
        return self.gross_edge_per_unit - self.fees_per_unit - self.carry_cost_per_unit

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_key": self.pair_key,
            "direction": self.direction,
            "scenario": self.scenario,
            "validity": self.validity,
            "qualifying_episodes": self.qualifying_episodes,
            "soak_days": round(self.soak_days, 3),
            "gross_edge_per_unit": round(self.gross_edge_per_unit, 6),
            "fees_per_unit": round(self.fees_per_unit, 6),
            "net_edge_per_unit": round(self.net_edge_per_unit, 6),
            "survival_probability": self.survival_probability,
            "survival_tier": self.survival_tier,
            "fill_probability_basis": self.fill_probability_basis,
            "expected_fill_size": round(self.expected_fill_size, 2),
            "carry_cost_per_unit": round(self.carry_cost_per_unit, 6),
            "unwind_cost_per_unit": round(self.unwind_cost_per_unit, 6),
            "failed_leg_expected_cost_per_unit": round(
                self.failed_leg_expected_cost_per_unit, 6
            ),
            "ev_per_opportunity_usd": round(self.ev_per_opportunity_usd, 4),
            "opportunities_per_day": round(self.opportunities_per_day, 3),
            "opportunities_per_week": round(self.opportunities_per_week, 3),
            "ev_per_day_usd": round(self.ev_per_day_usd, 4),
        }


def _zero_breakdown(pair_key: str, direction: str, params: EVParams, soak_days: float) -> EVBreakdown:
    return EVBreakdown(
        pair_key=pair_key,
        direction=direction,
        scenario=params.executable.scenario,
        validity=params.executable.validity,
        qualifying_episodes=0,
        soak_days=soak_days,
        gross_edge_per_unit=0.0,
        fees_per_unit=0.0,
        survival_probability=0.0,
        survival_tier=None,
        fill_probability_basis="no_episodes",
        expected_fill_size=0.0,
        carry_cost_per_unit=0.0,
        unwind_cost_per_unit=0.0,
        failed_leg_expected_cost_per_unit=0.0,
        ev_per_opportunity_usd=0.0,
        opportunities_per_day=0.0,
        ev_per_day_usd=0.0,
    )


def _qualifying(
    episodes: Iterable[Episode], pair_key: str, direction: str, params: EVParams
) -> list[Episode]:
    """Executable-edge gates at episode granularity: no blips, no over-skew captures,
    and persistent-basis episodes are never opportunities."""
    return [
        ep
        for ep in episodes
        if ep.pair_key == pair_key
        and ep.direction == direction
        and not ep.is_basis_suspect
        and ep.snapshots >= params.executable.min_episode_snapshots
        and ep.max_abs_skew_ms <= params.executable.max_skew_ms
    ]


def _fill_probability(
    episodes: list[Episode], params: EVParams
) -> tuple[float, str | None, str]:
    best_tier: str | None = None
    best_through = -1
    for ep in episodes:
        if ep.survived_through_ms is not None and ep.survived_through_ms > best_through:
            best_through = ep.survived_through_ms
            best_tier = ep.best_survival_tier
    if best_tier is None:
        return params.unprobed_fill_probability, None, "unprobed"
    return (
        float(params.fill_probability_tiers.get(best_tier, 0.0)),
        best_tier,
        f"tier:{best_tier}",
    )


def _unwind_cost_per_unit(
    params: EVParams,
    *,
    unwind_spread: float | None,
    capital_locked_per_unit: float,
    time_to_resolution_days: float,
) -> float:
    if params.unwind_cost_model == UNWIND_HOLD_TO_RESOLUTION:
        # Worst case: the stuck leg's collateral (bounded by the full pair
        # collateral) stays locked to resolution with no hedge.
        return capital_locked_per_unit * params.capital_apr * time_to_resolution_days / 365.0
    spread = unwind_spread if unwind_spread is not None else params.unwind_fallback_spread
    return max(spread, 0.0)


def pair_ev(
    episodes: list[Episode],
    pair,
    *,
    params: EVParams,
    soak_days: float,
    direction: str,
    fees_per_unit: float | None = None,
    unwind_spread: float | None = None,
) -> EVBreakdown:
    """North-star EV for one (pair, direction) over one soak's episodes.

    ``pair`` is a :class:`~arbx.pairs.registry.PairSpec` (its taxonomy provides
    ``time_to_resolution_days``). ``fees_per_unit`` is the real round-trip taker
    fee per unit measured from FeeEngine-derived rows; when absent the flat
    ``depth_fee_base`` is used (conservative only for cheap markets — pass the
    real number whenever available). ``unwind_spread`` is the median displayed
    spread of the worse leg, for the ``cross_spread`` unwind model.
    """
    if soak_days <= 0:
        raise ValueError(f"soak_days must be > 0, got {soak_days}")
    pair_key = getattr(pair, "pair_key", str(pair))
    qualifying = _qualifying(episodes, pair_key, direction, params)
    if not qualifying:
        return _zero_breakdown(pair_key, direction, params, soak_days)

    # Gross depth edge per unit: median episode depth-adjusted edge, with the
    # flat fee it was derived at added back (real fees are separated downstream).
    gross_edge = median(ep.median_depth_edge for ep in qualifying) + params.depth_fee_base
    fees = fees_per_unit if fees_per_unit is not None else params.depth_fee_base

    taxonomy = getattr(pair, "taxonomy", None)
    ttr_days = float(getattr(taxonomy, "time_to_resolution_days", None) or 0.0)
    # Both legs post full collateral: locked capital = kalshi + poly price = 1 - gross.
    capital_locked = max(1.0 - gross_edge, 0.0)
    carry = capital_locked * params.capital_apr * ttr_days / 365.0

    fill_prob, tier, basis = _fill_probability(qualifying, params)
    fill_size = params.executable.depth_haircut * median(
        ep.max_fill_size for ep in qualifying
    )
    unwind = _unwind_cost_per_unit(
        params,
        unwind_spread=unwind_spread,
        capital_locked_per_unit=capital_locked,
        time_to_resolution_days=ttr_days,
    )
    failed_leg_cost = params.leg_failure_prob * unwind

    ev_per_unit = fill_prob * (gross_edge - fees - carry) - failed_leg_cost
    ev_per_opportunity = ev_per_unit * fill_size
    opportunities_per_day = len(qualifying) / soak_days

    return EVBreakdown(
        pair_key=pair_key,
        direction=direction,
        scenario=params.executable.scenario,
        validity=params.executable.validity,
        qualifying_episodes=len(qualifying),
        soak_days=soak_days,
        gross_edge_per_unit=gross_edge,
        fees_per_unit=fees,
        survival_probability=fill_prob,
        survival_tier=tier,
        fill_probability_basis=basis,
        expected_fill_size=fill_size,
        carry_cost_per_unit=carry,
        unwind_cost_per_unit=unwind,
        failed_leg_expected_cost_per_unit=failed_leg_cost,
        ev_per_opportunity_usd=ev_per_opportunity,
        opportunities_per_day=opportunities_per_day,
        ev_per_day_usd=ev_per_opportunity * opportunities_per_day,
    )


def verdict(breakdown: EVBreakdown, thresholds: ViabilityThresholds) -> str:
    """The go/no-go label under the pinned operator thresholds."""
    if (
        breakdown.ev_per_day_usd >= thresholds.min_ev_per_day_usd
        and breakdown.opportunities_per_week >= thresholds.min_opportunities_per_week
    ):
        return VERDICT_VIABLE
    if breakdown.ev_per_day_usd > 0:
        return VERDICT_MARGINAL
    return VERDICT_NOT_VIABLE
