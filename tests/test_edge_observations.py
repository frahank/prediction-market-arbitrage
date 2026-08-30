# Scope: TEST — Unit tests for Phase 5 edge derivation.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbx.analysis.edges import (
    EdgePair,
    derive_edges,
    load_edge_pairs,
    write_edges_jsonl,
)


def _book(data_dir: Path, venue: str, market: str, *, seq: int, ns: int, bid, ask):
    d = data_dir / "raw" / "book" / f"venue={venue}"
    d.mkdir(parents=True, exist_ok=True)
    row = {
        "venue": venue, "market_id": market, "capture_seq": seq,
        "capture_ts_utc": "2026-06-28T01:00:00+00:00",
        "recv_monotonic_ns": ns, "best_bid": bid, "best_ask": ask, "run_id": "r",
    }
    if bid is not None:
        row["bid_px_1"] = bid
        row["bid_sz_1"] = 10.0
    if ask is not None:
        row["ask_px_1"] = ask
        row["ask_sz_1"] = 10.0
    with (d / "2026-06-28.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_edge_both_directions(tmp_path: Path):
    # kalshi: bid 0.40 ask 0.45 ; poly: bid 0.60 ask 0.65
    _book(tmp_path, "kalshi", "K", seq=1, ns=1_000, bid=0.40, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=2_000, bid=0.60, ask=0.65)
    pair = EdgePair("pk", "K", "P", include_in_strategy_metrics=True)
    edges = derive_edges(tmp_path, [pair], fee_round_trip=0.0)
    by_dir = {e["direction"]: e for e in edges}
    # kalshi_yes_poly_no: poly.bid - kalshi.ask = 0.60 - 0.45 = 0.15
    assert by_dir["kalshi_yes_poly_no"]["raw_edge"] == pytest.approx(0.15)
    # kalshi_no_poly_yes: kalshi.bid - poly.ask = 0.40 - 0.65 = -0.25
    assert by_dir["kalshi_no_poly_yes"]["raw_edge"] == pytest.approx(-0.25)


def test_edge_fee_adjustment(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=1_000, bid=0.40, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=2_000, bid=0.60, ask=0.65)
    edges = derive_edges(tmp_path, [EdgePair("pk", "K", "P")], fee_round_trip=0.05)
    e = next(x for x in edges if x["direction"] == "kalshi_yes_poly_no")
    assert e["fee_adj_edge"] == pytest.approx(0.15 - 0.05)


def test_edge_depth_metrics_walk_visible_liquidity(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=1_000, bid=0.40, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=2_000, bid=0.60, ask=0.65)
    edges = derive_edges(
        tmp_path,
        [EdgePair("pk", "K", "P", include_in_strategy_metrics=True)],
        fee_round_trip=0.02,
        target_size=5.0,
    )
    edge = next(row for row in edges if row["direction"] == "kalshi_yes_poly_no")

    assert edge["vwap"] == pytest.approx(0.85)
    assert edge["depth_adj_edge"] == pytest.approx(0.13)
    assert edge["slippage"] == pytest.approx(0.0)
    assert edge["depth_fillable_size"] == pytest.approx(5.0)
    assert edge["depth_liquidity_complete"] is True
    assert edge["max_profitable_size"] == pytest.approx(10.0)


def test_skew_computed_from_monotonic(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=5_000_000, bid=0.4, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=3_000_000, bid=0.6, ask=0.65)
    edges = derive_edges(tmp_path, [EdgePair("pk", "K", "P")], fee_round_trip=0.0)
    # skew = (5e6 - 3e6)/1e6 = 2.0 ms
    assert edges[0]["capture_skew_ms"] == pytest.approx(2.0)


def test_one_sided_book_yields_no_edge(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=1_000, bid=0.93, ask=None)
    _book(tmp_path, "polymarket", "P", seq=2, ns=2_000, bid=None, ask=None)
    edges = derive_edges(tmp_path, [EdgePair("pk", "K", "P")])
    assert edges == []


def test_match_tolerance_excludes_far_apart(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=0, bid=0.4, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=10_000_000_000, bid=0.6, ask=0.65)  # 10s away
    edges = derive_edges(tmp_path, [EdgePair("pk", "K", "P")], match_tolerance_ns=5_000_000_000)
    assert edges == []


def test_benchmark_columns_are_null(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=1_000, bid=0.4, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=2_000, bid=0.6, ask=0.65)
    edges = derive_edges(tmp_path, [EdgePair("pk", "K", "P")])
    assert edges[0]["benchmark_ms"] is None
    assert edges[0]["survived"] is None


def test_write_edges_jsonl_roundtrip(tmp_path: Path):
    _book(tmp_path, "kalshi", "K", seq=1, ns=1_000, bid=0.4, ask=0.45)
    _book(tmp_path, "polymarket", "P", seq=2, ns=2_000, bid=0.6, ask=0.65)
    edges = derive_edges(tmp_path, [EdgePair("pk", "K", "P")])
    out = write_edges_jsonl(tmp_path, edges, date="2026-06-28")
    lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(lines) == len(edges)
    assert lines[0]["pair_key"] == "pk"


def test_load_edge_pairs(tmp_path: Path):
    yaml_path = tmp_path / "pairs.yaml"
    yaml_path.write_text(
        "pairs:\n"
        "  - pair_key: pk1\n"
        "    kalshi_market_id: K1\n"
        "    polymarket_market_id: P1\n"
        "    include_in_strategy_metrics: true\n"
        "  - kalshi_market_id: K2\n"  # no poly id -> skipped
    )
    pairs = load_edge_pairs(yaml_path)
    assert len(pairs) == 1
    assert pairs[0].pair_key == "pk1"
    assert pairs[0].include_in_strategy_metrics is True


def test_load_edge_pairs_uses_yes_token_id(tmp_path: Path):
    yaml_path = tmp_path / "pairs.yaml"
    yaml_path.write_text(
        "pairs:\n"
        "  - pair_key: pk\n"
        "    kalshi_market_id: KX\n"
        "    polymarket_market_id: 0xcond\n"
        "    polymarket_identifiers:\n"
        "      condition_id: 0xcond\n"
        "      yes_token_id: '999'\n"
    )
    pairs = load_edge_pairs(yaml_path)
    # must match the recorded book id (YES token), not the condition id
    assert pairs[0].polymarket_market_id == "999"
