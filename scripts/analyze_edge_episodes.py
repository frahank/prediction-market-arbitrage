#!/usr/bin/env python3
# Scope: BOT_RUNTIME — Post-hoc laser-focused opportunity analysis over edge rows.
#
# Reads derived edge rows, classifies persistent basis-suspect pairs (excluded),
# collapses transient dislocations into episodes, attaches probe survival tiers,
# and ranks the sharpest capturable-looking opportunities. Public-data only: an
# "opportunity" is a displayed-book estimate, never a fill or profit claim.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.core.paths import default_data_dir  # noqa: E402 — import after sys.path setup


def _edge_files(data_dir: Path) -> list[Path]:
    edge_dir = data_dir / "raw" / "edge"
    return sorted(edge_dir.glob("*.jsonl")) if edge_dir.is_dir() else []


def build_fee_engine(poly_bps: int | None = None):
    """Real FeeEngine from the pinned configs (public fee GETs only).

    ``poly_bps`` pins the Polymarket rate offline — resolved historical
    markets 404 on the live ``/fee-rate`` endpoint and would re-score at the
    worst-case fallback instead of the rate that applied during the soak.
    """
    from arbx.fees.engine import FeeEngine

    kalshi_yaml = ROOT / "configs" / "fees_kalshi.yaml"
    poly_yaml = ROOT / "configs" / "fees_polymarket.yaml"
    if poly_bps is None:
        return FeeEngine.from_configs(kalshi_yaml, poly_yaml)
    from arbx.fees.polymarket import StaticFeeRateProvider

    return FeeEngine.from_configs(
        kalshi_yaml, poly_yaml, provider=StaticFeeRateProvider(poly_bps)
    )


def rederive_with_real_fees(
    data_dir: Path, pairs_yaml: Path, poly_bps: int | None = None
) -> list[dict]:
    """Re-derive edge rows from raw book rows with real per-venue fees."""
    from arbx.analysis.edges import derive_edges, load_edge_pairs

    return derive_edges(
        data_dir,
        load_edge_pairs(pairs_yaml),
        fee_engine=build_fee_engine(poly_bps),
    )


