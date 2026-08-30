#!/usr/bin/env python3
# Scope: BOT_RUNTIME — Derive edges and render modeling heatmaps (Phase 5).
"""
Derive edge_observations from the recorder's book data and render heatmaps.

Usage:
    # derive edges for all approved pairs, render heatmaps to data/heatmaps.html
    python scripts/build_heatmaps.py

    python scripts/build_heatmaps.py --strategy-only        # only contract-equivalent pairs
    python scripts/build_heatmaps.py --write-edges          # also append data/raw/edge/<date>.jsonl
    python scripts/build_heatmaps.py --html data/heatmaps.html

Notes:
  - The edge is only *meaningful* on genuinely contract-equivalent pairs. With
    --strategy-only the edge heatmap is restricted to pairs whose registry flag
    include_in_strategy_metrics is true; until curation approves such pairs
    (docs/pair_approval_guide.md), that view is empty by design.
  - The survival heatmap needs probe-ladder data (benchmark_ms/survived) and
    stays empty on plain snapshots — that is reported, not faked.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive edges and render heatmaps")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--pairs-yaml", type=Path, default=ROOT / "configs" / "pairs.approved.yaml")
    parser.add_argument("--fee-round-trip", type=float, default=0.02,
                        help="Round-trip fee estimate subtracted from raw edge")
    parser.add_argument("--target-size", type=float, default=1.0,
                        help="Visible two-leg depth target in outcome units/contracts")
    parser.add_argument("--strategy-only", action="store_true",
                        help="Restrict edge heatmap to include_in_strategy_metrics pairs")
    parser.add_argument("--write-edges", action="store_true",
                        help="Append derived edges to data/raw/edge/<date>.jsonl")
    parser.add_argument("--html", type=Path, default=None, help="Write an HTML page of heatmaps")
    args = parser.parse_args(argv)

    from arbx.analysis.edges import derive_edges, load_edge_pairs, write_edges_jsonl
    from arbx.analysis.heatmap import (
        edge_heatmap,
        latency_heatmap,
        render_html_page,
        survival_heatmap,
    )

    data_dir = args.data_dir.resolve()

    # book rows for the latency heatmap
    import json
    book_rows = []
    book_base = data_dir / "raw" / "book"
    if book_base.exists():
        for f in sorted(book_base.rglob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        book_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    pairs = load_edge_pairs(args.pairs_yaml)
    edges = derive_edges(
        data_dir,
        pairs,
        fee_round_trip=args.fee_round_trip,
        target_size=args.target_size,
    )
    print(f"Loaded {len(book_rows)} book rows, {len(pairs)} pairs, derived {len(edges)} edge rows")

    if args.write_edges and edges:
        out = write_edges_jsonl(data_dir, edges)
        print(f"Wrote edges to {out}")

    hm_latency = latency_heatmap(book_rows)
    hm_edge = edge_heatmap(edges, strategy_only=args.strategy_only)
    hm_survival = survival_heatmap(edges)

    for hm in (hm_latency, hm_edge, hm_survival):
        print()
        print(hm.render_text())

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(
            render_html_page([hm_latency, hm_edge, hm_survival]), encoding="utf-8"
        )
        print(f"\nWrote {args.html}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
