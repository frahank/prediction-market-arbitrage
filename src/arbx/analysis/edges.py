# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Derive edge_observations from paired book_observations (Phase 5).
"""
Derive the ``edge_observations`` table from the recorder's ``book_observations``.

A cross-venue binary arbitrage locks a guaranteed $1 payout by buying the YES
outcome on one venue and the NO outcome on the other. With per-market best
prices (``best_bid`` = YES bid, ``best_ask`` = YES ask, NO ask = ``1 - YES bid``):

    direction "kalshi_yes_poly_no":  raw_edge = poly.best_bid  - kalshi.best_ask
    direction "kalshi_no_poly_yes":  raw_edge = kalshi.best_bid - poly.best_ask

Both directions are emitted per paired capture. ``fee_adj_edge`` subtracts a
round-trip fee estimate; ``capture_skew_ms`` is the host-monotonic receive-time
difference between the two venue fetches.

The probe-ladder columns (``benchmark_ms``, ``survived``, ``survived_edge``) are
left null here: edge *survival by latency* requires delayed re-fetches (the
opportunity runner's latency ladder, or Phase 3 streaming), not plain snapshots.
The heatmap tooling reads those columns when a richer source populates them.

Pure stdlib. The edge is only *meaningful* for genuinely contract-equivalent
pairs (see docs/pair_approval_guide.md); for connectivity-only pairs it measures
noise between unrelated markets and is flagged via ``include_in_strategy_metrics``.

Real fees (P2-T4): pass ``fee_engine`` to :func:`derive_edges` and
``fee_adj_edge`` / ``max_profitable_size`` use per-venue real fees instead of
the flat ``fee_round_trip``; rows gain ``fee_model_version`` and
``fee_usd_at_target``. With ``fee_engine=None`` the output is byte-identical
to the flat path so old runs stay reproducible.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

if TYPE_CHECKING:  # pure-stdlib module: fees only imported for typing
    from arbx.fees.engine import FeeEngine

# (kalshi_exec_price, polymarket_exec_price, chunk_size) -> round-trip fee per unit
MarginalFeePerUnit = Callable[[float, float, float], float]


@dataclass(frozen=True)
class EdgePair:
    pair_key: str
    kalshi_market_id: str
    polymarket_market_id: str
    include_in_strategy_metrics: bool = False


@dataclass(frozen=True)
class DepthMetrics:
    target_size: float
    depth_fillable_size: float
    depth_liquidity_complete: bool
    vwap: float | None
    kalshi_vwap: float | None
    polymarket_vwap: float | None
    slippage: float | None
    depth_adj_edge: float | None
    max_profitable_size: float


def load_edge_pairs(pairs_yaml_path: Path) -> list[EdgePair]:
    """Load (pair_key, kalshi_id, polymarket_id) triples from the approved registry."""
    import yaml

    with Path(pairs_yaml_path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    from arbx.data.recorder import _polymarket_fetch_id

    pairs: list[EdgePair] = []
    for i, pair in enumerate(data.get("pairs", [])):
        kalshi_id = pair.get("kalshi_market_id") or pair.get("kalshi_identifiers", {}).get("market_ticker")
        # Match Polymarket book rows on the same id the recorder fetched with
        # (the YES CLOB token id), so derived edges join to recorded books.
        poly_id = _polymarket_fetch_id(pair)
        if not kalshi_id or not poly_id:
            continue
        pair_key = pair.get("pair_key") or pair.get("name") or f"{kalshi_id}__{poly_id}"
        pairs.append(EdgePair(
            pair_key=pair_key,
            kalshi_market_id=kalshi_id,
            polymarket_market_id=poly_id,
            include_in_strategy_metrics=bool(pair.get("include_in_strategy_metrics", False)),
        ))
    return pairs


def _iter_book_rows(data_dir: Path) -> Iterator[dict[str, Any]]:
    base = data_dir / "raw" / "book"
    if not base.exists():
        return
    for jsonl in sorted(base.rglob("*.jsonl")):
        try:
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def _edge_rows_for_capture(
    pair: EdgePair,
    k_row: dict[str, Any],
    p_row: dict[str, Any],
    *,
    fee_round_trip: float,
    target_size: float = 1.0,
    fee_engine: FeeEngine | None = None,
) -> list[dict[str, Any]]:
    k_bid, k_ask = k_row.get("best_bid"), k_row.get("best_ask")
    p_bid, p_ask = p_row.get("best_bid"), p_row.get("best_ask")
    k_ns, p_ns = k_row.get("recv_monotonic_ns"), p_row.get("recv_monotonic_ns")
    skew_ms = ((k_ns - p_ns) / 1e6) if (isinstance(k_ns, int) and isinstance(p_ns, int)) else None

    base = {
        "pair_key": pair.pair_key,
        "capture_ts_utc": k_row.get("capture_ts_utc"),
        "recv_monotonic_ns": k_ns,
        "kalshi_capture_seq": k_row.get("capture_seq"),
        "polymarket_capture_seq": p_row.get("capture_seq"),
        "source_capture_seq": _max_number(k_row.get("capture_seq"), p_row.get("capture_seq")),
        "capture_skew_ms": skew_ms,
        "kalshi_freshness_status": k_row.get("freshness_status"),
        "polymarket_freshness_status": p_row.get("freshness_status"),
        "max_staleness_seconds": _max_number(
            k_row.get("staleness_seconds"),
            p_row.get("staleness_seconds"),
        ),
        "books_fresh": _books_fresh(k_row, p_row),
        "benchmark_ms": None,
        "survived": None,
        "survived_edge": None,
        "survived_through_ms": None,
        "survival_tier": None,
        "include_in_strategy_metrics": pair.include_in_strategy_metrics,
        "run_id": k_row.get("run_id"),
    }

    marginal_fee = _marginal_fee_per_unit(fee_engine, pair) if fee_engine else None

    rows: list[dict[str, Any]] = []
    if p_bid is not None and k_ask is not None:
        # Buy kalshi YES at k_ask, buy poly NO at 1 - p_bid.
        raw = p_bid - k_ask
        depth = compute_depth_metrics(
            k_row,
            p_row,
            direction="kalshi_yes_poly_no",
            raw_edge=raw,
            fee_round_trip=fee_round_trip,
            target_size=target_size,
            marginal_fee_per_unit=marginal_fee,
        )
        row = {**base, "direction": "kalshi_yes_poly_no",
               "raw_edge": raw, "fee_adj_edge": raw - fee_round_trip,
               **_depth_record(depth)}
        if fee_engine is not None:
            row.update(_real_fee_fields(
                fee_engine, pair, raw,
                kalshi_price=k_ask, polymarket_price=1.0 - p_bid,
                target_size=target_size,
            ))
        rows.append(row)
    if k_bid is not None and p_ask is not None:
        # Buy kalshi NO at 1 - k_bid, buy poly YES at p_ask.
        raw = k_bid - p_ask
        depth = compute_depth_metrics(
            k_row,
            p_row,
            direction="kalshi_no_poly_yes",
            raw_edge=raw,
            fee_round_trip=fee_round_trip,
            target_size=target_size,
            marginal_fee_per_unit=marginal_fee,
        )
        row = {**base, "direction": "kalshi_no_poly_yes",
               "raw_edge": raw, "fee_adj_edge": raw - fee_round_trip,
               **_depth_record(depth)}
        if fee_engine is not None:
            row.update(_real_fee_fields(
                fee_engine, pair, raw,
                kalshi_price=1.0 - k_bid, polymarket_price=p_ask,
                target_size=target_size,
            ))
        rows.append(row)
    return rows


def edge_rows_for_capture(
    pair: EdgePair,
    k_row: dict[str, Any],
    p_row: dict[str, Any],
    *,
    fee_round_trip: float = 0.02,
    target_size: float = 1.0,
    fee_engine: FeeEngine | None = None,
) -> list[dict[str, Any]]:
    """Public seam over :func:`_edge_rows_for_capture` for streaming callers
    (Phase 3): derive both directions' edge rows from one paired capture."""
    return _edge_rows_for_capture(
        pair,
        k_row,
        p_row,
        fee_round_trip=fee_round_trip,
        target_size=target_size,
        fee_engine=fee_engine,
    )