def main(argv: list[str] | None = None) -> int:
    from arbx.analysis.episodes import (
        BUCKET_TRANSIENT,
        MIN_EDGE,
        PERSISTENCE_CUTOFF_FRAC,
        episode_to_dict,
        fee_sensitivity,
        iter_edge_rows,
        pair_persistence,
        rank_opportunities,
    )

    parser = argparse.ArgumentParser(
        description="Rank laser-focused transient edge opportunities from edge rows"
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--edge-file", type=Path, default=None,
                        help="Explicit edge JSONL; default globs <data-dir>/raw/edge/*.jsonl")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE,
                        help="Min fee- AND depth-adjusted edge in probability units (default 0.01 = 1c)")
    parser.add_argument("--cutoff-frac", type=float, default=PERSISTENCE_CUTOFF_FRAC,
                        help="A pair in-edge in >this fraction of cycles is basis-suspect (default 0.25)")
    parser.add_argument("--top", type=int, default=20, help="Show top N opportunities")
    parser.add_argument("--include-basis", action="store_true",
                        help="Do not exclude persistent basis-suspect pairs")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    parser.add_argument("--real-fees", action="store_true",
                        help="Re-derive edges from raw book rows with the real FeeEngine "
                             "instead of reading stored (flat-fee) edge files")
    parser.add_argument("--pairs", type=Path, default=ROOT / "configs" / "pairs.approved.yaml",
                        help="Pair registry for --real-fees re-derivation")
    parser.add_argument("--poly-bps", type=int, default=None,
                        help="With --real-fees: pin Polymarket base_fee bps offline instead of "
                             "querying /fee-rate (resolved markets 404 into the worst-case fallback)")
    parser.add_argument("--legacy-book-fix", action="store_true",
                        help="Recover legacy rows with swapped bid/ask labels "
                             "(docs/book_semantics_fix.md). Forces re-derivation from raw book "
                             "rows — stored edge files in legacy dirs were derived from swapped "
                             "books and cannot be trusted")
    args = parser.parse_args(argv)

    fee_model_version = None
    row_transform = None
    if args.legacy_book_fix:
        from arbx.data.legacy import unswap_legacy_book_row

        row_transform = unswap_legacy_book_row
    if args.real_fees or args.legacy_book_fix:
        engine = build_fee_engine(args.poly_bps) if args.real_fees else None
        fee_model_version = engine.fee_model_version if engine is not None else None
        from arbx.analysis.edges import derive_edges, load_edge_pairs

        rows = derive_edges(
            args.data_dir.resolve(), load_edge_pairs(args.pairs),
            fee_engine=engine, row_transform=row_transform,
        )
        files = []
        if not rows:
            print(f"No book rows derived under {args.data_dir}", file=sys.stderr)
            return 1
    else:
        files = [args.edge_file] if args.edge_file else _edge_files(args.data_dir.resolve())
        files = [f for f in files if f and f.exists()]
        if not files:
            print(f"No edge files found under {args.data_dir}", file=sys.stderr)
            return 1

        rows = [row for f in files for row in iter_edge_rows(f)]
    persistence = pair_persistence(rows, min_edge=args.min_edge, cutoff_frac=args.cutoff_frac)
    ranked = rank_opportunities(
        rows, min_edge=args.min_edge, cutoff_frac=args.cutoff_frac,
        include_basis=args.include_basis,
    )
    basis = sorted(
        (p for p in persistence.values() if p.is_basis_suspect),
        key=lambda p: p.in_edge_frac, reverse=True,
    )

    # The assumed-fee ladder shifts edges relative to the flat base fee; on
    # real-fee rows that arithmetic is meaningless, so it is skipped there.
    fees = None if args.real_fees else fee_sensitivity(
        rows, min_edge=args.min_edge, cutoff_frac=args.cutoff_frac
    )

    if args.json:
        print(json.dumps({
            "edge_files": [str(f) for f in files],
            "total_rows": len(rows),
            "min_edge": args.min_edge,
            "cutoff_frac": args.cutoff_frac,
            "fee_model_version": fee_model_version,
            "legacy_book_fix": bool(args.legacy_book_fix),
            "basis_suspect_pairs": [
                {"kalshi_market": p.kalshi_market, "in_edge_frac": round(p.in_edge_frac, 4),
                 "cycles": p.total_cycles} for p in basis
            ],
            "fee_sensitivity": fees,
            "opportunities": [episode_to_dict(e) for e in ranked[: args.top]],
            "opportunity_count": len(ranked),
        }, indent=2))
        return 0

    if args.real_fees or args.legacy_book_fix:
        fee_desc = f"REAL fees ({fee_model_version})" if args.real_fees else "flat fees"
        fix_desc = " + legacy book fix" if args.legacy_book_fix else ""
        print(f"Edge rows analyzed: {len(rows)} re-derived from raw books "
              f"with {fee_desc}{fix_desc}")
    else:
        print(f"Edge rows analyzed: {len(rows)} from {len(files)} file(s)")
    print(f"Thresholds: min_edge={args.min_edge*100:.1f}c, basis cutoff={args.cutoff_frac:.0%} of cycles\n")

    print(f"Basis-suspect pairs excluded ({len(basis)}) — persistent => not capturable arb:")
    for p in basis:
        print(f"  {p.kalshi_market:26s} in-edge {p.in_edge_frac:5.0%} of {p.total_cycles} cycles")
    if not basis:
        print("  (none)")

    if fees is None:
        print(f"\nFEES: real venue formulas ({fee_model_version}) — "
              "fee_adj_edge above is net of real per-leg taker fees")
    else:
        print("\nFEE SENSITIVITY (non-basis candidates surviving each assumed round-trip fee):")
        for f in fees:
            drop = f", dropped: {', '.join(f['pairs_dropped_vs_prev'])}" if f["pairs_dropped_vs_prev"] else ""
            print(f"  fee {f['fee']*100:4.1f}c : {f['candidate_rows']:6d} rows across {len(f['candidate_pairs'])} pairs{drop}")
        print("  (fees are heuristic — no profitability claim until real venue schedules are wired in)")

    candidates = [e for e in ranked if e.bucket == BUCKET_TRANSIENT]
    sustained = [e for e in candidates if e.snapshots >= 2]
    print(f"\nRESEARCH CANDIDATES (transient, basis excluded): {len(candidates)} "
          f"({len(sustained)} sustained)\n")
    header = (f"{'#':>2}  {'pair / direction':40s} {'start':8s} {'dur':>5s} {'snaps':>5s} "
              f"{'recur':>5s} {'dEdge c':>7s} {'survAdj':>7s} {'surv':>14s} {'score':>6s}")
    print(header)
    print("-" * len(header))
    for i, e in enumerate(candidates[: args.top], 1):
        tag = e.best_survival_tier or "unprobed"
        start = (e.start_ts or "")[11:19]
        dur = f"{e.duration_s/60:.0f}m" if e.duration_s >= 60 else f"{e.duration_s:.0f}s"
        label = f"{e.kalshi_market} / {e.direction}"[:40]
        sadj = f"{e.survival_adjusted_edge*100:.2f}" if e.survival_adjusted_edge is not None else "n/a"
        print(f"{i:>2}  {label:40s} {start:8s} {dur:>5s} {e.snapshots:>5d} {e.recurrence_count:>5d} "
              f"{e.median_depth_edge*100:>7.1f} {sadj:>7s} {tag:>14s} {e.score:>6.1f}")
    if not candidates:
        print("  (no transient research candidates cleared the thresholds)")
    print("\nLabels: research/candidate signals from public data only — not trades, not profit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
