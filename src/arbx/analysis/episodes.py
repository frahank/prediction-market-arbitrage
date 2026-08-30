# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: SHARED_PAPER_CORE — Edge episode + persistence analysis for laser-focused
# opportunity discovery over public recorder/edge data.
#
# Core interpretive rule:
# a cross-venue edge that PERSISTS for hours is almost certainly basis (the two
# contracts are not truly equivalent) or non-executable displayed liquidity, not
# a capturable arb. Real, tradeable dislocations APPEAR and DISAPPEAR. This module
# turns raw edge rows into de-duplicated episodes, classifies persistent
# basis-suspect pairs, and ranks the transient opportunities worth probing.
#
# Pure stdlib. Public-data only: an "opportunity" here is still a displayed-book
# estimate, never a fill, P&L, or profit claim.
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Iterator, Sequence

# --- default practical-edge thresholds (kept here so the deriver's live probe
# gating and this post-hoc analyzer apply the SAME definition of "in edge") ---
MIN_EDGE = 0.01            # >=1c fee- AND depth-adjusted; excludes float break-even noise
MAX_SKEW_MS = 250.0        # both legs captured within this; guards volatility/skew artifacts
MIN_FILL_SIZE = 1.0        # at least one contract fillable at the target size
EPISODE_GAP_SECONDS = 45.0 # consecutive 30s snapshots merge; a >45s gap starts a new episode
PERSISTENCE_CUTOFF_FRAC = 0.25  # in-edge in >25% of a pair's cycles => basis-suspect
BASE_FEE_ROUND_TRIP = 0.02      # the flat round-trip fee the recorder/deriver assumed
DEFAULT_FEE_LEVELS = (0.01, 0.015, 0.02, 0.03)  # fee-sensitivity sweep points

# Classification buckets (single source of truth for the CLI, UI, and reports).
BUCKET_BASIS = "basis_suspect"
BUCKET_TRANSIENT = "transient_candidate"
BUCKET_UNUSABLE = "unusable"

# Conservative, PUBLIC-DATA modeling estimate of P(edge still capturable) by
# survival tier. These are deliberately pessimistic placeholders, NOT measured
# fill probabilities — real numbers require authenticated execution data.
SURVIVAL_PROBABILITY = {
    "survived_1000ms": 0.80,
    "survived_500ms": 0.50,
    "survived_250ms": 0.25,
    "expired_lt_250ms": 0.0,
}
# Score weight for survival. Unknown (no probe yet) is treated as a neutral 0.5 so
# un-probed transient candidates still rank — they are the whole point pre-probe.
_UNKNOWN_SURVIVAL_SCORE = 0.5
_SIZE_TERM_CAP = 1000.0  # displayed depth above this is not trusted (likely inflated)


def iter_edge_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream edge rows from a JSONL file (skips blank/corrupt lines)."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def is_probe_row(row: dict[str, Any]) -> bool:
    """True for rows emitted by the survival probe runner (not baseline snapshots)."""
    return bool(row.get("public_probe")) or bool(row.get("benchmark_ms"))


def qualifies(
    row: dict[str, Any],
    *,
    min_edge: float = MIN_EDGE,
    max_skew_ms: float = MAX_SKEW_MS,
    min_fill_size: float = MIN_FILL_SIZE,
) -> bool:
    """The single practical-edge predicate: fees + spread/depth + skew all folded in.

    Fresh both books, low capture skew, fee- AND depth-adjusted edge clears the
    threshold (so spread/slippage is already paid), and at least ``min_fill_size``
    is fillable. Shared by the analyzer and the deriver's live probe gate.
    """
    if not row.get("books_fresh"):
        return False
    skew = row.get("capture_skew_ms")
    if not isinstance(skew, (int, float)) or abs(skew) >= max_skew_ms:
        return False
    fee = row.get("fee_adj_edge")
    depth = row.get("depth_adj_edge")
    if not isinstance(fee, (int, float)) or fee < min_edge:
        return False
    if not isinstance(depth, (int, float)) or depth < min_edge:
        return False
    if (row.get("max_profitable_size") or 0) < min_fill_size:
        return False
    return True


