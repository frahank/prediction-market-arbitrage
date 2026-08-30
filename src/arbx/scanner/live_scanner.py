# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Round-robin batched live arbitrage scanner.
"""ArbScanner: cycle the pair universe in fixed batches, detect and capture.

Each tick the scanner takes the next ``batch_size`` pairs off a rolling cursor,
wrapping inside the batch when needed, fetches both venues concurrently per pair
(reusing the RTT-compensated capture path), prices the cross with the real fee
engine, and logs every qualifying (and near-miss) opportunity. The effective
per-pair refresh window takes ``ceil(len(pairs)/batch_size) × tick_s``.

Detection is a displayed-book estimate, never a fill or profit claim. The
scanner issues only public GETs; no order-mutation code exists here.

The fetch callable, sinks, sleeper, and clock are constructor arguments so the
scanner runs deterministically offline in tests (``tests/test_live_scanner.py``).
See ``docs/live_scanner.md``.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from arbx.analysis.edges import EdgePair, edge_rows_for_capture
from arbx.analysis.episodes import qualifies
from arbx.analysis.survival import _survival_edge
from arbx.capture.sink import ObservationSink
from arbx.capture.types import PairedSnapshot
from arbx.fees.engine import FeeEngine
from arbx.pairs.registry import PairSpec
from arbx.scanner.edges_writer import EdgesWriter
from arbx.scanner.rotation import RotationScheduler, effective_cadence_s

# fetch one pair -> its paired snapshot, or None when a venue leg failed.
PairFetcher = Callable[[PairSpec], Awaitable["PairedSnapshot | None"]]


@dataclass(frozen=True)
class ScannerConfig:
    batch_size: int = 20            # pairs scanned per tick
    tick_s: float = 1.0             # seconds between ticks
    min_arb_edge: float = 0.0       # log a row when fee_adj_edge > this
    target_size: float = 1.0        # depth target for edge/fill sizing
    record_books: bool = True       # persist raw book rows (recorder layout)
    ntp_remeasure_s: float = 900.0  # re-measure clock offset this often
    confirm_survival_ms: float | None = None  # if set, one delayed refetch per
    #                                           detection to label survival
    confirm_survival_ms_list: tuple[float, ...] | None = None  # multi-rung probe
    #   (e.g. (100.0, 200.0, 400.0)); when set it overrides confirm_survival_ms
    #   and records a probe_<d>ms_* block per rung (200 mirrored to legacy fields)

    def cycle_time_s(self, n_pairs: int) -> float:
        return effective_cadence_s(n_pairs, self.batch_size, self.tick_s)


class OpportunitySink:
    """Append detected-opportunity rows to ``<data_dir>/scan/opportunities``."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir) / "scan" / "opportunities"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._date: str | None = None
        self.count = 0

    def write(self, record: dict[str, Any]) -> None:
        date = (record.get("scanned_at") or "")[:10] or datetime.now(
            timezone.utc
        ).date().isoformat()
        if date != self._date:
            if self._fh is not None:
                self._fh.close()
            self._fh = (self._dir / f"{date}.jsonl").open("a", encoding="utf-8")
            self._date = date
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


@dataclass
class ScanStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ticks: int = 0
    lag_ticks: int = 0
    pairs_scanned: int = 0
    snapshots: int = 0
    fetch_errors: int = 0
    fetch_skips: int = 0
    arbs_detected: int = 0
    qualifying: int = 0
    survival_probes: int = 0
    survived_confirmed: int = 0
    by_pair: dict[str, int] = field(default_factory=dict)
    skews: list[float] = field(default_factory=list)

    def _pct(self, p: float) -> float | None:
        if not self.skews:
            return None
        ordered = sorted(self.skews)
        idx = min(len(ordered) - 1, int(len(ordered) * p / 100.0))
        return round(ordered[idx], 2)

    def summary(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ticks": self.ticks,
            "lag_ticks": self.lag_ticks,
            "pairs_scanned": self.pairs_scanned,
            "snapshots": self.snapshots,
            "fetch_errors": self.fetch_errors,
            "fetch_skips": self.fetch_skips,
            "arbs_detected": self.arbs_detected,
            "qualifying": self.qualifying,
            "survival_probes": self.survival_probes,
            "survived_confirmed": self.survived_confirmed,
            "opportunities_by_pair": dict(
                sorted(self.by_pair.items(), key=lambda kv: kv[1], reverse=True)
            ),
            "skew_ms": {
                "p50": self._pct(50),
                "p95": self._pct(95),
                "p99": self._pct(99),
                "max": round(max(self.skews), 2) if self.skews else None,
            },
        }


