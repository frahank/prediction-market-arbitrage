# Executable-edge model.
from __future__ import annotations

from pathlib import Path

from arbx.modeling.executable import (
    DEFAULT_MODELING_YAML,
    ExecutableEdgeParams,
    executable_edge,
    expected_fill_size,
    load_scenarios,
)

PARAMS = ExecutableEdgeParams()


def _row(**overrides):
    row = {
        "depth_adj_edge": 0.03,
        "books_fresh": True,
        "capture_skew_ms": 10.0,
        "max_staleness_seconds": 2.0,
        "max_profitable_size": 100.0,
    }
    row.update(overrides)
    return row


def test_never_exceeds_depth_adj():
    for staleness in (0.0, 0.5, 2.0, 30.0):
        row = _row(max_staleness_seconds=staleness)
        edge = executable_edge(row, PARAMS, episode_snapshots=3)
        assert edge is not None
        assert edge <= row["depth_adj_edge"]
    # penalty arithmetic: 2s of age at 0.001/s = 0.002 off the depth-adj edge
    assert executable_edge(_row(), PARAMS, episode_snapshots=3) == 0.03 - 0.002


def test_none_on_stale_or_skewed():
    assert executable_edge(_row(books_fresh=False), PARAMS) is None
    assert executable_edge(_row(books_fresh=None), PARAMS) is None
    assert executable_edge(_row(capture_skew_ms=120.0), PARAMS) is None
    assert executable_edge(_row(capture_skew_ms=-120.0), PARAMS) is None
    assert executable_edge(_row(capture_skew_ms=None), PARAMS) is None
    assert executable_edge(_row(depth_adj_edge=None), PARAMS) is None
    # None, never 0: a rejected row must not read as break-even
    assert executable_edge(_row(books_fresh=False), PARAMS) != 0


def test_blip_excluded():
    assert executable_edge(_row(), PARAMS, episode_snapshots=1) is None
    assert executable_edge(_row(), PARAMS, episode_snapshots=2) is not None
    # inline row key works too
    assert executable_edge(_row(episode_snapshots=1), PARAMS) is None
    # unknown episode length is not treated as a blip
    assert executable_edge(_row(), PARAMS) is not None


def test_haircut_applied_to_thinner_leg():
    row = _row(kalshi_fillable_size=40.0, polymarket_fillable_size=100.0)
    assert expected_fill_size(row, PARAMS) == 0.5 * 40.0
    # without per-leg sizes, the paired-walk profitable size is the base
    assert expected_fill_size(_row(), PARAMS) == 0.5 * 100.0
    assert expected_fill_size(_row(max_profitable_size=None), PARAMS) == 0.0


def test_both_skew_scenarios_produced():
    scenarios = load_scenarios(Path(DEFAULT_MODELING_YAML))
    assert set(scenarios) == {"clean_concurrency", "hybrid_reality"}
    clean = scenarios["clean_concurrency"]
    hybrid = scenarios["hybrid_reality"]
    assert clean.max_skew_ms == 50.0
    assert hybrid.max_skew_ms > 1000.0  # seconds-scale REST-poll skew
    assert hybrid.validity == "kalshi_rest_poll_stopgap"
    # shared knobs come from the executable block
    assert clean.depth_haircut == hybrid.depth_haircut == 0.50
    assert clean.min_episode_snapshots == 2
