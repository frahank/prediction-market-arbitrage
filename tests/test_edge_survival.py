from __future__ import annotations

from datetime import datetime, timezone

from arbx.analysis.edges import EdgePair
from arbx.analysis.survival import (
    SURVIVAL_TIER_COLORS,
    classify_survival_tiers,
    run_public_edge_survival_probe,
)
from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel, VenueHealth


def _rung(direction: str, benchmark_ms: int, survived: bool) -> dict:
    return {"direction": direction, "benchmark_ms": benchmark_ms, "survived": survived}


def test_classify_survival_tiers_buckets_by_contiguous_lifetime():
    # green: survives every rung through 1000ms
    green = [_rung("d", ms, True) for ms in (0, 50, 100, 250, 500, 1000)]
    # yellow: survives 500ms then dies at 1000ms
    yellow = [_rung("d", ms, ms <= 500) for ms in (0, 50, 100, 250, 500, 1000)]
    # orange: survives 250ms then dies at 500ms
    orange = [_rung("d", ms, ms <= 250) for ms in (0, 50, 100, 250, 500, 1000)]
    # blue: survives 100ms then dies (resolvable at the streaming rungs)
    blue = [_rung("d", ms, ms <= 100) for ms in (0, 50, 100, 250, 500, 1000)]
    # gray: an edge existed at observation but died at the first delayed rung
    gray = [_rung("d", ms, ms == 0) for ms in (0, 50, 100, 250, 500, 1000)]
    # none: no edge even at the baseline observation
    none = [_rung("d", ms, False) for ms in (0, 50, 100, 250, 500, 1000)]

    assert classify_survival_tiers(green)[0]["survival_tier"] == "survived_1000ms"
    assert classify_survival_tiers(yellow)[0]["survival_tier"] == "survived_500ms"
    assert classify_survival_tiers(orange)[0]["survival_tier"] == "survived_250ms"
    assert classify_survival_tiers(blue)[0]["survival_tier"] == "survived_100ms"
    assert classify_survival_tiers(gray)[0]["survival_tier"] == "expired_lt_250ms"
    assert classify_survival_tiers(none)[0]["survival_tier"] is None
    assert classify_survival_tiers(green)[0]["survived_through_ms"] == 1000
    # a late blip after an earlier failure must NOT count (contiguous only)
    broken = [_rung("d", 0, True), _rung("d", 250, False), _rung("d", 1000, True)]
    assert classify_survival_tiers(broken)[0]["survived_through_ms"] == 0


def test_classify_survival_tiers_is_per_direction():
    rows = (
        [_rung("kalshi_yes_poly_no", ms, True) for ms in (0, 250, 1000)]
        + [_rung("kalshi_no_poly_yes", ms, ms == 0) for ms in (0, 250, 1000)]
    )
    classify_survival_tiers(rows)
    by_dir = {r["direction"]: r["survival_tier"] for r in rows}
    assert by_dir["kalshi_yes_poly_no"] == "survived_1000ms"
    assert by_dir["kalshi_no_poly_yes"] == "expired_lt_250ms"


def test_survival_tier_color_mapping_is_complete():
    assert SURVIVAL_TIER_COLORS["survived_250ms"] == "orange"
    assert SURVIVAL_TIER_COLORS["survived_500ms"] == "yellow"
    assert SURVIVAL_TIER_COLORS["survived_1000ms"] == "green"


class FakeConnector:
    def __init__(self, venue: str, bid: float, ask: float) -> None:
        self.venue = venue
        self.connector_source = ConnectorSource.LIVE_PUBLIC
        self.bid = bid
        self.ask = ask

    def fetch_orderbook(self, market_id: str) -> OrderBook:
        return OrderBook(
            venue=self.venue,
            market_id=market_id,
            yes_levels=(OrderBookLevel(self.bid, 10.0),),
            no_levels=(OrderBookLevel(1.0 - self.ask, 10.0),),
            timestamp=datetime.now(timezone.utc),
            connector_source=ConnectorSource.LIVE_PUBLIC,
        )

    def fetch_market_metadata(self, market_id: str):
        return None

    def health(self) -> VenueHealth:
        return VenueHealth(self.venue, True, datetime.now(timezone.utc), "ok", self.connector_source)


def test_public_edge_survival_probe_emits_delay_rows_without_orders():
    pair = EdgePair("pk", "K", "P", include_in_strategy_metrics=True)
    connectors = {
        "kalshi": FakeConnector("kalshi", bid=0.40, ask=0.45),
        "polymarket": FakeConnector("polymarket", bid=0.60, ask=0.65),
    }

    rows = run_public_edge_survival_probe(
        pair,
        connectors,
        fee_round_trip=0.02,
        target_size=2.0,
        delays_ms=(50, 100),
        run_id="probe-test",
        sleeper=lambda _: None,
    )

    assert {row["benchmark_ms"] for row in rows} == {0, 50, 100}
    assert all(row["public_probe"] is True for row in rows)
    assert all(row["probe_source"] == "public_refetch" for row in rows)
    assert all("depth_adj_edge" in row for row in rows)
    assert any(row["survived"] is True for row in rows)
    assert all(row["run_id"] == "probe-test" for row in rows)
    # every probe row carries the progressive survival-tier fields
    assert all("survival_tier" in row and "survived_through_ms" in row for row in rows)
