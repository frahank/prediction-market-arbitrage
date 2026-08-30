from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.derive_edges_continuous import _run_probes, select_probe_targets

from arbx.analysis.edges import EdgePair


def _edge(pair, direction="kalshi_no_poly_yes", fee=0.03):
    return {
        "pair_key": pair,
        "direction": direction,
        "books_fresh": True,
        "capture_skew_ms": 100.0,
        "fee_adj_edge": fee,
        "depth_adj_edge": fee,
        "max_profitable_size": 1000.0,
    }


def test_select_probe_targets_probes_fresh_edges_and_tracks_runs():
    fresh = [_edge("KXA|0x"), _edge("KXB|0x", fee=0.05)]
    targets, runs = select_probe_targets(
        fresh, {}, threshold=0.01, persistence_cutoff_cycles=10, max_per_cycle=3,
    )
    # both in-edge; higher-edge pair ranked first
    assert targets[0] == "KXB|0x"
    assert set(targets) == {"KXA|0x", "KXB|0x"}
    assert runs["KXA|0x|kalshi_no_poly_yes"] == 1
    assert runs["KXB|0x|kalshi_no_poly_yes"] == 1


def test_select_probe_targets_drops_persistent_basis_pairs():
    fresh = [_edge("KXA|0x")]
    # KXA already in-edge for 10 straight passes -> next pass exceeds cutoff
    prior = {"KXA|0x|kalshi_no_poly_yes": 10}
    targets, runs = select_probe_targets(
        fresh, prior, threshold=0.01, persistence_cutoff_cycles=10, max_per_cycle=3,
    )
    assert targets == []                                  # basis/structural, skip
    assert runs["KXA|0x|kalshi_no_poly_yes"] == 11        # run still tracked


def test_select_probe_targets_resets_run_when_edge_disappears():
    fresh = [_edge("KXA|0x", fee=0.0)]  # no longer in edge
    prior = {"KXA|0x|kalshi_no_poly_yes": 5}
    targets, runs = select_probe_targets(
        fresh, prior, threshold=0.01, persistence_cutoff_cycles=10, max_per_cycle=3,
    )
    assert targets == []
    assert runs["KXA|0x|kalshi_no_poly_yes"] == 0


def test_select_probe_targets_caps_per_cycle():
    fresh = [_edge(f"KX{i}|0x", fee=0.01 + i / 100) for i in range(6)]
    targets, _ = select_probe_targets(
        fresh, {}, threshold=0.01, persistence_cutoff_cycles=10, max_per_cycle=2,
    )
    assert len(targets) == 2


def test_run_probes_invokes_injected_runner_and_writes_rows(tmp_path: Path):
    calls: list[str] = []

    def fake_probe(pair, connectors, **kwargs):
        calls.append(pair.pair_key)
        return [{
            "pair_key": pair.pair_key, "direction": "kalshi_no_poly_yes",
            "public_probe": True, "benchmark_ms": 250, "survived": True,
            "survived_through_ms": 250, "survival_tier": "survived_250ms",
        }]

    pairs = [EdgePair("KXA|0x", "KXA", "0x", include_in_strategy_metrics=True)]
    fresh = [_edge("KXA|0x")]
    state: dict = {}
    args = argparse.Namespace(
        probe_threshold=0.01, probe_persistence_cutoff=10, probe_max_per_cycle=3,
        probe_delays_ms=[250], fee_round_trip=0.02, target_size=1.0,
    )
    n = _run_probes(
        args, tmp_path, pairs, fresh, state,
        probe_runner=fake_probe, connectors={"kalshi": object(), "polymarket": object()},
    )
    assert n == 1
    assert calls == ["KXA|0x"]
    assert state["probe_runs"]["KXA|0x|kalshi_no_poly_yes"] == 1
    written = list((tmp_path / "raw" / "edge").glob("*.jsonl"))
    assert written, "probe rows should be persisted"
    rows = [json.loads(line) for line in written[0].read_text().splitlines() if line.strip()]
    assert any(r.get("survival_tier") == "survived_250ms" for r in rows)
