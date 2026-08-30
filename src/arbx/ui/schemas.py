# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Standardized UI objects.
"""Canonical standardized schemas for the local research UI.

These frozen dataclasses define the service/UI boundary. Field *semantics* are
load-bearing — ``est_profit`` is executable (depth-haircut, Kalshi-constrained)
candidate value, never visible-depth value and never realized profit;
``est_fees`` comes from the real FeeEngine, never the flat heuristic; the
honest-status fields (``simulation_scope``, ``contract_equivalent``,
``include_in_strategy_metrics``) are mandatory.

No I/O here; nothing beyond stdlib. ``to_dict()`` returns JSON-safe
primitives (tuples become lists) so every object serializes directly into
the operation envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


def _json_safe(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


class _ToDictMixin:
    def to_dict(self) -> dict[str, Any]:
        """Flat JSON-safe dict: field name → primitive/list/dict value."""
        return {f.name: _json_safe(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class StandardizedEdgeRow(_ToDictMixin):
    """Module 2 EDGES files + Module 4 edge rows.

    Field → source edge-row column mapping (edge rows are produced by
    ``arbx.analysis.edges`` and stamped by ``arbx.scanner.live_scanner``):

    - ``edge_id``                ← ``f"{soak_id}:{byte_offset}"`` of the row in its
                                   JSONL file (or a content hash when offsets are
                                   unstable) — stable, selectable, linkable.
    - ``pair_key``               ← ``pair_key``.
    - ``display_name``           ← pair registry ``display_name`` for ``pair_key``.
    - ``direction``              ← ``direction`` ("kalshi_yes_poly_no" |
                                   "kalshi_no_poly_yes").
    - ``scanned_at``             ← scanner ``scanned_at``; for recorder-derived
                                   rows, ``capture_ts_utc``.
    - ``arb_detected``           ← scanner ``arb_detected`` (permissive after-fee
                                   cross).
    - ``qualifies``              ← scanner ``qualifies`` (full ``qualifies()``
                                   gate).
    - ``round_trip_latency_ms``  ← max(recv) − min(recv) of the paired fetch
                                   (the two concurrent venue fetches'
                                   ``recv_monotonic_ns`` span).
    - ``est_fees``               ← ``fee_usd_at_target / target_size`` (USD/unit,
                                   real FeeEngine — never the flat heuristic).
    - ``est_profit``             ← ``depth_adj_edge × executable_size`` (USD at
                                   executable size — displayed candidate value,
                                   never realized profit).
    - ``raw_edge``               ← ``raw_edge``.
    - ``fee_adj_edge``           ← ``fee_adj_edge`` (real-fee override when the
                                   fee engine ran).
    - ``depth_adj_edge``         ← ``depth_adj_edge``.
    - ``visible_size``           ← ``depth_fillable_size`` (visible two-leg depth
                                   at target size).
    - ``executable_size``        ← ``max_profitable_size`` (fee-profitable
                                   depth-haircut size) — always distinguished
                                   from ``visible_size``.
    - ``vwap_kalshi``            ← ``kalshi_vwap``.
    - ``vwap_polymarket``        ← ``polymarket_vwap``.
    - ``slippage``               ← ``slippage``.
    - ``capture_skew_ms``        ← ``capture_skew_ms``.
    - ``freshness_status``       ← worst of ``kalshi_freshness_status`` /
                                   ``polymarket_freshness_status`` (see
                                   ``books_fresh``).
    - ``survival_tier``          ← ``survival_tier`` (null until a survival
                                   source populates the probe ladder).
    - ``fee_model_version``      ← ``fee_model_version``.
    - ``simulation_scope``, ``contract_equivalent``,
      ``include_in_strategy_metrics`` ← pair registry honest-status fields
      (``include_in_strategy_metrics`` also stamped on the edge row itself).
    """

    edge_id: str
    pair_key: str
    display_name: str
    direction: str
    scanned_at: str                              # ISO-8601 UTC
    arb_detected: bool
    qualifies: bool
    round_trip_latency_ms: float
    est_fees: float                              # USD/unit, real FeeEngine
    est_profit: float                            # USD at executable size
    raw_edge: float
    fee_adj_edge: float
    depth_adj_edge: float
    visible_size: float
    executable_size: float
    vwap_kalshi: float | None
    vwap_polymarket: float | None
    slippage: float | None
    capture_skew_ms: float
    freshness_status: str
    survival_tier: str | None
    fee_model_version: str
    simulation_scope: str                        # honest status fields, mandatory
    contract_equivalent: str
    include_in_strategy_metrics: bool


@dataclass(frozen=True)
class StandardizedDataRow(_ToDictMixin):
    """Module 4 soak rows (book-level)."""

    pair_key: str
    display_name: str
    captured_at: str
    round_trip_duration_ms: float
    est_fees: float | None
    est_profit: float | None
    freshness_status: str
    staleness_seconds: float | None
    dq_flags: tuple[str, ...]
    simulation_scope: str
    include_in_strategy_metrics: bool


@dataclass(frozen=True)
class SoakFileMeta(_ToDictMixin):
    """Module 4 list rows / Module 2 outputs."""

    soak_id: str                    # dir name, e.g. "scan_20260705-141530[_EDGES]"
    label: str
    path: str
    started_at: str
    ended_at: str | None
    pair_keys: tuple[str, ...]
    pair_count: int
    edges_only: bool
    record_books: bool
    row_counts: dict[str, int]      # {"book": n, "opportunities": n, "edges": n}
    dq_status: str                  # "pass" | "fail" | "unknown"
    legacy_book_fix_applied: bool
    size_bytes: int
    schema_version: int


@dataclass(frozen=True)
class AnalysisSummary(_ToDictMixin):
    """Module 2 "run full analysis"."""

    soak_ids: tuple[str, ...]
    generated_at: str
    profit_score: float             # candidate score, not realized profit
    min_latency_needed_ms: float | None   # p50 survived_through_ms; None if none
    chance_of_profit: float
    chance_of_loss: float
    would_have_made_money_live: dict  # {"verdict", "rationale": [...], "basis"}
    dq: dict                        # {"passed", "recorder_health_passed",
                                    #  "freshness_passed", "detail"}
    fee_sensitivity: dict           # candidate counts at 1¢/2¢/real fees
    per_pair: tuple[dict, ...]      # per-pair EV/episode summary rows
    sample: dict                    # {"snapshots", "qualifying_rows", "soak_hours"}
    graph: dict | None              # {"kind": "edge_timeline_v1", "payload": dict}
    caveats: tuple[str, ...]        # honesty strings


@dataclass(frozen=True)
class PairSummary(_ToDictMixin):
    """Module 3."""

    pair_key: str
    display_name: str
    status: str                     # registry status
    kalshi_market_id: str
    polymarket_condition_id: str
    polymarket_yes_token_id: str
    resolution_structure: str
    grouping_alignment: str
    date_cutoff_delta_hours: float | None
    time_to_resolution_days: float | None
    persistence_cause: str
    equivalence: dict               # {"status","audited_at","auditor","notes",
                                    #  "tail_risks":[...]}
    orientation_confirmed: dict
    liquidity: dict | None
    edge_behavior: dict | None      # from evidence packs when present
    evidence_links: tuple[str, ...]  # repo-relative paths + venue URLs
    latest_decision: dict | None    # {"at","decision","rationale"}
    simulation_scope: str
    contract_equivalent: str
    include_in_strategy_metrics: bool


@dataclass(frozen=True)
class TestSuiteResult(_ToDictMixin):
    """Module 2 "run full test suite"."""

    __test__ = False  # contract-pinned name; not a pytest test class

    passed: bool
    total: int
    failures: int
    errors: int
    duration_s: float
    message: str                    # "The bot is working properly." |
                                    # "The bot is NOT healthy: <n> failing tests."
    detail_path: str                # reports/test_runs/<ts>.txt
