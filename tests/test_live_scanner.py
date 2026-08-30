# Live arbitrage scanner: batching, detection, capture toggles.
"""Drives ``ArbScanner`` with an injected paired-fetch callable (no network).

Verifies the rolling cursor covers the universe inside each refresh window and
wraps deterministically, that a positive after-fee cross is logged while a
negative one is not, that book recording toggles, and that a per-pair fetch
error skips the pair without killing the tick.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from arbx.analysis.edges import EdgePair
from arbx.capture.sink import ObservationSink
from arbx.capture.types import BookSnapshot, PairedSnapshot, paired_snapshot
from arbx.scanner import ArbScanner, OpportunitySink, ScannerConfig


@dataclass
class _P:  # minimal PairSpec stand-in: the scanner only reads .pair_key
    pair_key: str


async def _noop(_delay: float) -> None:
    return None


def _book(venue: str, best_bid: float, best_ask: float, *, recv_ns: int) -> BookSnapshot:
    ts = datetime.now(timezone.utc)
    bids = tuple((round(best_bid - 0.01 * i, 4), 100.0) for i in range(5))
    asks = tuple((round(best_ask + 0.01 * i, 4), 100.0) for i in range(5))
    return BookSnapshot(
        venue=venue,
        market_id=f"{venue}_m",
        recv_monotonic_ns=recv_ns,
        capture_ts_utc=ts,
        venue_book_ts=ts,
        bids=bids,
        asks=asks,
        fetch_elapsed_ms=5.0,
        connector_source="live_public",
    )


def _paired(pair_key: str, kalshi: tuple[float, float], poly: tuple[float, float]) -> PairedSnapshot:
    k = _book("kalshi", kalshi[0], kalshi[1], recv_ns=1_000_000_000)
    p = _book("polymarket", poly[0], poly[1], recv_ns=1_001_000_000)  # 1ms skew
    return paired_snapshot(pair_key, k, p)


def _read_opps(data_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((data_dir / "scan" / "opportunities").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _scanner(pairs, edge_pairs, fetch, **kw):
    kw.setdefault("config", ScannerConfig(batch_size=2, tick_s=0.0))
    return ArbScanner(pairs, edge_pairs, fetch_pair=fetch, fee_engine=None,
                      sleeper=_noop, **kw)


def test_rolling_cursor_covers_universe_inside_refresh_window():
    pairs = [_P(f"k{i}|t{i}") for i in range(5)]
    seen: list[str] = []

    async def fetch(pair):
        seen.append(pair.pair_key)
        return None

    scanner = _scanner(pairs, {}, fetch)
    # ceil(5/2) == 3 ticks is one refresh window. The pinned scheduler wraps
    # inside the third batch, so the first pair appears twice.
    asyncio.run(scanner.run(max_ticks=3))
    counts = Counter(seen)
    assert all(counts[p.pair_key] >= 1 for p in pairs)
    assert max(counts.values()) == 2


def test_cursor_wraps_deterministically_to_zero():
    pairs = [_P(f"k{i}|t{i}") for i in range(5)]
    seen: list[str] = []

    async def fetch(pair):
        seen.append(pair.pair_key)
        return None

    scanner = _scanner(pairs, {}, fetch)
    asyncio.run(scanner.run(max_ticks=5))  # 5 ticks * 2/pairs = two passes.
    assert Counter(seen) == {p.pair_key: 2 for p in pairs}


def test_positive_cross_logged_as_opportunity(tmp_path):
    pk = "kX|tX"
    edge_pairs = {pk: EdgePair(pk, "kX", "tX", True)}
    paired = _paired(pk, kalshi=(0.38, 0.40), poly=(0.60, 0.62))  # raw ~0.20 one dir

    async def fetch(pair):
        return paired

    opp = OpportunitySink(tmp_path)
    scanner = _scanner(
        [_P(pk)], edge_pairs, fetch,
        config=ScannerConfig(batch_size=1, tick_s=0.0, record_books=False),
        opportunity_sink=opp,
    )
    stats = asyncio.run(scanner.run(max_ticks=1))
    assert stats.arbs_detected >= 1
    rows = _read_opps(tmp_path)
    assert rows
    assert any(r["arb_detected"] and r["fee_adj_edge"] > 0 for r in rows)


def test_negative_cross_not_logged(tmp_path):
    pk = "kX|tX"
    edge_pairs = {pk: EdgePair(pk, "kX", "tX", True)}
    paired = _paired(pk, kalshi=(0.35, 0.60), poly=(0.40, 0.62))  # both dirs negative

    async def fetch(pair):
        return paired

    opp = OpportunitySink(tmp_path)
    scanner = _scanner(
        [_P(pk)], edge_pairs, fetch,
        config=ScannerConfig(batch_size=1, tick_s=0.0, record_books=False),
        opportunity_sink=opp,
    )
    stats = asyncio.run(scanner.run(max_ticks=1))
    assert stats.arbs_detected == 0
    assert opp.count == 0
    assert _read_opps(tmp_path) == []


def test_book_recording_toggle(tmp_path):
    pk = "kX|tX"
    edge_pairs = {pk: EdgePair(pk, "kX", "tX", True)}
    paired = _paired(pk, kalshi=(0.38, 0.40), poly=(0.60, 0.62))

    async def fetch(pair):
        return paired

    # record_books=True -> raw/book rows land in the recorder layout.
    on_dir = tmp_path / "on"
    sink = ObservationSink(on_dir, run_id="t", ntp_offset_ms=None)
    scanner = _scanner(
        [_P(pk)], edge_pairs, fetch,
        config=ScannerConfig(batch_size=1, tick_s=0.0, record_books=True),
        sink=sink, opportunity_sink=OpportunitySink(on_dir),
    )
    asyncio.run(scanner.run(max_ticks=1))
    book_files = list((on_dir / "raw" / "book").rglob("*.jsonl"))
    assert book_files, "expected book rows written under raw/book"

    # record_books=False -> no book dir, opportunities still logged.
    off_dir = tmp_path / "off"
    opp = OpportunitySink(off_dir)
    scanner = _scanner(
        [_P(pk)], edge_pairs, fetch,
        config=ScannerConfig(batch_size=1, tick_s=0.0, record_books=False),
        opportunity_sink=opp,
    )
    asyncio.run(scanner.run(max_ticks=1))
    assert not (off_dir / "raw" / "book").exists()
    assert opp.count >= 1


def test_survival_probe_labels_persisted_edge(tmp_path):
    pk = "kX|tX"
    edge_pairs = {pk: EdgePair(pk, "kX", "tX", True)}
    cross = _paired(pk, kalshi=(0.38, 0.40), poly=(0.60, 0.62))  # edge present

    async def fetch(pair):  # edge still there on the 200ms refetch
        return cross

    opp = OpportunitySink(tmp_path)
    scanner = _scanner(
        [_P(pk)], edge_pairs, fetch,
        config=ScannerConfig(batch_size=1, tick_s=0.0, record_books=False,
                             confirm_survival_ms=200.0),
        opportunity_sink=opp,
    )
    stats = asyncio.run(scanner.run(max_ticks=1))
    assert stats.survival_probes == 1
    assert stats.survived_confirmed >= 1
    rows = _read_opps(tmp_path)
    survived = [r for r in rows if r["arb_detected"]]
    assert survived and survived[0]["survived_probe"] is True
    assert survived[0]["survived_probe_delay_ms"] == 200.0


def test_survival_probe_flags_vanished_edge(tmp_path):
    pk = "kX|tX"
    edge_pairs = {pk: EdgePair(pk, "kX", "tX", True)}
    cross = _paired(pk, kalshi=(0.38, 0.40), poly=(0.60, 0.62))   # detect: edge present
    gone = _paired(pk, kalshi=(0.38, 0.60), poly=(0.40, 0.62))    # probe: edge gone
    calls = {"n": 0}

    async def fetch(pair):
        calls["n"] += 1
        return cross if calls["n"] == 1 else gone

    opp = OpportunitySink(tmp_path)
    scanner = _scanner(
        [_P(pk)], edge_pairs, fetch,
        config=ScannerConfig(batch_size=1, tick_s=0.0, record_books=False,
                             confirm_survival_ms=200.0),
        opportunity_sink=opp,
    )
    stats = asyncio.run(scanner.run(max_ticks=1))
    assert stats.survival_probes == 1
    assert stats.survived_confirmed == 0
    rows = _read_opps(tmp_path)
    survived = [r for r in rows if r["arb_detected"]]
    assert survived and survived[0]["survived_probe"] is False


def test_fetch_error_skips_pair_not_tick():
    pairs = [_P("bad"), _P("good")]

    async def fetch(pair):
        if pair.pair_key == "bad":
            raise RuntimeError("boom")
        return None

    scanner = _scanner(pairs, {}, fetch)
    stats = asyncio.run(scanner.run(max_ticks=1))
    assert stats.ticks == 1
    assert stats.pairs_scanned == 2
    assert stats.fetch_errors == 1


def test_progress_is_published_each_tick(tmp_path: Path):
    """Counters must be visible during a run, not only after it exits.

    The cockpit reads the summary the child writes on exit, so a scan that was
    capturing real data showed zeros for its whole duration.
    """
    pairs = [_P(f"k{i}|t{i}") for i in range(4)]

    async def fetch(pair):
        return None

    progress = tmp_path / "scan_progress.json"
    scanner = _scanner(pairs, {}, fetch, progress_path=progress)
    asyncio.run(scanner.run(max_ticks=3))

    published = json.loads(progress.read_text(encoding="utf-8"))
    assert published["ticks"] == 3
    assert published["run_id"] == scanner.run_id
    assert "generated_at" in published


def test_progress_path_is_optional(tmp_path: Path):
    """A scanner without a progress path must behave exactly as before."""
    pairs = [_P("k0|t0")]

    async def fetch(pair):
        return None

    scanner = _scanner(pairs, {}, fetch)
    stats = asyncio.run(scanner.run(max_ticks=2))
    assert stats.ticks == 2
    assert not list(tmp_path.iterdir())
