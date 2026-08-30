# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — First-class market-data recorder, independent of pair registry.
"""
Decoupled market-data recorder (Phase 1).

Records book_observations for any (venue, market_id) universe, regardless of
whether the market appears in configs/pairs.approved.yaml. Output is flat
NDJSON written to data/raw/book/venue=<venue>/<date>.jsonl — one row per
capture event, matching the schema in docs/dataset_schema.md.

This module has no dependency on the paper-simulation subsystem (executor,
positions, risk, live_paper). It is public-data only and requires no
credentials.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from arbx.capture.clock import measure_ntp_offset_ms
from arbx.core.models import ConnectorSource, OrderBook
from arbx.data.connector import AdapterMarketDataConnector
from arbx.data.freshness import (
    DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
    compute_freshness,
)
from arbx.venues.kalshi_public import KalshiAdapter
from arbx.venues.kalshi_public import KalshiApiProvider as KalshiPublicProvider
from arbx.venues.polymarket_public import PolymarketAdapter
from arbx.venues.polymarket_public import (
    PolymarketApiProvider as PolymarketPublicProvider,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_TOP_N = 5  # number of price levels to flatten into columns

# Clock discipline (docs/clock_discipline.md): the offset is re-measured on
# this cadence and stamped into every row; past this magnitude the host clock
# is drifting enough to blur latency buckets.
NTP_REFRESH_SECONDS = 900.0
NTP_WARN_ABS_MS = 250.0


def _measure_and_log_ntp(previous_ms: float | None = None) -> float | None:
    """Measure the NTP offset, log it, WARN when large; keep last on failure."""
    offset = measure_ntp_offset_ms()
    if offset is None:
        logger.info("ntp offset measurement failed; keeping previous=%s", previous_ms)
        return previous_ms
    if abs(offset) > NTP_WARN_ABS_MS:
        logger.warning(
            "host clock offset %.1fms exceeds %.0fms — latency buckets are "
            "untrustworthy until NTP sync recovers (docs/clock_discipline.md)",
            offset, NTP_WARN_ABS_MS,
        )
    else:
        logger.info("ntp offset %.1fms", offset)
    return offset


def book_to_observation(
    book: OrderBook,
    *,
    capture_seq: int,
    recv_monotonic_ns: int,
    capture_ts_utc: datetime,
    fetch_elapsed_ms: float,
    run_id: str,
    ntp_offset_ms: float | None = None,
    freshness_threshold_s: float = DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Convert an OrderBook to a flat book_observations row."""
    yes = list(book.yes_levels)
    no = list(book.no_levels)

    # best bid = highest price willing to buy YES = best yes level price
    best_bid = yes[0].price if yes else None
    # best ask = lowest price willing to sell YES = complement of best NO level
    # YES ask = 1 - NO bid (highest NO price is cheapest YES ask)
    best_ask = (1.0 - no[0].price) if no else None

    mid = ((best_bid + best_ask) / 2.0) if (best_bid is not None and best_ask is not None) else None
    spread = ((best_ask - best_bid)) if (best_bid is not None and best_ask is not None) else None

    venue_book_ts = book.timestamp.isoformat() if book.timestamp else None
    # Stamp freshness at capture so the modeling slice can filter without
    # recomputing, and the stored decision is reproducible even if the
    # threshold later changes (see arbx.data.freshness / data_quality).
    staleness_seconds, freshness_status = compute_freshness(
        capture_ts_utc,
        book.timestamp,
        best_bid=best_bid,
        best_ask=best_ask,
        threshold_seconds=freshness_threshold_s,
    )

    row: dict[str, Any] = {
        "venue": book.venue,
        "market_id": book.market_id,
        "capture_seq": capture_seq,
        "capture_ts_utc": capture_ts_utc.isoformat(),
        "recv_monotonic_ns": recv_monotonic_ns,
        "venue_book_ts": venue_book_ts,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "staleness_seconds": staleness_seconds,
        "freshness_status": freshness_status,
        "freshness_threshold_seconds": freshness_threshold_s,
        "fetch_elapsed_ms": fetch_elapsed_ms,
        "connector_source": book.connector_source.value,
        "reportable": book.reportable,
        "run_id": run_id,
        "ntp_offset_ms": ntp_offset_ms,
    }

    # Flatten top-N bid levels (YES side)
    for i in range(1, _TOP_N + 1):
        level = yes[i - 1] if i <= len(yes) else None
        row[f"bid_px_{i}"] = level.price if level else None
        row[f"bid_sz_{i}"] = level.size if level else None

    # Flatten top-N ask levels (NO side, inverted to YES-ask price)
    for i in range(1, _TOP_N + 1):
        level = no[i - 1] if i <= len(no) else None
        row[f"ask_px_{i}"] = (1.0 - level.price) if level else None
        row[f"ask_sz_{i}"] = level.size if level else None

    # Full book JSON for fidelity
    row["book_json"] = json.dumps({
        "yes_levels": [{"price": lv.price, "size": lv.size} for lv in book.yes_levels],
        "no_levels": [{"price": lv.price, "size": lv.size} for lv in book.no_levels],
    })

    return row


