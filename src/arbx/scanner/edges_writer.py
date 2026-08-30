# Scope: BOT_RUNTIME — M2-T3 EDGES file writer: StandardizedEdgeRow at capture time.
"""Persist ``StandardizedEdgeRow`` rows into ``EDGES_<ts>.jsonl`` as they are
detected — the capture-time twin of Module 4's read-side mapping.

This is where the product's honesty gets persisted. The pinned semantics:

- ``round_trip_latency_ms = max(leg fetch_elapsed_ms)`` — the slower leg's
  wire time for the pair's two CONCURRENT venue fetches. The legs fire
  together, so the pair of books is as old as the slower fetch; using the
  recv-to-recv span (capture skew, ~5ms p50) instead would hide the ~110ms
  REST floor an actual reaction pays. Skew is still stamped separately in
  ``capture_skew_ms``. When leg timings are missing (offline fixtures), the
  conservative fallback is |capture_skew_ms|, never 0. Also pinned in
  ``docs/soak_layout.md``.
- ``est_fees`` = ``fee_usd_at_target / target_size`` (the real FeeEngine
  stamp); rows priced without the engine fall back to the fee actually
  subtracted (``raw_edge − fee_adj_edge``) and are labeled
  ``fee_model_version: "flat_heuristic"`` so the flat path can never
  masquerade as real fees.
- ``est_profit = depth_adj_edge × executable_size`` with
  ``executable_size = depth_haircut × max_profitable_size``
  (``configs/modeling.yaml`` ``executable.depth_haircut``).
  ``max_profitable_size`` is the pairwise-min fee-profitable depth walk, so
  it is ≤ the raw min(leg fillable) — strictly more conservative — and it
  matches the pinned F-T2 schema mapping and Module 4's read side, keeping
  the edges view uniform. Never the visible size.
- Honest fields come from the registry: ``contract_equivalent`` ← the pair's
  equivalence status, ``include_in_strategy_metrics`` ← the loader's
  deny-by-default gated flag, ``simulation_scope = "public_displayed_books"``.

File layout per ``docs/soak_layout.md``: one ``StandardizedEdgeRow.to_dict()``
per line, appended at the soak root, flushed per row, fsync on close.
``edge_id`` is stamped at write time as ``<soak_id>:<filename>:<byte_offset>``
— the same shape Module 4 synthesizes for non-EDGES sources.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arbx.ui.schemas import StandardizedEdgeRow

SIMULATION_SCOPE = "public_displayed_books"

_SOAK_TS_RE = re.compile(r"^scan_(\d{8}-\d{6})")
_FRESHNESS_WORST_FIRST = ("missing_book", "missing_venue_timestamp", "stale")


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _float_or(value: Any, default: float = 0.0) -> float:
    parsed = _float_or_none(value)
    return default if parsed is None else parsed


def _pair_freshness(record: dict[str, Any]) -> str:
    kalshi = record.get("kalshi_freshness_status")
    polymarket = record.get("polymarket_freshness_status")
    statuses = {kalshi, polymarket}
    for status in _FRESHNESS_WORST_FIRST:
        if status in statuses:
            return status
    if kalshi == "fresh" and polymarket == "fresh":
        return "fresh"
    return "unknown"


def round_trip_latency_ms(record: dict[str, Any]) -> float:
    """The pinned definition: the slower leg's fetch wire time (see module
    docstring); |capture_skew_ms| only as the missing-timings fallback."""
    legs = [
        _float_or_none(record.get("kalshi_fetch_elapsed_ms")),
        _float_or_none(record.get("polymarket_fetch_elapsed_ms")),
    ]
    known = [leg for leg in legs if leg is not None]
    if known:
        return max(known)
    return abs(_float_or(record.get("capture_skew_ms")))


def build_edge_row(
    pair: Any,
    record: dict[str, Any],
    *,
    edge_id: str,
    depth_haircut: float,
) -> StandardizedEdgeRow:
    """Map a scanner opportunity record + its registry pair into the
    standardized row. ``pair`` is a ``PairSpec``; registry fields are read
    defensively (offline test stand-ins carry only ``pair_key``) with honest
    deny-by-default values."""
    pair_key = str(record.get("pair_key") or getattr(pair, "pair_key", ""))
    display_name = str(getattr(pair, "display_name", "") or pair_key)
    equivalence = getattr(pair, "equivalence", None)
    contract_equivalent = str(getattr(equivalence, "status", "") or "unreviewed")
    include = bool(getattr(pair, "include_in_strategy_metrics", False))

    raw_edge = _float_or(record.get("raw_edge"))
    fee_adj_edge = _float_or(record.get("fee_adj_edge"))
    depth_adj_edge = _float_or_none(record.get("depth_adj_edge"))
    executable_size = _float_or(record.get("max_profitable_size")) * depth_haircut

    fee_usd = _float_or_none(record.get("fee_usd_at_target"))
    target_size = _float_or_none(record.get("target_size"))
    if fee_usd is not None and target_size is not None and target_size > 0:
        est_fees = fee_usd / target_size
        fee_model_version = str(record.get("fee_model_version") or "unknown")
    else:
        est_fees = max(raw_edge - fee_adj_edge, 0.0)
        fee_model_version = "flat_heuristic"

    return StandardizedEdgeRow(
        edge_id=edge_id,
        pair_key=pair_key,
        display_name=display_name,
        direction=str(record.get("direction") or ""),
        scanned_at=str(record.get("scanned_at") or record.get("capture_ts_utc") or ""),
        arb_detected=bool(record.get("arb_detected", False)),
        qualifies=bool(record.get("qualifies", False)),
        round_trip_latency_ms=round_trip_latency_ms(record),
        est_fees=est_fees,
        est_profit=(depth_adj_edge * executable_size) if depth_adj_edge is not None else 0.0,
        raw_edge=raw_edge,
        fee_adj_edge=fee_adj_edge,
        depth_adj_edge=_float_or(depth_adj_edge),
        visible_size=_float_or(record.get("depth_fillable_size")),
        executable_size=executable_size,
        vwap_kalshi=_float_or_none(record.get("kalshi_vwap")),
        vwap_polymarket=_float_or_none(record.get("polymarket_vwap")),
        slippage=_float_or_none(record.get("slippage")),
        capture_skew_ms=_float_or(record.get("capture_skew_ms")),
        freshness_status=_pair_freshness(record),
        survival_tier=(
            str(record["survival_tier"]) if record.get("survival_tier") else None
        ),
        fee_model_version=fee_model_version,
        simulation_scope=SIMULATION_SCOPE,
        contract_equivalent=contract_equivalent,
        include_in_strategy_metrics=include,
    )


class EdgesWriter:
    """Append standardized edge rows to ``<data_dir>/EDGES_<ts>.jsonl``.

    ``qualifying_only=True`` (full-record runs) persists only rows passing the
    full ``qualifies()`` gate; edges-only runs persist every detected row.
    Appends are flushed per row; ``close()`` fsyncs so a finished soak's EDGES
    file is durable.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        depth_haircut: float,
        qualifying_only: bool = False,
        timestamp: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.depth_haircut = float(depth_haircut)
        self.qualifying_only = qualifying_only
        if timestamp is None:
            match = _SOAK_TS_RE.match(self.data_dir.name)
            timestamp = match.group(1) if match else (
                datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            )
        self.path = self.data_dir / f"EDGES_{timestamp}.jsonl"
        self._fh = None
        self.count = 0

    def write(self, pair: Any, record: dict[str, Any]) -> None:
        if self.qualifying_only and not record.get("qualifies"):
            return
        if self._fh is None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("ab")
        offset = self._fh.tell()
        edge_id = f"{self.data_dir.name}:{self.path.name}:{offset}"
        row = build_edge_row(
            pair, record, edge_id=edge_id, depth_haircut=self.depth_haircut
        )
        self._fh.write(json.dumps(row.to_dict()).encode("utf-8") + b"\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