def kalshi_market(pair_key: str) -> str:
    """The Kalshi market id is the human-readable left side of a pair_key."""
    return pair_key.split("|", 1)[0]


@dataclass
class PairPersistence:
    kalshi_market: str
    total_cycles: int
    in_edge_cycles: int
    is_basis_suspect: bool

    @property
    def in_edge_frac(self) -> float:
        return self.in_edge_cycles / self.total_cycles if self.total_cycles else 0.0


def pair_persistence(
    rows: Iterable[dict[str, Any]],
    *,
    min_edge: float = MIN_EDGE,
    max_skew_ms: float = MAX_SKEW_MS,
    min_fill_size: float = MIN_FILL_SIZE,
    cutoff_frac: float = PERSISTENCE_CUTOFF_FRAC,
) -> dict[str, PairPersistence]:
    """Per Kalshi market: fraction of observed cycles spent in-edge, and whether
    that fraction marks it basis-suspect (persistent => exclude from arb count)."""
    total: dict[str, set] = {}
    in_edge: dict[str, set] = {}
    for row in rows:
        if is_probe_row(row):
            continue
        mkt = kalshi_market(row.get("pair_key", ""))
        ts = row.get("capture_ts_utc")
        total.setdefault(mkt, set()).add(ts)
        if qualifies(row, min_edge=min_edge, max_skew_ms=max_skew_ms, min_fill_size=min_fill_size):
            in_edge.setdefault(mkt, set()).add(ts)
    out: dict[str, PairPersistence] = {}
    for mkt, cycles in total.items():
        hits = len(in_edge.get(mkt, set()))
        n = len(cycles)
        out[mkt] = PairPersistence(
            kalshi_market=mkt,
            total_cycles=n,
            in_edge_cycles=hits,
            is_basis_suspect=(n > 0 and hits / n > cutoff_frac),
        )
    return out


@dataclass
class Episode:
    pair_key: str
    kalshi_market: str
    direction: str
    start_ts: str
    end_ts: str
    duration_s: float
    snapshots: int
    peak_edge: float
    median_edge: float
    median_depth_edge: float
    max_fill_size: float
    max_abs_skew_ms: float
    is_basis_suspect: bool = False
    best_survival_tier: str | None = None
    survived_through_ms: int | None = None
    recurrence_count: int = 1        # episodes for this (pair, direction) in the soak
    bucket: str = BUCKET_TRANSIENT
    reason: str = ""
    survival_probability: float | None = None
    survival_adjusted_edge: float | None = None
    score: float = 0.0


def _parse(ts: Any) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def build_episodes(
    rows: Iterable[dict[str, Any]],
    *,
    min_edge: float = MIN_EDGE,
    max_skew_ms: float = MAX_SKEW_MS,
    min_fill_size: float = MIN_FILL_SIZE,
    gap_seconds: float = EPISODE_GAP_SECONDS,
) -> list[Episode]:
    """Collapse consecutive qualifying snapshots into per-(pair, direction) episodes.

    One episode = one dislocation observed across >=1 contiguous 30s snapshots. A
    gap longer than ``gap_seconds`` starts a new episode. Probe rows are ignored
    here (survival is attached separately via :func:`annotate_survival`).
    """
    grouped: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}
    for row in rows:
        if is_probe_row(row):
            continue
        if not qualifies(row, min_edge=min_edge, max_skew_ms=max_skew_ms, min_fill_size=min_fill_size):
            continue
        ts = _parse(row.get("capture_ts_utc"))
        if ts is None:
            continue
        grouped.setdefault((row.get("pair_key", ""), row.get("direction", "")), []).append((ts, row))

    episodes: list[Episode] = []
    for (pair_key, direction), items in grouped.items():
        items.sort(key=lambda t: t[0])
        run: list[tuple[float, dict[str, Any]]] = [items[0]]
        for ts, row in items[1:]:
            if ts - run[-1][0] <= gap_seconds:
                run.append((ts, row))
            else:
                episodes.append(_episode_from_run(pair_key, direction, run))
                run = [(ts, row)]
        episodes.append(_episode_from_run(pair_key, direction, run))
    return episodes