# Back-compat alias for ported callers that referenced the private name.
_book_to_observation = book_to_observation


# ---------------------------------------------------------------------------
# Connector construction
# ---------------------------------------------------------------------------

def build_live_public_connectors() -> dict[str, AdapterMarketDataConnector]:
    kalshi_conn = AdapterMarketDataConnector(
        venue="kalshi",
        adapter=KalshiAdapter(
            provider=KalshiPublicProvider(),
            connector_source=ConnectorSource.LIVE_PUBLIC,
        ),
        provider=KalshiPublicProvider(),
        connector_source=ConnectorSource.LIVE_PUBLIC,
        source_reference="kalshi public REST API",
        reportable=True,
    )
    poly_conn = AdapterMarketDataConnector(
        venue="polymarket",
        adapter=PolymarketAdapter(
            provider=PolymarketPublicProvider(),
            connector_source=ConnectorSource.LIVE_PUBLIC,
        ),
        provider=PolymarketPublicProvider(),
        connector_source=ConnectorSource.LIVE_PUBLIC,
        source_reference="polymarket public CLOB API",
        reportable=True,
    )
    return {"kalshi": kalshi_conn, "polymarket": poly_conn}


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

class _DailyWriter:
    """Appends NDJSON rows to data/raw/book/venue=<venue>/<date>.jsonl."""

    def __init__(self, data_dir: Path, venue: str) -> None:
        self._base = data_dir / "raw" / "book" / f"venue={venue}"
        self._base.mkdir(parents=True, exist_ok=True)
        self._venue = venue
        self._current_date: str = ""
        self._fh: Any = None

    def write(self, row: dict[str, Any]) -> None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if date_str != self._current_date:
            if self._fh is not None:
                self._fh.close()
            path = self._base / f"{date_str}.jsonl"
            self._fh = path.open("a", encoding="utf-8")
            self._current_date = date_str
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Gap heartbeat writer
# ---------------------------------------------------------------------------

class _HeartbeatWriter:
    """
    Writes heartbeat / gap / lifecycle records to data/raw/latency/<date>.jsonl.

    This stream is the dataset's continuity log: every cycle emits a heartbeat,
    every missed interval (intra-run or across a restart) emits a gap, and run
    start/stop plus universe changes are recorded so a consumer can tell exactly
    where the series is complete and where it is not.
    """

    def __init__(self, data_dir: Path) -> None:
        self._base = data_dir / "raw" / "latency"
        self._base.mkdir(parents=True, exist_ok=True)
        self._current_date: str = ""
        self._fh: Any = None

    def _write(self, row: dict[str, Any]) -> None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if date_str != self._current_date:
            if self._fh is not None:
                self._fh.close()
            path = self._base / f"{date_str}.jsonl"
            self._fh = path.open("a", encoding="utf-8")
            self._current_date = date_str
        self._fh.write(json.dumps(row) + "\n")
        self._fh.flush()

    def write_heartbeat(
        self, run_id: str, cycle: int, venues_ok: dict[str, bool], universe_size: int
    ) -> None:
        self._write({
            "kind": "recorder_heartbeat",
            "run_id": run_id,
            "cycle": cycle,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "recv_monotonic_ns": time.monotonic_ns(),
            "venues_ok": venues_ok,
            "universe_size": universe_size,
        })

    def write_gap(
        self,
        run_id: str,
        cycle: int,
        expected_at: datetime,
        actual_at: datetime,
        *,
        reason: str = "late_cycle",
    ) -> None:
        self._write({
            "kind": "recorder_gap",
            "run_id": run_id,
            "cycle": cycle,
            "reason": reason,
            "expected_at": expected_at.isoformat(),
            "actual_at": actual_at.isoformat(),
            "gap_ms": (actual_at - expected_at).total_seconds() * 1000,
            "recv_monotonic_ns": time.monotonic_ns(),
        })

    def write_event(self, kind: str, run_id: str, **fields: Any) -> None:
        """Emit a lifecycle event (recorder_start, recorder_stop, universe_change)."""
        row = {
            "kind": kind,
            "run_id": run_id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "recv_monotonic_ns": time.monotonic_ns(),
        }
        row.update(fields)
        self._write(row)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _last_heartbeat_at(data_dir: Path) -> datetime | None:
    """
    Return the wall-clock time of the most recent heartbeat across prior runs.

    Used on startup to annotate the downtime gap between a previous run's last
    heartbeat and this run's first cycle, so a restart leaves an explicit,
    machine-readable hole in the continuity log rather than a silent one.
    """
    latest: datetime | None = None
    base = data_dir / "raw" / "latency"
    if not base.exists():
        return None
    for jsonl in sorted(base.glob("*.jsonl")):
        try:
            lines = jsonl.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "recorder_heartbeat":
                continue
            observed = row.get("observed_at")
            if not observed:
                continue
            try:
                ts = datetime.fromisoformat(observed)
            except ValueError:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