class ArbScanner:
    """Round-robin batched detection + capture over the pair universe."""

    def __init__(
        self,
        pairs: list[PairSpec],
        edge_pairs: dict[str, EdgePair],
        *,
        fetch_pair: PairFetcher,
        fee_engine: FeeEngine | None,
        config: ScannerConfig | None = None,
        run_id: str | None = None,
        sink: ObservationSink | None = None,
        opportunity_sink: OpportunitySink | None = None,
        edges_writer: EdgesWriter | None = None,
        ntp_offset_ms: float | None = None,
        rotation_state_path: Path | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        ntp_measure: Callable[[], float | None] | None = None,
    ) -> None:
        self.pairs = pairs
        self._edge_pairs = edge_pairs
        self._fetch_pair = fetch_pair
        self._fee_engine = fee_engine
        self.config = config or ScannerConfig()
        self.run_id = run_id or f"scan_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        self.sink = sink
        self._opp_sink = opportunity_sink
        self._edges_writer = edges_writer
        self.ntp_offset_ms = ntp_offset_ms
        self._sleeper = sleeper
        self._clock = clock
        self._ntp_measure = ntp_measure
        self._rotation = RotationScheduler(
            [pair.pair_key for pair in pairs],
            batch_size=self.config.batch_size,
            state_path=rotation_state_path,
        )
        self._pair_by_key = {pair.pair_key: pair for pair in pairs}
        self._local_seq = 0

    def _next_batch(self) -> list[PairSpec]:
        plan = self._rotation.next_batch()
        return [self._pair_by_key[pair_key] for pair_key in plan.batch]

    def _next_seq(self) -> int:
        self._local_seq += 1
        return self._local_seq

    def _rows_for(
        self, paired: PairedSnapshot
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.config.record_books and self.sink is not None:
            return self.sink.write_paired(paired)
        k = paired.kalshi.to_observation_row(
            run_id=self.run_id, capture_seq=self._next_seq(),
            ntp_offset_ms=self.ntp_offset_ms,
        )
        p = paired.polymarket.to_observation_row(
            run_id=self.run_id, capture_seq=self._next_seq(),
            ntp_offset_ms=self.ntp_offset_ms,
        )
        return k, p

    def _edge_rows(self, pair: PairSpec, paired: PairedSnapshot, *, record: bool):
        """Edge rows for a paired capture; records books through the sink only
        when ``record`` (the survival refetch never touches the book sink)."""
        edge_pair = self._edge_pairs.get(pair.pair_key)
        if edge_pair is None:
            return []
        if record:
            k_row, p_row = self._rows_for(paired)
        else:
            k_row = paired.kalshi.to_observation_row(
                run_id=self.run_id, capture_seq=self._next_seq(),
                ntp_offset_ms=self.ntp_offset_ms,
            )
            p_row = paired.polymarket.to_observation_row(
                run_id=self.run_id, capture_seq=self._next_seq(),
                ntp_offset_ms=self.ntp_offset_ms,
            )
        return edge_rows_for_capture(
            edge_pair, k_row, p_row,
            fee_engine=self._fee_engine, target_size=self.config.target_size,
        )

    def _records_for(
        self, pair: PairSpec, paired: PairedSnapshot, *, tick_index: int
    ) -> list[dict[str, Any]]:
        scanned_at = datetime.now(timezone.utc).isoformat()
        records: list[dict[str, Any]] = []
        for row in self._edge_rows(pair, paired, record=True):
            fee_adj = row.get("fee_adj_edge")
            arb = isinstance(fee_adj, (int, float)) and fee_adj > self.config.min_arb_edge
            good = qualifies(row)
            if not (arb or good):
                continue
            records.append({
                **row,
                "scanned_at": scanned_at,
                "tick_index": tick_index,
                "arb_detected": bool(arb),
                "qualifies": bool(good),
                # Per-leg wire times of the concurrent fetch pair: the context
                # behind the pinned round_trip_latency_ms (edges_writer.py).
                "kalshi_fetch_elapsed_ms": paired.kalshi.fetch_elapsed_ms,
                "polymarket_fetch_elapsed_ms": paired.polymarket.fetch_elapsed_ms,
            })
        return records

    async def _confirm_survival(
        self, pending: list[tuple[PairSpec, list[dict[str, Any]]]], *, stats: ScanStats
    ) -> None:
        """One delayed refetch per detected pair; label whether the edge (same
        direction) still crosses after ``confirm_survival_ms``."""
        if self.config.confirm_survival_ms_list:
            await self._confirm_survival_multi(pending, stats=stats)
            return
        delay_ms = self.config.confirm_survival_ms or 0.0
        await self._sleeper(delay_ms / 1000.0)
        stats.survival_probes += len(pending)
        results = await asyncio.gather(
            *(self._fetch_pair(pair) for pair, _ in pending),
            return_exceptions=True,
        )
        for (pair, recs), res in zip(pending, results):
            delayed: dict[str | None, dict[str, Any]] = {}
            delayed_recv = None
            if not isinstance(res, BaseException) and res is not None:
                delayed_recv = res.kalshi.recv_monotonic_ns
                for row in self._edge_rows(pair, res, record=False):
                    delayed[row.get("direction")] = row
            for rec in recs:
                rec["survived_probe_delay_ms"] = delay_ms
                d = delayed.get(rec.get("direction"))
                if d is None:
                    rec["survived_probe"] = False
                    rec["survived_probe_edge"] = None
                    rec["survived_probe_qualifies"] = False
                    continue
                edge = _survival_edge(d)
                rec["survived_probe"] = bool(edge is not None and edge > 0)
                rec["survived_probe_edge"] = edge
                rec["survived_probe_qualifies"] = qualifies(d)
                if delayed_recv and isinstance(rec.get("recv_monotonic_ns"), int):
                    rec["survived_probe_elapsed_ms"] = round(
                        (delayed_recv - rec["recv_monotonic_ns"]) / 1e6, 2
                    )

    async def _confirm_survival_multi(
        self, pending: list[tuple[PairSpec, list[dict[str, Any]]]], *, stats: ScanStats
    ) -> None:
        """Probe survival at several latency rungs (e.g. 100/200/400 ms). Each rung
        is a refetch scheduled at its absolute offset from detection, so the rungs
        do not bleed into each other. Records a ``probe_<d>ms_*`` block per rung and
        mirrors the 200 ms rung (or the median) into the legacy ``survived_probe_*``
        fields so existing analysis keeps working."""
        delays = sorted(self.config.confirm_survival_ms_list or ())
        if not delays:
            return
        stats.survival_probes += len(pending)
        baseline = 200.0 if 200.0 in delays else delays[len(delays) // 2]

        async def _probe_at(delay_ms: float):
            await self._sleeper(delay_ms / 1000.0)
            res = await asyncio.gather(
                *(self._fetch_pair(pair) for pair, _ in pending),
                return_exceptions=True,
            )
            return delay_ms, res

        batches = await asyncio.gather(*(_probe_at(d) for d in delays))
        for delay_ms, results in batches:
            key = int(round(delay_ms))
            for (pair, recs), res in zip(pending, results):
                delayed: dict[str | None, dict[str, Any]] = {}
                delayed_recv = None
                if not isinstance(res, BaseException) and res is not None:
                    delayed_recv = res.kalshi.recv_monotonic_ns
                    for row in self._edge_rows(pair, res, record=False):
                        delayed[row.get("direction")] = row
                for rec in recs:
                    d = delayed.get(rec.get("direction"))
                    if d is None:
                        survived, edge, qual, elapsed = False, None, False, None
                    else:
                        edge = _survival_edge(d)
                        survived = bool(edge is not None and edge > 0)
                        qual = qualifies(d)
                        elapsed = None
                        if delayed_recv and isinstance(rec.get("recv_monotonic_ns"), int):
                            elapsed = round((delayed_recv - rec["recv_monotonic_ns"]) / 1e6, 2)
                    rec[f"probe_{key}ms_survived"] = survived
                    rec[f"probe_{key}ms_edge"] = edge
                    rec[f"probe_{key}ms_qualifies"] = qual
                    if elapsed is not None:
                        rec[f"probe_{key}ms_elapsed_ms"] = elapsed
                    if delay_ms == baseline:
                        rec["survived_probe_delay_ms"] = delay_ms
                        rec["survived_probe"] = survived
                        rec["survived_probe_edge"] = edge
                        rec["survived_probe_qualifies"] = qual
                        if elapsed is not None:
                            rec["survived_probe_elapsed_ms"] = elapsed

    async def _scan_batch(
        self, batch: list[PairSpec], *, tick_index: int, stats: ScanStats
    ) -> None:
        results = await asyncio.gather(
            *(self._fetch_pair(pair) for pair in batch),
            return_exceptions=True,
        )
        pending: list[tuple[PairSpec, list[dict[str, Any]]]] = []
        for pair, res in zip(batch, results):
            stats.pairs_scanned += 1
            if isinstance(res, BaseException):
                stats.fetch_errors += 1
                continue
            if res is None:
                stats.fetch_skips += 1
                continue
            stats.snapshots += 1
            stats.skews.append(abs(res.skew_ms))
            recs = self._records_for(pair, res, tick_index=tick_index)
            if recs:
                pending.append((pair, recs))

        if pending and (self.config.confirm_survival_ms_list or self.config.confirm_survival_ms):
            await self._confirm_survival(pending, stats=stats)

        for pair, recs in pending:
            for rec in recs:
                if self._opp_sink is not None:
                    self._opp_sink.write(rec)
                if self._edges_writer is not None:
                    self._edges_writer.write(pair, rec)
                stats.arbs_detected += int(rec["arb_detected"])
                stats.qualifying += int(rec["qualifies"])
                stats.survived_confirmed += int(bool(rec.get("survived_probe")))
                stats.by_pair[pair.pair_key] = stats.by_pair.get(pair.pair_key, 0) + 1

    async def run(
        self, *, duration_s: float | None = None, max_ticks: int | None = None
    ) -> ScanStats:
        """Scan until ``duration_s`` elapses or ``max_ticks`` are done.

        At least one bound should be set; with neither, runs until cancelled.
        """
        stats = ScanStats()
        start = self._clock()
        last_ntp = start
        next_tick_at = start
        tick_index = 0
        try:
            while True:
                if max_ticks is not None and tick_index >= max_ticks:
                    break
                if duration_s is not None and (self._clock() - start) >= duration_s:
                    break
                await self._scan_batch(
                    self._next_batch(), tick_index=tick_index, stats=stats
                )
                tick_index += 1
                stats.ticks = tick_index

                if self._ntp_measure is not None and (
                    self._clock() - last_ntp
                ) >= self.config.ntp_remeasure_s:
                    offset = self._ntp_measure()
                    if offset is not None:
                        self.ntp_offset_ms = offset
                        if self.sink is not None:
                            self.sink.ntp_offset_ms = offset
                    last_ntp = self._clock()

                next_tick_at += self.config.tick_s
                delay = next_tick_at - self._clock()
                if delay > 0:
                    await self._sleeper(delay)
                else:
                    stats.lag_ticks += 1
                    next_tick_at = self._clock()
        finally:
            if self._opp_sink is not None:
                self._opp_sink.close()
            if self._edges_writer is not None:
                self._edges_writer.close()
            if self.sink is not None:
                self.sink.close()
        return stats
