#!/usr/bin/env python3
# Scope: BOT_RUNTIME — Incremental public-data edge derivation for verified pairs,
# with optional persistence-gated event-triggered survival probes.
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def _state_path(data_dir: Path) -> Path:
    return data_dir / "cache" / "edge_deriver_state.json"


def _read_state(data_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _last_seq(state: dict[str, Any]) -> float:
    value = state.get("last_source_capture_seq")
    return float(value) if isinstance(value, (int, float)) else -1


def select_probe_targets(
    fresh_edges: list[dict[str, Any]],
    probe_runs: dict[str, int],
    *,
    threshold: float,
    persistence_cutoff_cycles: int,
    max_per_cycle: int,
) -> tuple[list[str], dict[str, int]]:
    """Pick which pair_keys to survival-probe this pass, and update run counts.

    An edge is worth probing when it has just APPEARED and is still young — that is
    the transient shape of a real dislocation. A pair that has been continuously
    in-edge for more than ``persistence_cutoff_cycles`` passes is treated as
    basis/structural and dropped from probing (it would trivially "survive" and
    waste the network budget). Returns ``(pair_keys, updated_runs)``.
    """
    from arbx.analysis.episodes import qualifies

    best_edge: dict[str, float] = {}
    in_edge_keys: set[str] = set()
    seen_keys: set[str] = set()
    for row in fresh_edges:
        pair_key = row.get("pair_key", "")
        direction = row.get("direction", "")
        run_key = f"{pair_key}|{direction}"
        seen_keys.add(run_key)
        if qualifies(row, min_edge=threshold):
            in_edge_keys.add(run_key)
            fee = float(row.get("fee_adj_edge") or 0.0)
            if fee > best_edge.get(pair_key, -1.0):
                best_edge[pair_key] = fee

    runs = dict(probe_runs)
    eligible: list[tuple[str, float]] = []  # (pair_key, best_edge)
    for run_key in seen_keys:
        if run_key in in_edge_keys:
            runs[run_key] = runs.get(run_key, 0) + 1
            if runs[run_key] <= persistence_cutoff_cycles:
                pair_key = run_key.rsplit("|", 1)[0]
                eligible.append((pair_key, best_edge.get(pair_key, 0.0)))
        else:
            runs[run_key] = 0

    # dedupe pair_keys (keep best edge), rank by edge desc, cap per cycle
    by_pair: dict[str, float] = {}
    for pair_key, edge in eligible:
        by_pair[pair_key] = max(by_pair.get(pair_key, -1.0), edge)
    targets = [pk for pk, _ in sorted(by_pair.items(), key=lambda t: t[1], reverse=True)]
    return targets[: max(0, max_per_cycle)], runs


def _run_once(
    args: argparse.Namespace,
    *,
    probe_runner: Callable[..., list[dict[str, Any]]] | None = None,
    connectors: dict[str, Any] | None = None,
) -> int:
    from arbx.analysis.edges import derive_edges, load_edge_pairs, write_edges_jsonl

    data_dir = args.data_dir.resolve()
    pairs = [
        pair for pair in load_edge_pairs(args.pairs_yaml)
        if pair.include_in_strategy_metrics or args.all_pairs
    ]
    state = _read_state(data_dir)
    last_seq = _last_seq(state)
    edges = derive_edges(
        data_dir,
        pairs,
        fee_round_trip=args.fee_round_trip,
        match_tolerance_ns=int(args.match_tolerance_ms * 1_000_000),
        target_size=args.target_size,
    )
    fresh_edges = [
        row for row in edges
        if isinstance(row.get("source_capture_seq"), (int, float))
        and float(row["source_capture_seq"]) > last_seq
    ]
    if fresh_edges:
        write_edges_jsonl(data_dir, fresh_edges)
        state["last_source_capture_seq"] = max(float(row["source_capture_seq"]) for row in fresh_edges)

    probed = 0
    if args.probe and fresh_edges:
        probed = _run_probes(
            args, data_dir, pairs, fresh_edges, state,
            probe_runner=probe_runner, connectors=connectors,
        )

    if fresh_edges or args.probe:
        _write_state(data_dir, state)
    print(f"pairs={len(pairs)} derived={len(edges)} new={len(fresh_edges)} "
          f"probed={probed} last_seq={last_seq:g}")
    return len(fresh_edges)


def _run_probes(
    args: argparse.Namespace,
    data_dir: Path,
    pairs: list[Any],
    fresh_edges: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    probe_runner: Callable[..., list[dict[str, Any]]] | None,
    connectors: dict[str, Any] | None,
) -> int:
    from arbx.analysis.edges import write_edges_jsonl

    probe_runs = state.get("probe_runs") if isinstance(state.get("probe_runs"), dict) else {}
    delays = tuple(args.probe_delays_ms) if args.probe_delays_ms else None
    targets, runs = select_probe_targets(
        fresh_edges, probe_runs,
        threshold=args.probe_threshold,
        persistence_cutoff_cycles=args.probe_persistence_cutoff,
        max_per_cycle=args.probe_max_per_cycle,
    )
    state["probe_runs"] = runs
    if not targets:
        return 0

    if probe_runner is None:
        from arbx.analysis.survival import (
            DEFAULT_PROBE_DELAYS_MS,
            run_public_edge_survival_probe,
        )
        probe_runner = run_public_edge_survival_probe
        if delays is None:
            delays = DEFAULT_PROBE_DELAYS_MS
    if connectors is None:
        from arbx.data.recorder import build_live_public_connectors
        connectors = build_live_public_connectors()

    pair_by_key = {p.pair_key: p for p in pairs}
    probe_rows: list[dict[str, Any]] = []
    for pair_key in targets:
        pair = pair_by_key.get(pair_key)
        if pair is None:
            continue
        kwargs: dict[str, Any] = {
            "fee_round_trip": args.fee_round_trip,
            "target_size": args.target_size,
        }
        if delays is not None:
            kwargs["delays_ms"] = delays
        probe_rows.extend(probe_runner(pair, connectors, **kwargs))
    if probe_rows:
        write_edges_jsonl(data_dir, probe_rows)
    return len(targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally derive edge rows from recorder book rows"
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--pairs-yaml", type=Path, default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--fee-round-trip", type=float, default=0.02)
    parser.add_argument("--target-size", type=float, default=1.0)
    parser.add_argument("--match-tolerance-ms", type=float, default=5000.0)
    parser.add_argument("--all-pairs", action="store_true",
                        help="Include non-strategy pairs if present; default is strategy pairs only")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--every", type=float, default=30.0, help="Seconds between --watch passes")
    parser.add_argument("--max-cycles", type=int, default=None)
    # --- event-triggered survival probes (public refetch, no orders) ---
    parser.add_argument("--probe", action="store_true",
                        help="Fire survival probes on freshly-appeared, non-persistent edges")
    parser.add_argument("--probe-threshold", type=float, default=0.01,
                        help="Min fee+depth-adjusted edge (prob units) to trigger a probe (default 0.01)")
    parser.add_argument("--probe-max-per-cycle", type=int, default=3,
                        help="Cap probes fired per pass, ranked by edge (default 3)")
    parser.add_argument("--probe-persistence-cutoff", type=int, default=10,
                        help="Stop probing a pair after this many consecutive in-edge passes "
                             "(assumed basis/structural; default 10)")
    parser.add_argument("--probe-delays-ms", type=int, action="append", default=None,
                        help="Probe delay rung in ms; repeatable. Default: 50,100,250,500,1000")
    args = parser.parse_args(argv)

    cycles = 0
    while True:
        _run_once(args)
        cycles += 1
        if not args.watch or (args.max_cycles is not None and cycles >= args.max_cycles):
            return 0
        time.sleep(max(1.0, args.every))


if __name__ == "__main__":
    raise SystemExit(main())