# ---------------------------------------------------------------------------
# Universe: list of (venue, market_id) to record
# ---------------------------------------------------------------------------

def _polymarket_fetch_id(pair: dict[str, Any]) -> str | None:
    """
    The Polymarket id usable for a public book fetch.

    The CLOB ``/book`` endpoint keys on the YES CLOB **token id**, not the
    condition id, so prefer the token id and fall back to the condition id only
    when no token is recorded. Mirrors ``live_paper._polymarket_fetch_id`` so the
    recorder, edge layer, and opportunity runner all fetch the same object.
    """
    identifiers = pair.get("polymarket_identifiers")
    if isinstance(identifiers, dict):
        for key in ("yes_token_id", "token_id", "clob_token_id"):
            value = identifiers.get(key)
            if isinstance(value, str) and value:
                return value
        cond = identifiers.get("condition_id")
        if isinstance(cond, str) and cond:
            return cond
    return pair.get("polymarket_market_id")


def load_universe_from_registry(pairs_yaml_path: Path) -> list[tuple[str, str]]:
    """
    Build the recording universe from pairs.approved.yaml.

    Returns every unique (venue, market_id) found in the registry,
    regardless of contract_equivalent or include_in_strategy_metrics.
    The recorder is deliberately broader than the evaluator.
    """
    import yaml  # local import — optional dependency at recorder level

    with pairs_yaml_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    seen: set[tuple[str, str]] = set()
    universe: list[tuple[str, str]] = []
    for pair in data.get("pairs", []):
        kalshi_id = pair.get("kalshi_market_id") or pair.get("kalshi_identifiers", {}).get("market_ticker")
        poly_id = _polymarket_fetch_id(pair)
        if kalshi_id and ("kalshi", kalshi_id) not in seen:
            seen.add(("kalshi", kalshi_id))
            universe.append(("kalshi", kalshi_id))
        if poly_id and ("polymarket", poly_id) not in seen:
            seen.add(("polymarket", poly_id))
            universe.append(("polymarket", poly_id))

    return universe