def _marginal_fee_per_unit(fee_engine: FeeEngine, pair: EdgePair) -> MarginalFeePerUnit:
    """Real round-trip taker fee per unit at a depth chunk's execution prices.

    The Polymarket rate is keyed on the pair's recorded fetch token; YES and
    NO tokens of one market share a ``base_fee``, and the ``min(P, 1-P)`` fee
    base is complement-invariant, so the fetched token prices both directions.
    """
    def per_unit(kalshi_price: float, polymarket_price: float, qty: float) -> float:
        kalshi_fee = fee_engine.leg_fee(
            "kalshi",
            market_id=pair.kalshi_market_id,
            price=kalshi_price,
            size=qty,
            liquidity="taker",
        )
        poly_fee = fee_engine.leg_fee(
            "polymarket",
            market_id=pair.polymarket_market_id,
            price=polymarket_price,
            size=qty,
            liquidity="taker",
        )
        return kalshi_fee.per_unit_usd + poly_fee.per_unit_usd

    return per_unit


def _real_fee_fields(
    fee_engine: FeeEngine,
    pair: EdgePair,
    raw_edge: float,
    *,
    kalshi_price: float,
    polymarket_price: float,
    target_size: float,
) -> dict[str, Any]:
    """Real-fee columns: overrides ``fee_adj_edge``, adds the P2-T4 stamps."""
    per_unit = _marginal_fee_per_unit(fee_engine, pair)(
        kalshi_price, polymarket_price, target_size
    )
    return {
        "fee_adj_edge": raw_edge - per_unit,
        "fee_model_version": fee_engine.fee_model_version,
        "fee_usd_at_target": per_unit * target_size,
    }


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _max_number(*values: Any) -> float | None:
    numbers = [_as_float(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    return max(numbers) if numbers else None


def _books_fresh(k_row: dict[str, Any], p_row: dict[str, Any]) -> bool | None:
    statuses = [k_row.get("freshness_status"), p_row.get("freshness_status")]
    if any(status in {"stale", "missing_venue_timestamp", "missing_book"} for status in statuses):
        return False
    if all(status == "fresh" for status in statuses):
        return True
    return None


def _depth_record(metrics: DepthMetrics) -> dict[str, Any]:
    return {
        "target_size": metrics.target_size,
        "depth_fillable_size": metrics.depth_fillable_size,
        "depth_liquidity_complete": metrics.depth_liquidity_complete,
        "vwap": metrics.vwap,
        "kalshi_vwap": metrics.kalshi_vwap,
        "polymarket_vwap": metrics.polymarket_vwap,
        "slippage": metrics.slippage,
        "depth_adj_edge": metrics.depth_adj_edge,
        "max_profitable_size": metrics.max_profitable_size,
    }


def _levels(row: dict[str, Any], side: str) -> list[tuple[float, float]]:
    """Return visible executable levels as ``[(price, size), ...]``.

    ``yes_ask`` walks the stored YES ask ladder. ``no_ask`` walks the opposite
    side by converting YES bid levels into NO ask prices (``1 - yes_bid``).
    """
    levels: list[tuple[float, float]] = []
    if side == "yes_ask":
        px_prefix, sz_prefix = "ask_px_", "ask_sz_"
        transform = lambda price: price  # noqa: E731 — ported verbatim
    elif side == "no_ask":
        px_prefix, sz_prefix = "bid_px_", "bid_sz_"
        transform = lambda price: 1.0 - price  # noqa: E731 — ported verbatim
    else:
        raise ValueError(f"unknown side {side!r}")

    for index in range(1, 6):
        price = _as_float(row.get(f"{px_prefix}{index}"))
        size = _as_float(row.get(f"{sz_prefix}{index}"))
        if price is None or size is None or size <= 0:
            continue
        levels.append((transform(price), size))
    return levels


def _paired_chunks(
    leg_a: list[tuple[float, float]],
    leg_b: list[tuple[float, float]],
) -> Iterator[tuple[float, float, float]]:
    i = j = 0
    a_remaining = leg_a[0][1] if leg_a else 0.0
    b_remaining = leg_b[0][1] if leg_b else 0.0
    while i < len(leg_a) and j < len(leg_b):
        qty = min(a_remaining, b_remaining)
        if qty <= 0:
            break
        yield qty, leg_a[i][0], leg_b[j][0]
        a_remaining -= qty
        b_remaining -= qty
        if a_remaining <= 1e-12:
            i += 1
            a_remaining = leg_a[i][1] if i < len(leg_a) else 0.0
        if b_remaining <= 1e-12:
            j += 1
            b_remaining = leg_b[j][1] if j < len(leg_b) else 0.0


def compute_depth_metrics(
    k_row: dict[str, Any],
    p_row: dict[str, Any],
    *,
    direction: str,
    raw_edge: float,
    fee_round_trip: float,
    target_size: float = 1.0,
    marginal_fee_per_unit: MarginalFeePerUnit | None = None,
) -> DepthMetrics:
    """Estimate visible two-leg liquidity from public book levels.

    The result is a book-implied estimate only. It does not model queue
    priority, real order acceptance, partial fills, or account balances.
    When ``marginal_fee_per_unit`` is given, ``max_profitable_size`` walks
    depth with real marginal fees at each chunk's execution prices instead of
    the flat ``fee_round_trip``.
    """
    target = max(0.0, float(target_size))
    if direction == "kalshi_yes_poly_no":
        kalshi_levels = _levels(k_row, "yes_ask")
        poly_levels = _levels(p_row, "no_ask")
    elif direction == "kalshi_no_poly_yes":
        kalshi_levels = _levels(k_row, "no_ask")
        poly_levels = _levels(p_row, "yes_ask")
    else:
        return DepthMetrics(target, 0.0, False, None, None, None, None, None, 0.0)

    chunks = list(_paired_chunks(kalshi_levels, poly_levels))
    max_profitable_size = sum(
        qty for qty, k_price, p_price in chunks
        if 1.0 - (k_price + p_price) - (
            marginal_fee_per_unit(k_price, p_price, qty)
            if marginal_fee_per_unit is not None
            else fee_round_trip
        ) > 0
    )

    remaining = target
    filled = 0.0
    kalshi_cost = 0.0
    poly_cost = 0.0
    for qty, k_price, p_price in chunks:
        if remaining <= 1e-12:
            break
        take = min(qty, remaining)
        filled += take
        kalshi_cost += take * k_price
        poly_cost += take * p_price
        remaining -= take

    if filled <= 0:
        return DepthMetrics(target, 0.0, False, None, None, None, None, None, max_profitable_size)

    kalshi_vwap = kalshi_cost / filled
    poly_vwap = poly_cost / filled
    vwap = (kalshi_cost + poly_cost) / filled
    depth_raw_edge = 1.0 - vwap
    depth_adj_edge = depth_raw_edge - fee_round_trip
    slippage = raw_edge - depth_raw_edge
    return DepthMetrics(
        target_size=target,
        depth_fillable_size=filled,
        depth_liquidity_complete=filled + 1e-12 >= target,
        vwap=vwap,
        kalshi_vwap=kalshi_vwap,
        polymarket_vwap=poly_vwap,
        slippage=slippage,
        depth_adj_edge=depth_adj_edge,
        max_profitable_size=max_profitable_size,
    )


def derive_edges(
    data_dir: Path,
    pairs: list[EdgePair],
    *,
    fee_round_trip: float = 0.02,
    match_tolerance_ns: int = 5_000_000_000,
    target_size: float = 1.0,
    fee_engine: FeeEngine | None = None,
    row_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Derive edge_observations rows for ``pairs`` from book rows under ``data_dir``.

    For each pair, kalshi and polymarket captures are matched by nearest
    host-monotonic receive time within ``match_tolerance_ns`` (default 5s), so a
    cross-venue edge is computed only for near-simultaneous snapshots.

    With ``fee_engine`` set, ``fee_adj_edge`` and ``max_profitable_size`` use
    real per-venue fees and rows gain ``fee_model_version`` /
    ``fee_usd_at_target``; with ``None`` the flat path is byte-identical to
    the pre-P2-T4 output.

    ``row_transform`` is applied to every book row before matching — used to
    recover legacy rows with swapped bid/ask labels
    (:func:`arbx.data.legacy.unswap_legacy_book_row`).
    """
    # index book rows by (venue, market_id) -> sorted [(recv_ns, row)]
    by_market: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for row in _iter_book_rows(data_dir):
        if row_transform is not None:
            row = row_transform(row)
        ns = row.get("recv_monotonic_ns")
        key = (row.get("venue"), row.get("market_id"))
        if not isinstance(ns, int) or None in key:
            continue
        by_market.setdefault(key, []).append((ns, row))
    for series in by_market.values():
        series.sort(key=lambda t: t[0])

    edges: list[dict[str, Any]] = []
    for pair in pairs:
        k_series = by_market.get(("kalshi", pair.kalshi_market_id), [])
        p_series = by_market.get(("polymarket", pair.polymarket_market_id), [])
        if not k_series or not p_series:
            continue
        # two-pointer nearest match
        j = 0
        for k_ns, k_row in k_series:
            # advance j to the poly row nearest k_ns
            while j + 1 < len(p_series) and abs(p_series[j + 1][0] - k_ns) <= abs(p_series[j][0] - k_ns):
                j += 1
            p_ns, p_row = p_series[j]
            if abs(p_ns - k_ns) > match_tolerance_ns:
                continue
            edges.extend(_edge_rows_for_capture(
                pair,
                k_row,
                p_row,
                fee_round_trip=fee_round_trip,
                target_size=target_size,
                fee_engine=fee_engine,
            ))
    return edges


def write_edges_jsonl(data_dir: Path, edges: list[dict[str, Any]], *, date: str | None = None) -> Path:
    """Append derived edge rows to data/raw/edge/<date>.jsonl (Tier-1 landing)."""
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = data_dir / "raw" / "edge"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.jsonl"
    with out_path.open("a", encoding="utf-8") as fh:
        for row in edges:
            fh.write(json.dumps(row, default=str) + "\n")
    return out_path
