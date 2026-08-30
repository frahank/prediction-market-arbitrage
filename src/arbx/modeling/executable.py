# Scope: BOT_RUNTIME — Module A: displayed edge -> actually capturable edge (P5-T1).
"""Executable-edge model: what fraction of a displayed edge is capturable.

Starts from ``depth_adj_edge`` (already VWAP/depth-adjusted, so spread and
slippage are paid) and applies the Module A knobs from ``configs/modeling.yaml``:

* rows whose legs were captured further apart than ``max_skew_ms`` are
  untrustworthy -> ``None``;
* stale books are penalized at ``staleness_penalty_per_s`` per second of book
  age (never negative, so the invariant ``executable_edge <= depth_adj_edge``
  holds by construction);
* single-tick blips (episodes shorter than ``min_episode_snapshots``) -> ``None``;
* sizing haircuts displayed depth by ``depth_haircut`` on the thinner leg.

``None`` always means "not modelable from this row" — never 0, so a dead row
can't be mistaken for a break-even edge.

Skew scenarios (v1.1 amendment 4): until authenticated Kalshi WS lands
every fallback soak is a hybrid whose Kalshi leg is a REST poll with
seconds of skew. A flat 50ms gate would zero that leg, so the config defines
two labeled scenarios — ``clean_concurrency`` (50ms; only concurrent-REST
quality rows, small sample) and ``hybrid_reality`` (the measured hybrid skew,
tagged ``kalshi_rest_poll_stopgap``) — and every downstream report must show both.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MODELING_YAML = Path(__file__).resolve().parents[3] / "configs" / "modeling.yaml"


@dataclass(frozen=True)
class ExecutableEdgeParams:
    depth_haircut: float = 0.50
    max_skew_ms: float = 50.0
    staleness_penalty_per_s: float = 0.001
    min_episode_snapshots: int = 2
    scenario: str = "clean_concurrency"
    validity: str = "provisional_small_sample"


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _staleness_seconds(row: dict[str, Any]) -> float:
    """Book age in seconds; edge rows carry ``max_staleness_seconds`` (the worse
    leg), raw book rows carry ``staleness_seconds``. Missing -> 0 (freshness is
    separately gated by ``books_fresh``)."""
    for key in ("max_staleness_seconds", "staleness_seconds"):
        value = _as_number(row.get(key))
        if value is not None:
            return max(value, 0.0)
    return 0.0


def executable_edge(
    row: dict[str, Any],
    params: ExecutableEdgeParams,
    *,
    episode_snapshots: int | None = None,
) -> float | None:
    """Capturable edge per unit for one edge row, or ``None`` when the row is
    not trustworthy (stale/skewed/blip) — never 0 in that case.

    ``episode_snapshots`` is the length of the episode the row belongs to;
    rows may also carry it inline as ``row["episode_snapshots"]``.
    """
    depth_adj = _as_number(row.get("depth_adj_edge"))
    if depth_adj is None:
        return None
    if not row.get("books_fresh"):
        return None
    skew = _as_number(row.get("capture_skew_ms"))
    if skew is None or abs(skew) > params.max_skew_ms:
        return None
    if episode_snapshots is None:
        episode_snapshots = row.get("episode_snapshots")
    if episode_snapshots is not None and episode_snapshots < params.min_episode_snapshots:
        return None
    penalty = params.staleness_penalty_per_s * _staleness_seconds(row)
    return depth_adj - penalty


def expected_fill_size(row: dict[str, Any], params: ExecutableEdgeParams) -> float:
    """Haircut executable size: ``depth_haircut`` x the thinner leg's fillable
    depth. With only the paired walk available, ``max_profitable_size`` (already
    constrained by the thinner leg at every chunk) is the base."""
    kalshi = _as_number(row.get("kalshi_fillable_size"))
    poly = _as_number(row.get("polymarket_fillable_size"))
    if kalshi is not None and poly is not None:
        base = min(kalshi, poly)
    else:
        base = _as_number(row.get("max_profitable_size")) or 0.0
    return params.depth_haircut * max(base, 0.0)


def load_modeling_config(path: Path = DEFAULT_MODELING_YAML) -> dict[str, Any]:
    """Raw ``configs/modeling.yaml`` mapping (all Module A–G knobs)."""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"modeling config {path} is not a mapping")
    return data


def load_scenarios(path: Path = DEFAULT_MODELING_YAML) -> dict[str, ExecutableEdgeParams]:
    """Module A params per labeled skew scenario, from ``configs/modeling.yaml``.

    Shared knobs (haircut, staleness penalty, blip floor) come from
    ``executable:``; each entry under ``executable.scenarios:`` overrides
    ``max_skew_ms`` and carries its own ``validity`` tag.
    """
    config = load_modeling_config(path)
    executable = config.get("executable") or {}
    scenarios = executable.get("scenarios") or {}
    if not scenarios:
        raise ValueError(f"modeling config {path} defines no executable.scenarios")
    out: dict[str, ExecutableEdgeParams] = {}
    for name, spec in scenarios.items():
        spec = spec or {}
        out[str(name)] = ExecutableEdgeParams(
            depth_haircut=float(executable.get("depth_haircut", 0.50)),
            max_skew_ms=float(spec.get("max_skew_ms", 50.0)),
            staleness_penalty_per_s=float(executable.get("staleness_penalty_per_s", 0.001)),
            min_episode_snapshots=int(executable.get("min_episode_snapshots", 2)),
            scenario=str(name),
            validity=str(spec.get("validity", "")),
        )
    return out