def _episode_from_run(pair_key: str, direction: str, run: list[tuple[float, dict[str, Any]]]) -> Episode:
    edges = [float(r.get("fee_adj_edge") or 0.0) for _, r in run]
    depth_edges = [float(r.get("depth_adj_edge") or 0.0) for _, r in run]
    sizes = [float(r.get("max_profitable_size") or 0.0) for _, r in run]
    skews = [abs(float(r.get("capture_skew_ms") or 0.0)) for _, r in run]
    return Episode(
        pair_key=pair_key,
        kalshi_market=kalshi_market(pair_key),
        direction=direction,
        start_ts=run[0][1].get("capture_ts_utc"),
        end_ts=run[-1][1].get("capture_ts_utc"),
        # a single snapshot proves >=30s of presence only implicitly; report the
        # observed span (0s for a lone snapshot) so blips are visible as such.
        duration_s=run[-1][0] - run[0][0],
        snapshots=len(run),
        peak_edge=max(edges),
        median_edge=median(edges),
        median_depth_edge=median(depth_edges),
        max_fill_size=max(sizes),
        max_abs_skew_ms=max(skews) if skews else 0.0,
    )


def annotate_survival(episodes: list[Episode], rows: Iterable[dict[str, Any]]) -> None:
    """Attach the best observed survival tier per ``(pair_key, direction)``.

    Keying on direction matters: a probe on ``kalshi_no_poly_yes`` must not
    annotate the opposite ``kalshi_yes_poly_no`` episode of the same market.
    """
    best: dict[tuple[str, str], int] = {}
    tier: dict[tuple[str, str], str | None] = {}
    for row in rows:
        if not is_probe_row(row):
            continue
        through = row.get("survived_through_ms")
        if not isinstance(through, (int, float)):
            continue
        key = (row.get("pair_key", ""), row.get("direction", ""))
        if through > best.get(key, -1):
            best[key] = int(through)
            tier[key] = row.get("survival_tier")
    for ep in episodes:
        key = (ep.pair_key, ep.direction)
        if key in best:
            ep.survived_through_ms = best[key]
            ep.best_survival_tier = tier[key]


def survival_probability(tier: str | None) -> float | None:
    """Conservative public-data estimate of P(capturable) by tier; None if un-probed."""
    if tier is None:
        return None
    return SURVIVAL_PROBABILITY.get(tier, 0.0)


def _survival_score(tier: str | None) -> float:
    if tier is None:
        return _UNKNOWN_SURVIVAL_SCORE
    return SURVIVAL_PROBABILITY.get(tier, 0.0)


def classify_episode(ep: Episode) -> tuple[str, str]:
    """Bucket + one human-readable reason for a single episode. Single source of
    truth shared by the CLI, the report exporter, and the Data UI."""
    if ep.is_basis_suspect:
        return BUCKET_BASIS, "persistent basis — in-edge in too many cycles; likely not true arb"
    if ep.snapshots < 2:
        return BUCKET_UNUSABLE, "single-snapshot blip — likely a capture-skew/volatility artifact"
    if ep.best_survival_tier == "expired_lt_250ms":
        return BUCKET_UNUSABLE, "died before 250ms — edge did not survive order-routing latency"
    if ep.best_survival_tier is None:
        return BUCKET_TRANSIENT, (
            f"appears/disappears over {ep.snapshots} snapshots; survival not yet probed"
        )
    return BUCKET_TRANSIENT, (
        f"appears/disappears over {ep.snapshots} snapshots; survived to "
        f"{ep.survived_through_ms}ms"
    )


