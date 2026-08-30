# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Data-quality report over the recorder's raw landing (Phase 4).
"""
Data-quality report for the market-data recorder dataset.

Reads the Tier-1 raw landing written by ``market_recorder.py``:

    data/raw/book/venue=<venue>/<date>.jsonl     # book_observations
    data/raw/latency/<date>.jsonl                # continuity log

and computes the acceptance statistics in ``docs/dataset_schema.md`` §5:
rows/market/day, sampling-interval distribution, gap rate, duplicate rate,
staleness rate, and the per-run NTP offset flag. Pure stdlib — no pandas,
duckdb, or network — so it runs anywhere the recorder runs.

Two distinct axes (kept separate on purpose):

* **Recorder health** — rows/market/day, gap rate, duplicate ``capture_seq``,
  NTP offset. This is the binary acceptance gate (``passed()`` / ``--strict``).
* **Market freshness** — per-venue ``fresh`` / ``stale`` / ``missing_*`` counts.
  Staleness is a market-activity signal (an illiquid-but-valid market reads as
  stale through no recorder fault), so it is reported as a distribution and is
  *not* part of the hard gate. See ``arbx.data.freshness`` and
  ``docs/modeling_readiness_north_stars.md``.

Caching: pass ``cache_dir`` to memoize per-source-file partial stats keyed by
(size, mtime). Closed day-files are never reparsed; only changed/active files
are re-read. ``cache_dir=None`` (the default) parses everything and is exactly
equivalent — both paths share one aggregation function.

Cross-venue skew p99 is materialized in ``edge_observations`` (it requires the
pair layer), so it is reported as ``None`` here with a pointer, not faked.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from arbx.data.freshness import (
    DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
    FRESH,
    MISSING_BOOK,
    MISSING_VENUE_TIMESTAMP,
    STALE,
    compute_freshness,
    parse_ts,
)

# Acceptance thresholds (docs/dataset_schema.md §5), Phase 1–2 30s polling.
MIN_ROWS_PER_MARKET_DAY = 1_440
MAX_GAP_RATE = 0.05
MAX_STALENESS_RATE = 0.10
MAX_NTP_OFFSET_MS = 100.0
# Single source of truth lives in arbx.data.freshness; kept as a module alias for
# backwards compatibility with callers that imported it from here.
STALENESS_AGE_SECONDS = DEFAULT_FRESHNESS_THRESHOLD_SECONDS

# threshold_results() keys, grouped by axis.
_HEALTH_KEYS = ("rows_per_market_day", "gap_rate", "duplicate_seq", "ntp_offset", "crossed_books")
_FRESHNESS_KEYS = ("staleness_rate",)

_CACHE_VERSION = 3  # bump to invalidate stored partials on schema change


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct in [0, 100]); None for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


@dataclass
class MarketQuality:
    venue: str
    market_id: str
    rows: int = 0
    median_interval_s: float | None = None
    gap_intervals: int = 0
    expected_intervals: int = 0
    stale_rows: int = 0
    empty_book_rows: int = 0
    missing_ts_rows: int = 0
    fresh_rows: int = 0
    crossed_rows: int = 0

    @property
    def gap_rate(self) -> float:
        return (self.gap_intervals / self.expected_intervals) if self.expected_intervals else 0.0

    @property
    def staleness_rate(self) -> float:
        return (self.stale_rows / self.rows) if self.rows else 0.0


# ---------------------------------------------------------------------------
# Per-file partial stats (cache unit)
# ---------------------------------------------------------------------------


@dataclass
class _MarketPartial:
    rows: int = 0
    stale: int = 0
    empty: int = 0
    missing_ts: int = 0
    fresh: int = 0
    crossed: int = 0
    ts_epoch: list[float] = field(default_factory=list)


@dataclass
class _FilePartial:
    rows: int = 0
    seqs: list[int] = field(default_factory=list)
    fetch_ms: list[float] = field(default_factory=list)
    markets: dict[tuple[str, str], _MarketPartial] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "seqs": self.seqs,
            "fetch_ms": self.fetch_ms,
            "markets": [
                {
                    "venue": v, "market_id": m,
                    "rows": p.rows, "stale": p.stale, "empty": p.empty,
                    "missing_ts": p.missing_ts, "fresh": p.fresh,
                    "crossed": p.crossed, "ts": p.ts_epoch,
                }
                for (v, m), p in self.markets.items()
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "_FilePartial":
        markets: dict[tuple[str, str], _MarketPartial] = {}
        for m in data.get("markets", []):
            markets[(m["venue"], m["market_id"])] = _MarketPartial(
                rows=m["rows"], stale=m["stale"], empty=m["empty"],
                missing_ts=m["missing_ts"], fresh=m["fresh"],
                crossed=m.get("crossed", 0), ts_epoch=list(m["ts"]),
            )
        return cls(
            rows=data.get("rows", 0),
            seqs=list(data.get("seqs", [])),
            fetch_ms=list(data.get("fetch_ms", [])),
            markets=markets,
        )


def _parse_book_file(path: Path) -> _FilePartial:
    """Reduce one book day-file to the slim numeric stats the report needs."""
    fp = _FilePartial()
    for row in _iter_jsonl(path):
        venue = row.get("venue")
        market_id = row.get("market_id")
        if not venue or not market_id:
            continue
        fp.rows += 1
        mp = fp.markets.setdefault((venue, market_id), _MarketPartial())
        mp.rows += 1

        seq = row.get("capture_seq")
        if isinstance(seq, int):
            fp.seqs.append(seq)

        fe = row.get("fetch_elapsed_ms")
        if isinstance(fe, (int, float)):
            fp.fetch_ms.append(float(fe))

        cap = parse_ts(row.get("capture_ts_utc"))
        if cap is not None:
            mp.ts_epoch.append(cap.timestamp())

        # A single venue's book can never be crossed (its matching engine
        # would have traded through it). bid > ask therefore means the row's
        # sides are mislabeled — the inversion documented in
        # docs/book_semantics_fix.md. Hard health gate.
        bb, ba = row.get("best_bid"), row.get("best_ask")
        if isinstance(bb, (int, float)) and isinstance(ba, (int, float)) and bb > ba:
            mp.crossed += 1

        # Prefer the freshness the recorder stamped at capture; fall back to
        # recomputing for legacy rows written before the freshness columns.
        status = row.get("freshness_status")
        if status not in (FRESH, STALE, MISSING_VENUE_TIMESTAMP, MISSING_BOOK):
            _, status = compute_freshness(
                row.get("capture_ts_utc"),
                row.get("venue_book_ts"),
                best_bid=row.get("best_bid"),
                best_ask=row.get("best_ask"),
            )
        if status == STALE:
            mp.stale += 1
        elif status == MISSING_BOOK:
            mp.empty += 1
        elif status == MISSING_VENUE_TIMESTAMP:
            mp.missing_ts += 1
        else:
            mp.fresh += 1
    return fp


def _cache_file(cache_dir: Path, src: Path) -> Path:
    digest = hashlib.sha256(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{src.name}.{digest}.json"


def _load_or_parse(src: Path, cache_dir: Path | None) -> _FilePartial:
    if cache_dir is None:
        return _parse_book_file(src)
    try:
        st = src.stat()
        sig = [_CACHE_VERSION, st.st_size, st.st_mtime_ns]
    except OSError:
        return _parse_book_file(src)
    cpath = _cache_file(cache_dir, src)
    try:
        cached = json.loads(cpath.read_text(encoding="utf-8"))
        if cached.get("sig") == sig:
            return _FilePartial.from_json(cached["partial"])
    except (OSError, ValueError, KeyError):
        pass
    fp = _parse_book_file(src)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps({"sig": sig, "partial": fp.to_json()}), encoding="utf-8")
    except OSError:
        pass
    return fp


@dataclass
class DataQualityReport:
    data_dir: Path
    nominal_interval_s: float
    markets: list[MarketQuality] = field(default_factory=list)
    total_book_rows: int = 0
    duplicate_seq: int = 0
    fetch_ms_p50: float | None = None
    fetch_ms_p99: float | None = None
    # continuity log
    heartbeats: int = 0
    late_gaps: int = 0
    restart_gaps: int = 0
    universe_changes: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)
    skew_p99_ms: float | None = None  # materialized in edge_observations

    # ----- freshness (per-venue distribution; not a hard gate) ------------
    def freshness_by_venue(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for m in self.markets:
            v = out.setdefault(
                m.venue,
                {"fresh": 0, "stale": 0, "missing_venue_timestamp": 0,
                 "missing_book": 0, "rows": 0},
            )
            v["fresh"] += m.fresh_rows
            v["stale"] += m.stale_rows
            v["missing_venue_timestamp"] += m.missing_ts_rows
            v["missing_book"] += m.empty_book_rows
            v["rows"] += m.rows
        for v in out.values():
            v["fresh_rate"] = (v["fresh"] / v["rows"]) if v["rows"] else 0.0
            v["stale_rate"] = (v["stale"] / v["rows"]) if v["rows"] else 0.0
        return out

    # ----- thresholds -----------------------------------------------------
    def threshold_results(self) -> dict[str, tuple[bool, str]]:
        results: dict[str, tuple[bool, str]] = {}

        # rows/market/day — judged on the worst market with any data
        if self.markets:
            worst = min(m.rows for m in self.markets)
            results["rows_per_market_day"] = (
                worst >= MIN_ROWS_PER_MARKET_DAY,
                f"min {worst} rows (threshold ≥{MIN_ROWS_PER_MARKET_DAY})",
            )
            worst_gap = max((m.gap_rate for m in self.markets), default=0.0)
            results["gap_rate"] = (
                worst_gap < MAX_GAP_RATE,
                f"max {worst_gap:.1%} (threshold <{MAX_GAP_RATE:.0%})",
            )
            worst_stale = max((m.staleness_rate for m in self.markets), default=0.0)
            results["staleness_rate"] = (
                worst_stale < MAX_STALENESS_RATE,
                f"max {worst_stale:.1%} (threshold <{MAX_STALENESS_RATE:.0%}) "
                "— freshness signal, not a recorder-health gate",
            )

        results["duplicate_seq"] = (
            self.duplicate_seq == 0,
            f"{self.duplicate_seq} duplicate capture_seq (threshold 0)",
        )

        crossed = sum(m.crossed_rows for m in self.markets)
        results["crossed_books"] = (
            crossed == 0,
            f"{crossed} rows with best_bid > best_ask (threshold 0; "
            "bid/ask sides mislabeled — see docs/book_semantics_fix.md)",
        )

        bad_ntp = [
            r for r in self.runs
            if r.get("ntp_offset_ms") is not None and abs(r["ntp_offset_ms"]) > MAX_NTP_OFFSET_MS
        ]
        results["ntp_offset"] = (
            not bad_ntp,
            f"{len(bad_ntp)} run(s) with |offset| > {MAX_NTP_OFFSET_MS:.0f}ms"
            + (" (offsets null until Phase 3)" if all(r.get("ntp_offset_ms") is None for r in self.runs) else ""),
        )
        return results

    def recorder_health_passed(self) -> bool:
        """The acceptance gate: continuity health only, freshness excluded."""
        res = self.threshold_results()
        return all(res[k][0] for k in _HEALTH_KEYS if k in res)

    def freshness_passed(self) -> bool:
        res = self.threshold_results()
        return all(res[k][0] for k in _FRESHNESS_KEYS if k in res)

    def passed(self) -> bool:
        """Acceptance gate == recorder health. Freshness is reported separately."""
        return self.recorder_health_passed()

    # ----- rendering ------------------------------------------------------
    def render_text(self) -> str:
        lines: list[str] = []
        lines.append(f"Data-quality report — {self.data_dir}")
        lines.append(f"  nominal cadence: {self.nominal_interval_s:g}s")
        lines.append(f"  book rows: {self.total_book_rows}  markets: {len(self.markets)}")
        lines.append(f"  duplicate capture_seq: {self.duplicate_seq}")
        if self.fetch_ms_p50 is not None:
            lines.append(f"  fetch latency p50/p99: {self.fetch_ms_p50:.0f} / {self.fetch_ms_p99:.0f} ms")
        lines.append(
            f"  continuity: {self.heartbeats} heartbeats, "
            f"{self.late_gaps} late gaps, {self.restart_gaps} restart gaps, "
            f"{self.universe_changes} universe changes, {len(self.runs)} run(s)"
        )
        lines.append(f"  cross-venue skew p99: {'n/a (see edge_observations)' if self.skew_p99_ms is None else f'{self.skew_p99_ms:.0f} ms'}")
        lines.append("")
        lines.append("  Per-market (worst 10 by gap rate):")
        worst = sorted(self.markets, key=lambda m: (-m.gap_rate, -m.staleness_rate))[:10]
        for m in worst:
            lines.append(
                f"    {m.venue:11s} {m.market_id[:40]:40s} "
                f"rows={m.rows:6d} gap={m.gap_rate:5.1%} stale={m.staleness_rate:5.1%} "
                f"med_int={m.median_interval_s if m.median_interval_s is None else round(m.median_interval_s,1)}s"
            )
        lines.append("")
        lines.append("  Recorder-health thresholds (acceptance gate):")
        for name in _HEALTH_KEYS:
            if name in self.threshold_results():
                ok, detail = self.threshold_results()[name]
                lines.append(f"    [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        lines.append("")
        lines.append("  Freshness by venue (not a gate):")
        for venue, fv in sorted(self.freshness_by_venue().items()):
            lines.append(
                f"    {venue:11s} fresh={fv['fresh_rate']:5.1%} stale={fv['stale_rate']:5.1%} "
                f"(fresh={fv['fresh']} stale={fv['stale']} "
                f"missing_ts={fv['missing_venue_timestamp']} missing_book={fv['missing_book']})"
            )
        lines.append("")
        lines.append(f"  RECORDER HEALTH: {'PASS' if self.recorder_health_passed() else 'FAIL'}")
        lines.append(f"  FRESHNESS:       {'PASS' if self.freshness_passed() else 'CHECK'}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "nominal_interval_s": self.nominal_interval_s,
            "total_book_rows": self.total_book_rows,
            "duplicate_seq": self.duplicate_seq,
            "fetch_ms_p50": self.fetch_ms_p50,
            "fetch_ms_p99": self.fetch_ms_p99,
            "heartbeats": self.heartbeats,
            "late_gaps": self.late_gaps,
            "restart_gaps": self.restart_gaps,
            "universe_changes": self.universe_changes,
            "runs": self.runs,
            "passed": self.passed(),
            "recorder_health_passed": self.recorder_health_passed(),
            "freshness_passed": self.freshness_passed(),
            "freshness_by_venue": self.freshness_by_venue(),
            "thresholds": {k: {"ok": ok, "detail": d} for k, (ok, d) in self.threshold_results().items()},
            "markets": [
                {
                    "venue": m.venue,
                    "market_id": m.market_id,
                    "rows": m.rows,
                    "median_interval_s": m.median_interval_s,
                    "gap_rate": m.gap_rate,
                    "staleness_rate": m.staleness_rate,
                    "fresh_rows": m.fresh_rows,
                    "stale_rows": m.stale_rows,
                    "missing_ts_rows": m.missing_ts_rows,
                    "empty_book_rows": m.empty_book_rows,
                    "crossed_rows": m.crossed_rows,
                }
                for m in self.markets
            ],
        }


def _aggregate(
    partials: list[_FilePartial],
    nominal_interval_s: float,
) -> tuple[list[MarketQuality], int, int, float | None, float | None]:
    """Merge per-file partials into the global market list + scalar stats.

    Order-preserving over ``partials`` so duplicate ``capture_seq`` detection
    and percentile inputs match a single-pass parse exactly.
    """
    per_market: dict[tuple[str, str], MarketQuality] = {}
    per_market_ts: dict[tuple[str, str], list[float]] = {}
    seen_seq: set[int] = set()
    duplicate_seq = 0
    fetch_ms: list[float] = []
    total_rows = 0

    for fp in partials:
        total_rows += fp.rows
        for seq in fp.seqs:
            if seq in seen_seq:
                duplicate_seq += 1
            else:
                seen_seq.add(seq)
        fetch_ms.extend(fp.fetch_ms)
        for key, mp in fp.markets.items():
            mq = per_market.setdefault(key, MarketQuality(venue=key[0], market_id=key[1]))
            mq.rows += mp.rows
            mq.stale_rows += mp.stale
            mq.empty_book_rows += mp.empty
            mq.missing_ts_rows += mp.missing_ts
            mq.fresh_rows += mp.fresh
            mq.crossed_rows += mp.crossed
            per_market_ts.setdefault(key, []).extend(mp.ts_epoch)

    for key, epochs in per_market_ts.items():
        mq = per_market[key]
        epochs.sort()
        intervals = [epochs[i] - epochs[i - 1] for i in range(1, len(epochs))]
        mq.expected_intervals = len(intervals)
        mq.median_interval_s = _percentile(intervals, 50)
        threshold = nominal_interval_s * 2.0
        mq.gap_intervals = sum(1 for iv in intervals if iv > threshold)

    markets = sorted(per_market.values(), key=lambda m: (m.venue, m.market_id))
    return markets, total_rows, duplicate_seq, _percentile(fetch_ms, 50), _percentile(fetch_ms, 99)


def analyze(
    data_dir: Path,
    *,
    nominal_interval_s: float = 30.0,
    cache_dir: Path | None = None,
) -> DataQualityReport:
    """Compute a DataQualityReport over the raw landing under ``data_dir``.

    Pass ``cache_dir`` to reuse per-source-file partials for unchanged files
    (keyed by size+mtime). The result is identical to ``cache_dir=None``.
    """
    report = DataQualityReport(data_dir=data_dir, nominal_interval_s=nominal_interval_s)

    book_base = data_dir / "raw" / "book"
    sources = sorted(book_base.rglob("*.jsonl")) if book_base.exists() else []
    partials = [_load_or_parse(src, cache_dir) for src in sources]

    (report.markets, report.total_book_rows, report.duplicate_seq,
     report.fetch_ms_p50, report.fetch_ms_p99) = _aggregate(partials, nominal_interval_s)

    # continuity log (small; always parsed fresh)
    latency_base = data_dir / "raw" / "latency"
    for jsonl in sorted(latency_base.glob("*.jsonl")) if latency_base.exists() else []:
        for row in _iter_jsonl(jsonl):
            kind = row.get("kind")
            if kind == "recorder_heartbeat":
                report.heartbeats += 1
            elif kind == "recorder_gap":
                if row.get("reason") == "restart":
                    report.restart_gaps += 1
                else:
                    report.late_gaps += 1
            elif kind == "universe_change":
                report.universe_changes += 1
            elif kind == "recorder_start":
                report.runs.append({
                    "run_id": row.get("run_id"),
                    "observed_at": row.get("observed_at"),
                    "resumed_from_seq": row.get("resumed_from_seq"),
                    "ntp_offset_ms": row.get("ntp_offset_ms"),
                })

    return report
