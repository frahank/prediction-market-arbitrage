# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Public-data edge survival probes for strategy pairs.
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from arbx.analysis.edges import EdgePair, _edge_rows_for_capture
from arbx.data.connector import PublicMarketDataConnector, capture_connector_snapshot
from arbx.data.recorder import _book_to_observation

DEFAULT_PROBE_DELAYS_MS = (50, 100, 250, 500, 1000)

# Phase 3 streaming rungs: with single-digit-ms paired skew the 25/50/100ms
# tiers become measurable, so the ladder extends below the old 110ms floor.
# Defaults stay unchanged for reproducibility of pre-streaming probes.
STREAMING_PROBE_DELAYS_MS = (25, 50, 100, 250, 500, 1000)

# Progressive edge-lifetime tiers, in milliseconds of *contiguous* survival from
# the baseline observation. These are the buckets a future UI should color so an
# operator can see, at a glance, how long a displayed edge actually persisted
# under public refetch. The color mapping is co-located here so the data layer
# and the UI never disagree on thresholds.
#
#   survived_250ms  -> orange  (lived past 250 ms but not 500 ms)
#   survived_500ms  -> yellow  (lived past 500 ms but not 1000 ms)
#   survived_1000ms -> green   (lived past 1000 ms)
#   expired_lt_250ms-> gray    (an edge existed at observation but died < 250 ms)
#   None            -> no edge at the baseline observation (nothing to survive)
#
# NOTE: with sequential public fetches the capture skew (~150 ms median) is the
# same order as the 50/100 ms rungs, so only the 250 ms+ tiers are trustworthy
# without concurrent per-rung fetches or streaming (Phase 3).
SURVIVAL_TIER_THRESHOLDS_MS = (25, 100, 250, 500, 1000)
SURVIVAL_TIER_COLORS = {
    "survived_1000ms": "green",
    "survived_500ms": "yellow",
    "survived_250ms": "orange",
    "survived_100ms": "blue",   # sub-250ms tiers: only measurable with
    "survived_25ms": "purple",  # concurrent/streaming capture (Phase 3)
    "expired_lt_250ms": "gray",
}


def _survived_through_ms(direction_rows: list[dict[str, Any]]) -> int | None:
    """Largest benchmark_ms with *contiguous* survival from the baseline.

    Returns ``None`` when the baseline observation had no surviving edge to
    begin with. Returns ``0`` when an edge existed at observation but failed the
    first delayed rung.
    """
    ordered = sorted(direction_rows, key=lambda r: r.get("benchmark_ms") or 0)
    through: int | None = None
    for row in ordered:
        ms = row.get("benchmark_ms")
        if not isinstance(ms, (int, float)):
            continue
        if row.get("survived"):
            through = int(ms)
        else:
            break
    return through


def _survival_tier_for(through_ms: int | None) -> str | None:
    if through_ms is None:
        return None
    if through_ms >= 1000:
        return "survived_1000ms"
    if through_ms >= 500:
        return "survived_500ms"
    if through_ms >= 250:
        return "survived_250ms"
    if through_ms >= 100:
        return "survived_100ms"
    if through_ms >= 25:
        return "survived_25ms"
    return "expired_lt_250ms"


def classify_survival_tiers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``survived_through_ms`` and ``survival_tier`` on every probe row.

    The tier is computed once per ``direction`` group (all benchmark rungs of one
    edge observation share it) so the UI can color any row of the group.
    """
    by_direction: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_direction.setdefault(row.get("direction"), []).append(row)
    for direction_rows in by_direction.values():
        through = _survived_through_ms(direction_rows)
        tier = _survival_tier_for(through)
        for row in direction_rows:
            row["survived_through_ms"] = through
            row["survival_tier"] = tier
    return rows


def run_public_edge_survival_probe(
    pair: EdgePair,
    connectors: Mapping[str, PublicMarketDataConnector],
    *,
    fee_round_trip: float = 0.02,
    target_size: float = 1.0,
    delays_ms: tuple[int, ...] = DEFAULT_PROBE_DELAYS_MS,
    run_id: str | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Refetch public books after latency delays and label edge survival.

    This is still public-data only. It measures whether the visible book-implied
    edge survived after a delay; it does not submit orders, reserve liquidity,
    infer queue priority, or prove a fill.
    """
    probe_run_id = run_id or f"probe_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    market_ids = {
        "kalshi": pair.kalshi_market_id,
        "polymarket": pair.polymarket_market_id,
    }

    rows: list[dict[str, Any]] = []
    baseline = _snapshot_edge_rows(
        pair,
        connectors,
        market_ids,
        fee_round_trip=fee_round_trip,
        target_size=target_size,
        run_id=probe_run_id,
        capture_seq=0,
    )
    for row in baseline:
        row["benchmark_ms"] = 0
        row["probe_source"] = "public_refetch"
        edge = _survival_edge(row)
        row["survived"] = edge is not None and edge > 0
        row["survived_edge"] = edge
    rows.extend(baseline)

    for delay_ms in delays_ms:
        sleeper(max(0, delay_ms) / 1000.0)
        delayed = _snapshot_edge_rows(
            pair,
            connectors,
            market_ids,
            fee_round_trip=fee_round_trip,
            target_size=target_size,
            run_id=probe_run_id,
            capture_seq=delay_ms,
        )
        for row in delayed:
            row["benchmark_ms"] = delay_ms
            row["probe_source"] = "public_refetch"
            edge = _survival_edge(row)
            row["survived"] = edge is not None and edge > 0
            row["survived_edge"] = edge
        rows.extend(delayed)
    classify_survival_tiers(rows)
    return rows


def _snapshot_edge_rows(
    pair: EdgePair,
    connectors: Mapping[str, PublicMarketDataConnector],
    market_ids: Mapping[str, str],
    *,
    fee_round_trip: float,
    target_size: float,
    run_id: str,
    capture_seq: int,
) -> list[dict[str, Any]]:
    snapshot = capture_connector_snapshot(connectors, market_ids)
    k_book = snapshot.books["kalshi"]
    p_book = snapshot.books["polymarket"]
    base_ns = time.monotonic_ns()
    finish_delta_ns = int(
        (
            snapshot.response_received_at["kalshi"]
            - snapshot.response_received_at["polymarket"]
        ).total_seconds()
        * 1e9
    )
    k_recv_ns = base_ns
    p_recv_ns = base_ns - finish_delta_ns
    k_row = _book_to_observation(
        k_book,
        capture_seq=capture_seq,
        recv_monotonic_ns=k_recv_ns,
        capture_ts_utc=snapshot.response_received_at["kalshi"],
        fetch_elapsed_ms=snapshot.fetch_elapsed_ms["kalshi"],
        run_id=run_id,
    )
    p_row = _book_to_observation(
        p_book,
        capture_seq=capture_seq,
        recv_monotonic_ns=p_recv_ns,
        capture_ts_utc=snapshot.response_received_at["polymarket"],
        fetch_elapsed_ms=snapshot.fetch_elapsed_ms["polymarket"],
        run_id=run_id,
    )
    rows = _edge_rows_for_capture(
        pair,
        k_row,
        p_row,
        fee_round_trip=fee_round_trip,
        target_size=target_size,
    )
    for row in rows:
        row["public_probe"] = True
    return rows


def _survival_edge(row: dict[str, Any]) -> float | None:
    depth_edge = row.get("depth_adj_edge")
    if isinstance(depth_edge, (int, float)) and row.get("depth_liquidity_complete"):
        return float(depth_edge)
    fee_edge = row.get("fee_adj_edge")
    if isinstance(fee_edge, (int, float)):
        return float(fee_edge)
    return None