def _pair_rejection_reason(
    pair_rows: list[dict[str, Any]], *, min_edge: float, max_skew_ms: float, min_fill_size: float,
) -> str:
    """Primary reason a pair produced no qualifying candidate (row-level)."""
    fresh = [r for r in pair_rows if r.get("books_fresh")]
    if not fresh:
        return "stale book — no fresh paired snapshot"
    low_skew = [r for r in fresh if isinstance(r.get("capture_skew_ms"), (int, float))
                and abs(r["capture_skew_ms"]) < max_skew_ms]
    if not low_skew:
        return "high skew — legs never captured close enough in time"
    fees = [float(r["fee_adj_edge"]) for r in low_skew if isinstance(r.get("fee_adj_edge"), (int, float))]
    if not fees or max(fees) < min_edge:
        if fees and max(fees) < 0:
            return "negative after fees — gross gap does not cover the assumed fee"
        return "edge below fee threshold — clears fees but under the min edge"
    depths = [float(r["depth_adj_edge"]) for r in low_skew if isinstance(r.get("depth_adj_edge"), (int, float))]
    if not depths or max(depths) < min_edge:
        return "negative after depth — spread/slippage erases the fee-positive edge"
    sizes = [float(r.get("max_profitable_size") or 0.0) for r in low_skew]
    if not sizes or max(sizes) < min_fill_size:
        return "no depth — nothing fillable at the target size"
    return "below thresholds — no snapshot cleared every filter simultaneously"


def classify_pairs(
    rows: Iterable[dict[str, Any]] | list[dict[str, Any]],
    *,
    min_edge: float = MIN_EDGE,
    max_skew_ms: float = MAX_SKEW_MS,
    min_fill_size: float = MIN_FILL_SIZE,
    gap_seconds: float = EPISODE_GAP_SECONDS,
    cutoff_frac: float = PERSISTENCE_CUTOFF_FRAC,
) -> list[dict[str, Any]]:
    """Label EVERY pair as basis_suspect / transient_candidate / unusable, with a
    human-readable reason — so nobody has to read raw rows to know why a pair is
    (un)interesting. Sorted best research candidates first."""
    rows = list(rows)
    persistence = pair_persistence(
        rows, min_edge=min_edge, max_skew_ms=max_skew_ms,
        min_fill_size=min_fill_size, cutoff_frac=cutoff_frac,
    )
    episodes = rank_opportunities(
        rows, min_edge=min_edge, max_skew_ms=max_skew_ms, min_fill_size=min_fill_size,
        gap_seconds=gap_seconds, cutoff_frac=cutoff_frac, include_basis=True,
    )
    by_market: dict[str, list[Episode]] = {}
    for ep in episodes:
        by_market.setdefault(ep.kalshi_market, []).append(ep)
    rows_by_market: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not is_probe_row(r):
            rows_by_market.setdefault(kalshi_market(r.get("pair_key", "")), []).append(r)

    out: list[dict[str, Any]] = []
    for market, prof in persistence.items():
        eps = by_market.get(market, [])
        transient_eps = [e for e in eps if e.bucket == BUCKET_TRANSIENT]
        # a pair is a candidate if it has ANY transient episode, even if its
        # single highest-scoring episode happens to be a blip.
        best = (max(transient_eps, key=lambda e: e.score) if transient_eps
                else (max(eps, key=lambda e: e.score) if eps else None))
        if prof.is_basis_suspect:
            bucket = BUCKET_BASIS
            reason = f"persistent basis — in-edge in {prof.in_edge_frac:.0%} of {prof.total_cycles} cycles"
        elif transient_eps:
            bucket, reason = BUCKET_TRANSIENT, best.reason
        else:
            bucket = BUCKET_UNUSABLE
            reason = _pair_rejection_reason(
                rows_by_market.get(market, []),
                min_edge=min_edge, max_skew_ms=max_skew_ms, min_fill_size=min_fill_size,
            )
        out.append({
            "kalshi_market": market,
            "bucket": bucket,
            "reason": reason,
            "in_edge_frac": round(prof.in_edge_frac, 4),
            "total_cycles": prof.total_cycles,
            "episode_count": len(eps),
            "best_survival_tier": best.best_survival_tier if best else None,
            "score": round(best.score, 2) if best else 0.0,
        })
    order = {BUCKET_TRANSIENT: 0, BUCKET_BASIS: 1, BUCKET_UNUSABLE: 2}
    out.sort(key=lambda d: (order.get(d["bucket"], 9), -d["score"]))
    return out


