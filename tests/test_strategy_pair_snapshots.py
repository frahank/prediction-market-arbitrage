# Regression tests for v2.4 strategy-pair recorder snapshots.
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from arbx.analysis.edges import EdgePair, derive_edges, load_edge_pairs
from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel
from arbx.data.recorder import load_universe_from_registry, run_recorder

ROOT = Path(__file__).resolve().parents[1]
APPROVED = ROOT / "configs" / "pairs.approved.yaml"


class SnapshotConnector:
    """Offline connector that returns a fresh book for the requested market id."""

    def __init__(self, venue: str, *, yes_bid: float, no_bid: float) -> None:
        self.venue = venue
        self.yes_bid = yes_bid
        self.no_bid = no_bid
        self.requests: list[str] = []

    def fetch_orderbook(self, market_id: str) -> OrderBook:
        self.requests.append(market_id)
        return OrderBook(
            venue=self.venue,
            market_id=market_id,
            yes_levels=(
                OrderBookLevel(self.yes_bid, 100.0),
                OrderBookLevel(max(self.yes_bid - 0.03, 0.01), 50.0),
            ),
            no_levels=(
                OrderBookLevel(self.no_bid, 100.0),
                OrderBookLevel(max(self.no_bid - 0.03, 0.01), 50.0),
            ),
            timestamp=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
            connector_source=ConnectorSource.LIVE_PUBLIC,
            reportable=True,
        )


def _approved_registry() -> dict[str, Any]:
    return yaml.safe_load(APPROVED.read_text(encoding="utf-8"))


def _strategy_pairs() -> list[dict[str, Any]]:
    return [
        pair
        for pair in _approved_registry()["pairs"]
        if pair.get("contract_equivalent") and pair.get("include_in_strategy_metrics")
    ]


def _book_rows(data_dir: Path, venue: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((data_dir / "raw" / "book" / f"venue={venue}").glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return rows


def _latency_rows(data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((data_dir / "raw" / "latency").glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return rows


def test_committed_strategy_pairs_have_clob_token_ids_for_snapshot_capture() -> None:
    strategy = _strategy_pairs()

    # 30 approved in June; four basis pairs and three completed World Cup
    # continent pairs have since been archived.
    assert len(strategy) == 23
    for pair in strategy:
        identifiers = pair["polymarket_identifiers"]
        assert identifiers["yes_token_id"]
        assert identifiers["no_token_id"]
        assert identifiers["yes_token_id"] != pair["polymarket_market_id"]


def test_committed_strategy_pairs_have_kalshi_market_identifiers_for_snapshot_capture() -> None:
    strategy = _strategy_pairs()

    # 30 approved in June; four basis pairs and three completed World Cup
    # continent pairs have since been archived.
    assert len(strategy) == 23
    for pair in strategy:
        assert pair["kalshi_market_id"]
        assert pair["kalshi_identifiers"]["market_ticker"] == pair["kalshi_market_id"]
        assert pair["review_snapshot"]["kalshi"]["market_id"] == pair["kalshi_market_id"]
        assert pair["review_snapshot"]["kalshi"]["question"]
        assert pair["review_snapshot"]["kalshi"]["rules"]


def test_registry_universe_records_strategy_pairs_by_polymarket_yes_token_id() -> None:
    universe = set(load_universe_from_registry(APPROVED))

    for pair in _strategy_pairs():
        identifiers = pair["polymarket_identifiers"]
        assert ("kalshi", pair["kalshi_market_id"]) in universe
        assert ("polymarket", identifiers["yes_token_id"]) in universe
        assert ("polymarket", pair["polymarket_market_id"]) not in universe


def test_strategy_pairs_can_capture_multiple_snapshot_cycles_and_derive_edges(tmp_path: Path) -> None:
    selected = _strategy_pairs()[:3]
    universe: list[tuple[str, str]] = []
    edge_pairs: list[EdgePair] = []
    for pair in selected:
        poly_id = pair["polymarket_identifiers"]["yes_token_id"]
        universe.extend([("kalshi", pair["kalshi_market_id"]), ("polymarket", poly_id)])
        edge_pairs.append(
            EdgePair(
                pair_key=pair["pair_key"],
                kalshi_market_id=pair["kalshi_market_id"],
                polymarket_market_id=poly_id,
                include_in_strategy_metrics=True,
            )
        )

    connectors = {
        "kalshi": SnapshotConnector("kalshi", yes_bid=0.40, no_bid=0.52),
        "polymarket": SnapshotConnector("polymarket", yes_bid=0.58, no_bid=0.38),
    }

    run_recorder(
        universe,
        data_dir=tmp_path,
        run_id="v24-strategy-snapshot-test",
        interval_seconds=0.01,
        connectors=connectors,
        max_cycles=3,
        ntp_offset_ms=4.5,
    )

    kalshi_rows = _book_rows(tmp_path, "kalshi")
    poly_rows = _book_rows(tmp_path, "polymarket")
    assert len(kalshi_rows) == len(selected) * 3
    assert len(poly_rows) == len(selected) * 3
    assert {row["run_id"] for row in kalshi_rows + poly_rows} == {"v24-strategy-snapshot-test"}
    assert {row["ntp_offset_ms"] for row in kalshi_rows + poly_rows} == {4.5}
    assert all(row["connector_source"] == "live_public" for row in kalshi_rows + poly_rows)

    requested = set(connectors["polymarket"].requests)
    assert requested == {pair["polymarket_identifiers"]["yes_token_id"] for pair in selected}
    assert requested.isdisjoint({pair["polymarket_market_id"] for pair in selected})
    assert set(connectors["kalshi"].requests) == {pair["kalshi_market_id"] for pair in selected}

    per_market = Counter((row["venue"], row["market_id"]) for row in kalshi_rows + poly_rows)
    assert set(per_market.values()) == {3}

    seqs = [row["capture_seq"] for row in kalshi_rows + poly_rows]
    assert sorted(seqs) == list(range(1, len(seqs) + 1))

    heartbeats = [row for row in _latency_rows(tmp_path) if row["kind"] == "recorder_heartbeat"]
    assert len(heartbeats) == 3
    assert all(row["universe_size"] == len(universe) for row in heartbeats)

    edges = derive_edges(tmp_path, edge_pairs, fee_round_trip=0.02)
    strategy_edges = [row for row in edges if row["include_in_strategy_metrics"]]
    assert len(strategy_edges) == len(selected) * 3 * 2
    assert {row["benchmark_ms"] for row in strategy_edges} == {None}
    assert {row["survived"] for row in strategy_edges} == {None}
    assert any(row["fee_adj_edge"] > 0 for row in strategy_edges)


def test_committed_edge_pair_loader_matches_strategy_polymarket_snapshot_ids() -> None:
    strategy = _strategy_pairs()
    expected = {
        pair["pair_key"]: pair["polymarket_identifiers"]["yes_token_id"]
        for pair in strategy
    }
    loaded = {
        pair.pair_key: pair.polymarket_market_id
        for pair in load_edge_pairs(APPROVED)
        if pair.include_in_strategy_metrics
    }

    # 30 approved in June; four basis pairs and three completed World Cup
    # continent pairs have since been archived.
    assert len(loaded) == 23
    assert loaded == expected