def load_universe_from_list(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Accept an explicit [(venue, market_id), ...] list."""
    return list(items)


def load_universe_from_discovery(
    paths: list[Path],
    *,
    max_markets: int | None = None,
    min_activity: float = 0.0,
) -> list[tuple[str, str]]:
    """
    Build the recording universe from discovery result JSON files.

    Each path is a ``DiscoveryResult.to_record()`` dump (see
    ``scripts/discover_*_public_markets.py``) containing a ``markets`` array.
    Markets are ranked by ``activity_score`` (most active first) across all
    files, deduped on ``(venue, market_id)``, then truncated to ``max_markets``.

    This decouples the recorder's coverage from the 24-pair registry: re-running
    discovery and re-loading here is how the universe grows and retires markets
    over time (Phase 2 — coverage). Missing or malformed files are skipped so a
    failed discovery refresh never takes the recorder down.
    """
    candidates: list[tuple[float, str, str]] = []
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        venue_default = data.get("venue", "")
        for market in data.get("markets", []):
            venue = market.get("venue") or venue_default
            market_id = market.get("market_id")
            if not venue or not market_id:
                continue
            activity = float(market.get("activity_score", 0.0) or 0.0)
            if activity < min_activity:
                continue
            candidates.append((activity, venue, market_id))

    candidates.sort(key=lambda c: c[0], reverse=True)

    seen: set[tuple[str, str]] = set()
    universe: list[tuple[str, str]] = []
    for _activity, venue, market_id in candidates:
        key = (venue, market_id)
        if key in seen:
            continue
        seen.add(key)
        universe.append(key)
        if max_markets is not None and len(universe) >= max_markets:
            break

    return universe


# ---------------------------------------------------------------------------
# Core recorder loop
# ---------------------------------------------------------------------------

def run_recorder(
    universe: list[tuple[str, str]],
    *,
    data_dir: Path,
    run_id: str,
    interval_seconds: float = 30.0,
    connectors: dict[str, AdapterMarketDataConnector] | None = None,
    max_cycles: int | None = None,
    on_cycle: Callable[[int, list[dict[str, Any]]], None] | None = None,
    ntp_offset_ms: float | None = None,
    universe_provider: Callable[[], list[tuple[str, str]]] | None = None,
    universe_refresh_seconds: float | None = None,
    restart_gap_threshold_seconds: float | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """
    Run the market-data recorder until stopped or max_cycles reached.

    Args:
        universe: initial list of (venue, market_id) pairs to record.
        data_dir: root of the data/ directory tree.
        run_id: unique identifier for this session (used in every row).
        interval_seconds: target cadence between cycle starts.
        connectors: pre-built connectors; built fresh if None.
        max_cycles: stop after this many cycles (None = run until signal).
        on_cycle: optional callback(cycle, rows) called after each cycle.
        ntp_offset_ms: host clock vs NTP offset recorded in every row. When
            None (default), it is measured at start via SNTP and re-measured
            every ``NTP_REFRESH_SECONDS``; pass a value to pin it (tests).
        universe_provider: optional callable re-invoked on the refresh cadence to
            re-resolve the universe (e.g. from freshly re-run discovery), so
            markets are added/retired without restarting. Failures are logged and
            the previous universe is kept.
        universe_refresh_seconds: how often to call universe_provider. If None
            (default) the universe is fixed for the run.
        restart_gap_threshold_seconds: if the previous run's last heartbeat is
            older than this on startup, annotate the downtime as a restart gap.
            Defaults to ``interval_seconds * 2`` when a provider/continuous run is
            in use; pass explicitly to override.
        stop_event: optional externally-owned stop flag (used by the supervisor);
            a fresh one is created when omitted.
    """
    if connectors is None:
        connectors = build_live_public_connectors()

    # Clock discipline: measure at start unless the caller pinned a value,
    # then re-measure on the refresh cadence below.
    ntp_pinned = ntp_offset_ms is not None
    if not ntp_pinned:
        ntp_offset_ms = _measure_and_log_ntp()
    last_ntp_at = time.monotonic()

    writers = {
        "kalshi": _DailyWriter(data_dir, "kalshi"),
        "polymarket": _DailyWriter(data_dir, "polymarket"),
    }
    hb_writer = _HeartbeatWriter(data_dir)

    owns_stop_event = stop_event is None
    if stop_event is None:
        stop_event = threading.Event()

    original_sigint = original_sigterm = None
    if owns_stop_event:
        def _handle_signal(sig: int, frame: Any) -> None:
            print(f"\n[recorder] received signal {sig}, stopping after current cycle…")
            stop_event.set()

        original_sigint = signal.signal(signal.SIGINT, _handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, _handle_signal)

    capture_seq = _resume_seq(data_dir)

    # Restart-gap annotation: if a previous run left off and we are resuming,
    # record the downtime so the continuity log has an explicit hole.
    if restart_gap_threshold_seconds is None:
        restart_gap_threshold_seconds = interval_seconds * 2
    last_hb = _last_heartbeat_at(data_dir)
    now_utc = datetime.now(timezone.utc)
    if last_hb is not None and (now_utc - last_hb) > timedelta(seconds=restart_gap_threshold_seconds):
        hb_writer.write_gap(run_id, 0, last_hb, now_utc, reason="restart")

    hb_writer.write_event(
        "recorder_start",
        run_id,
        resumed_from_seq=capture_seq,
        universe_size=len(universe),
        interval_seconds=interval_seconds,
        ntp_offset_ms=ntp_offset_ms,
    )

    cycle = 0
    next_cycle_at = time.monotonic()
    last_refresh_at = time.monotonic()

    print(f"[recorder] run_id={run_id} universe={len(universe)} markets interval={interval_seconds}s")

    try:
        while not stop_event.is_set():
            if max_cycles is not None and cycle >= max_cycles:
                break

            # Periodic universe refresh: re-resolve and diff against current.
            if (
                universe_provider is not None
                and universe_refresh_seconds is not None
                and time.monotonic() - last_refresh_at >= universe_refresh_seconds
            ):
                last_refresh_at = time.monotonic()
                universe = _refresh_universe(universe, universe_provider, hb_writer, run_id, cycle)

            # Periodic NTP re-measure so long runs track clock drift.
            if not ntp_pinned and time.monotonic() - last_ntp_at >= NTP_REFRESH_SECONDS:
                last_ntp_at = time.monotonic()
                ntp_offset_ms = _measure_and_log_ntp(ntp_offset_ms)

            # Gap detection: flag if we started late
            now_mono = time.monotonic()
            if cycle > 0 and now_mono > next_cycle_at + interval_seconds * 0.1:
                expected_dt = datetime.now(timezone.utc) - timedelta(seconds=now_mono - next_cycle_at)
                hb_writer.write_gap(run_id, cycle, expected_dt, datetime.now(timezone.utc))

            cycle_rows: list[dict[str, Any]] = []
            venues_ok: dict[str, bool] = {"kalshi": True, "polymarket": True}

            for venue, market_id in universe:
                if stop_event.is_set():
                    break
                connector = connectors.get(venue)
                if connector is None:
                    continue

                capture_ts = datetime.now(timezone.utc)
                mono_before = time.monotonic()

                try:
                    book = connector.fetch_orderbook(market_id)
                    fetch_ms = (time.monotonic() - mono_before) * 1000.0
                    recv_mono_ns = time.monotonic_ns()
                except Exception as exc:
                    print(f"[recorder] fetch error {venue}/{market_id}: {exc}")
                    venues_ok[venue] = False
                    continue

                capture_seq += 1
                row = _book_to_observation(
                    book,
                    capture_seq=capture_seq,
                    recv_monotonic_ns=recv_mono_ns,
                    capture_ts_utc=capture_ts,
                    fetch_elapsed_ms=fetch_ms,
                    run_id=run_id,
                    ntp_offset_ms=ntp_offset_ms,
                )
                writers[venue].write(row)
                cycle_rows.append(row)

            hb_writer.write_heartbeat(run_id, cycle, venues_ok, len(universe))

            if on_cycle is not None:
                try:
                    on_cycle(cycle, cycle_rows)
                except Exception as exc:
                    print(f"[recorder] on_cycle callback error: {exc}")

            cycle += 1
            next_cycle_at += interval_seconds
            sleep_for = next_cycle_at - time.monotonic()
            if sleep_for > 0:
                stop_event.wait(timeout=sleep_for)

    finally:
        hb_writer.write_event(
            "recorder_stop",
            run_id,
            cycles=cycle,
            last_seq=capture_seq,
        )
        for w in writers.values():
            w.close()
        hb_writer.close()
        if owns_stop_event:
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)
        print(f"[recorder] stopped. cycles={cycle} total_rows={capture_seq}")


def _refresh_universe(
    current: list[tuple[str, str]],
    provider: Callable[[], list[tuple[str, str]]],
    hb_writer: _HeartbeatWriter,
    run_id: str,
    cycle: int,
) -> list[tuple[str, str]]:
    """
    Re-resolve the universe via ``provider`` and emit a universe_change event
    describing added/retired markets. On any provider error the previous
    universe is retained so a failed discovery refresh never stalls collection.
    """
    try:
        refreshed = provider()
    except Exception as exc:  # noqa: BLE001 — never let a refresh failure stop recording
        print(f"[recorder] universe refresh failed, keeping current universe: {exc}")
        return current

    if not refreshed:
        # An empty universe almost certainly means a failed discovery fetch;
        # keep recording the markets we already have rather than going dark.
        print("[recorder] universe refresh returned empty, keeping current universe")
        return current

    current_set = set(current)
    refreshed_set = set(refreshed)
    added = sorted(refreshed_set - current_set)
    retired = sorted(current_set - refreshed_set)
    if added or retired:
        hb_writer.write_event(
            "universe_change",
            run_id,
            cycle=cycle,
            size=len(refreshed),
            added=[list(m) for m in added],
            retired=[list(m) for m in retired],
        )
        print(f"[recorder] universe refreshed: +{len(added)} -{len(retired)} -> {len(refreshed)} markets")
    return refreshed


def _resume_seq(data_dir: Path) -> int:
    """Return the highest capture_seq seen in existing JSONL files so restart appends cleanly."""
    max_seq = 0
    for jsonl in (data_dir / "raw" / "book").rglob("*.jsonl"):
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    seq = json.loads(line).get("capture_seq", 0)
                    if seq > max_seq:
                        max_seq = seq
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass
    return max_seq