def rank_opportunities(
    rows: Iterable[dict[str, Any]] | list[dict[str, Any]],
    *,
    min_edge: float = MIN_EDGE,
    max_skew_ms: float = MAX_SKEW_MS,
    min_fill_size: float = MIN_FILL_SIZE,
    gap_seconds: float = EPISODE_GAP_SECONDS,
    cutoff_frac: float = PERSISTENCE_CUTOFF_FRAC,
    include_basis: bool = False,
) -> list[Episode]:
    """End-to-end: episodes + persistence + survival + classification + research score.

    Basis-suspect pairs are flagged and (unless ``include_basis``) excluded, since
    persistent edges are not capturable arbs. Remaining transient episodes are
    scored (a RESEARCH/candidate score, never a profit claim) so the sharpest
    research candidates surface first.
    """
    rows = list(rows)
    persistence = pair_persistence(
        rows, min_edge=min_edge, max_skew_ms=max_skew_ms,
        min_fill_size=min_fill_size, cutoff_frac=cutoff_frac,
    )
    episodes = build_episodes(
        rows, min_edge=min_edge, max_skew_ms=max_skew_ms,
        min_fill_size=min_fill_size, gap_seconds=gap_seconds,
    )
    annotate_survival(episodes, rows)
    recurrence: dict[tuple[str, str], int] = {}
    for ep in episodes:
        recurrence[(ep.pair_key, ep.direction)] = recurrence.get((ep.pair_key, ep.direction), 0) + 1
    for ep in episodes:
        ep.is_basis_suspect = persistence.get(
            ep.kalshi_market, PairPersistence(ep.kalshi_market, 0, 0, False)
        ).is_basis_suspect
        ep.recurrence_count = recurrence[(ep.pair_key, ep.direction)]
        ep.survival_probability = survival_probability(ep.best_survival_tier)
        if ep.survival_probability is not None:
            ep.survival_adjusted_edge = ep.median_depth_edge * ep.survival_probability
        ep.bucket, ep.reason = classify_episode(ep)
        ep.score = _profit_score(ep)
    if not include_basis:
        episodes = [ep for ep in episodes if not ep.is_basis_suspect]
    # transient candidates first, then unusable/basis; highest score within each.
    _bucket_rank = {BUCKET_TRANSIENT: 0, BUCKET_UNUSABLE: 1, BUCKET_BASIS: 2}
    episodes.sort(key=lambda e: (_bucket_rank.get(e.bucket, 9), -e.score))
    return episodes


def _profit_score(ep: Episode) -> float:
    """RESEARCH/candidate score — NOT guaranteed profit, NOT executable P&L.

        median_depth_edge(c) × survival_score × recurrence_score × log10(size, capped)
        − basis_penalty − skew_penalty − staleness_penalty

    The size term is capped at ~1000 contracts because displayed depth above that
    is not trusted on these thin markets (see the profitability playbook)."""
    edge_term = max(ep.median_depth_edge, 0.0) * 100.0            # cents
    survival = _survival_score(ep.best_survival_tier)
    recurrence = 1.0 + math.log1p(max(ep.recurrence_count - 1, 0))
    size_term = math.log10(min(max(ep.max_fill_size, 1.0), _SIZE_TERM_CAP) + 1.0)
    blip_penalty = 0.0 if ep.snapshots >= 2 else edge_term * 0.5  # single-snapshot blips
    basis_penalty = 5.0 if ep.is_basis_suspect else 0.0
    skew_penalty = (ep.max_abs_skew_ms / MAX_SKEW_MS) * 0.5 if MAX_SKEW_MS else 0.0
    base = edge_term * survival * recurrence * size_term
    return base - basis_penalty - skew_penalty - blip_penalty


def fee_sensitivity(
    rows: Iterable[dict[str, Any]] | list[dict[str, Any]],
    *,
    fee_levels: Sequence[float] = DEFAULT_FEE_LEVELS,
    base_fee: float = BASE_FEE_ROUND_TRIP,
    max_skew_ms: float = MAX_SKEW_MS,
    min_edge: float = MIN_EDGE,
    min_fill_size: float = MIN_FILL_SIZE,
    cutoff_frac: float = PERSISTENCE_CUTOFF_FRAC,
) -> list[dict[str, Any]]:
    """How many non-basis candidates survive each assumed round-trip fee.

    Edges were derived at ``base_fee`` (flat), so edge at fee f = stored + (base − f).
    Reports, per fee level, the qualifying-row count and the set of pairs that
    still show a candidate — so you can see which pairs vanish as fees rise.
    """
    rows = list(rows)
    persistence = pair_persistence(
        rows, min_edge=min_edge, max_skew_ms=max_skew_ms,
        min_fill_size=min_fill_size, cutoff_frac=cutoff_frac,
    )
    basis = {m for m, p in persistence.items() if p.is_basis_suspect}
    out: list[dict[str, Any]] = []
    prev_pairs: set[str] | None = None
    for fee in fee_levels:
        shift = base_fee - fee
        pairs: set[str] = set()
        n = 0
        for r in rows:
            if is_probe_row(r) or kalshi_market(r.get("pair_key", "")) in basis:
                continue
            sk = r.get("capture_skew_ms")
            fee_e = r.get("fee_adj_edge")
            depth_e = r.get("depth_adj_edge")
            if not r.get("books_fresh") or not isinstance(sk, (int, float)) or abs(sk) >= max_skew_ms:
                continue
            if not isinstance(fee_e, (int, float)) or not isinstance(depth_e, (int, float)):
                continue
            if (fee_e + shift) < min_edge or (depth_e + shift) < min_edge:
                continue
            if (r.get("max_profitable_size") or 0) < min_fill_size:
                continue
            n += 1
            pairs.add(kalshi_market(r.get("pair_key", "")))
        dropped = sorted(prev_pairs - pairs) if prev_pairs is not None else []
        out.append({
            "fee": fee, "candidate_rows": n, "candidate_pairs": sorted(pairs),
            "pairs_dropped_vs_prev": dropped,
        })
        prev_pairs = pairs
    return out


def episode_to_dict(ep: Episode) -> dict[str, Any]:
    return {
        "pair_key": ep.pair_key,
        "kalshi_market": ep.kalshi_market,
        "direction": ep.direction,
        "start_ts": ep.start_ts,
        "end_ts": ep.end_ts,
        "duration_s": round(ep.duration_s, 1),
        "snapshots": ep.snapshots,
        "recurrence_count": ep.recurrence_count,
        "peak_edge": round(ep.peak_edge, 4),
        "median_edge": round(ep.median_edge, 4),
        "median_depth_edge": round(ep.median_depth_edge, 4),
        "max_fill_size": round(ep.max_fill_size, 2),
        "max_abs_skew_ms": round(ep.max_abs_skew_ms, 1),
        "bucket": ep.bucket,
        "reason": ep.reason,
        "is_basis_suspect": ep.is_basis_suspect,
        "best_survival_tier": ep.best_survival_tier,
        "survived_through_ms": ep.survived_through_ms,
        "survival_probability": ep.survival_probability,
        "survival_adjusted_edge": (
            round(ep.survival_adjusted_edge, 4) if ep.survival_adjusted_edge is not None else None
        ),
        "score": round(ep.score, 2),
    }
