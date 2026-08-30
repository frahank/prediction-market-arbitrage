# FeeEngine composition and its wiring into derive_edges.
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbx.analysis.edges import EdgePair, derive_edges
from arbx.core.models import ConnectorSource, OrderBook, OrderBookLevel
from arbx.data.recorder import book_to_observation
from arbx.fees.engine import FeeEngine
from arbx.fees.kalshi import KalshiFeeModel, KalshiFeeSchedule
from arbx.fees.polymarket import PolymarketFeeConfig, PolymarketFeeModel
from arbx.fees.types import FeeBreakdown

CONFIGS = Path(__file__).resolve().parents[1] / "configs"

PAIR = EdgePair(
    pair_key="KXTEST-26-A/12345",
    kalshi_market_id="KXTEST-26-A",
    polymarket_market_id="12345",
    include_in_strategy_metrics=True,
)


def _book(venue: str, market_id: str, yes_bid: float, no_bid: float) -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id=market_id,
        yes_levels=(OrderBookLevel(yes_bid, 100.0),),
        no_levels=(OrderBookLevel(no_bid, 100.0),),
        timestamp=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        connector_source=ConnectorSource.LIVE_PUBLIC,
        reportable=True,
    )


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    """A tiny recorder-shaped data dir: one paired kalshi/polymarket capture."""
    rows = [
        book_to_observation(
            _book("kalshi", PAIR.kalshi_market_id, yes_bid=0.60, no_bid=0.35),
            capture_seq=1,
            recv_monotonic_ns=1_000_000,
            capture_ts_utc=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
            fetch_elapsed_ms=10.0,
            run_id="fee-engine-test",
        ),
        book_to_observation(
            _book("polymarket", PAIR.polymarket_market_id, yes_bid=0.70, no_bid=0.28),
            capture_seq=2,
            recv_monotonic_ns=2_000_000,
            capture_ts_utc=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
            fetch_elapsed_ms=10.0,
            run_id="fee-engine-test",
        ),
    ]
    out = tmp_path / "raw" / "book"
    out.mkdir(parents=True)
    with (out / "2026-07-01.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")
    return tmp_path


class StubFeeEngine:
    """Duck-typed FeeEngine returning fixed per-unit leg fees."""

    fee_model_version = "stub:v0"

    def __init__(self, kalshi_per_unit: float = 0.03, poly_per_unit: float = 0.02):
        self.kalshi_per_unit = kalshi_per_unit
        self.poly_per_unit = poly_per_unit

    def leg_fee(self, venue, *, market_id, price, size, liquidity) -> FeeBreakdown:
        per_unit = self.kalshi_per_unit if venue == "kalshi" else self.poly_per_unit
        return FeeBreakdown(
            venue=venue,
            taker_fee_usd=per_unit * size,
            maker_fee_usd=0.0,
            settlement_fee_usd=0.0,
            gas_usd=0.0,
            total_usd=per_unit * size,
            per_unit_usd=per_unit,
            source="stub",
        )


def test_flat_path_unchanged(fixture_dir: Path):
    without_kwarg = derive_edges(fixture_dir, [PAIR])
    with_none = derive_edges(fixture_dir, [PAIR], fee_engine=None)
    assert without_kwarg == with_none
    assert without_kwarg, "fixture should yield edge rows"
    for row in without_kwarg:
        assert "fee_model_version" not in row  # flat path stays byte-identical
        assert "fee_usd_at_target" not in row
        assert row["fee_adj_edge"] == pytest.approx(row["raw_edge"] - 0.02)


def test_real_fees_change_fee_adj_edge(fixture_dir: Path):
    stub = StubFeeEngine(kalshi_per_unit=0.03, poly_per_unit=0.02)
    rows = derive_edges(fixture_dir, [PAIR], fee_engine=stub)
    assert rows
    for row in rows:
        # round trip = 0.03 (kalshi) + 0.02 (poly) per unit, replacing flat 0.02
        assert row["fee_adj_edge"] == pytest.approx(row["raw_edge"] - 0.05)
        assert row["fee_usd_at_target"] == pytest.approx(0.05 * row["target_size"])

    # max_profitable_size walks depth with the real marginal fee: raise the
    # stub fee above every chunk's gross edge and the profitable size hits 0.
    flat_rows = derive_edges(fixture_dir, [PAIR])
    assert any(row["max_profitable_size"] > 0 for row in flat_rows)
    prohibitive = StubFeeEngine(kalshi_per_unit=0.60, poly_per_unit=0.60)
    for row in derive_edges(fixture_dir, [PAIR], fee_engine=prohibitive):
        assert row["max_profitable_size"] == 0.0


def test_rows_carry_fee_model_version(fixture_dir: Path):
    rows = derive_edges(fixture_dir, [PAIR], fee_engine=StubFeeEngine())
    assert rows
    for row in rows:
        assert row["fee_model_version"] == "stub:v0"
        assert isinstance(row["fee_usd_at_target"], float)


# --- FeeEngine unit tests ---------------------------------------------------


class _StubPolyProvider:
    def __init__(self, base_fee=300, *, healthy=True):
        self.base_fee = base_fee
        self.healthy = healthy

    def fetch_market_info_json(self, condition_id):
        raise AssertionError("engine must not need /clob-markets")

    def fetch_fee_rate_json(self, token_id):
        from arbx.core.models import VenueHealth
        from arbx.venues.http import HttpResult

        return HttpResult(
            payload={"base_fee": self.base_fee} if self.healthy else None,
            status_code=200 if self.healthy else 503,
            attempts=1,
            health=VenueHealth(
                venue="polymarket",
                is_healthy=self.healthy,
                last_checked=datetime.now(timezone.utc),
                reason="ok" if self.healthy else "degraded",
            ),
        )


def engine_with(provider) -> FeeEngine:
    return FeeEngine(
        kalshi=KalshiFeeModel(KalshiFeeSchedule.load(CONFIGS / "fees_kalshi.yaml")),
        polymarket=PolymarketFeeModel(
            provider=provider,
            config=PolymarketFeeConfig.load(CONFIGS / "fees_polymarket.yaml"),
        ),
    )


def test_engine_round_trip_is_sum_of_taker_legs():
    class Pair:
        kalshi_market_id = "KXTEST-26-A"
        polymarket_yes_token_id = "yes-token"
        polymarket_no_token_id = "no-token"

    engine = engine_with(_StubPolyProvider(base_fee=300))
    rt = engine.round_trip_cost(
        Pair(), "kalshi_yes_poly_no", kalshi_price=0.50, polymarket_price=0.50, size=100
    )
    # kalshi: ceil(0.07 x 100 x 0.25) = $1.75; poly: 100 x 0.03 x 0.5 = $1.50
    assert rt.taker_fee_usd == pytest.approx(3.25)
    assert rt.total_usd == pytest.approx(3.25)
    assert rt.per_unit_usd == pytest.approx(0.0325)
    assert rt.maker_fee_usd == 0.0
    assert "formula:v1" in rt.source and "api:fee_rate_bps" in rt.source
    assert engine.fee_model_version == "kalshi:v1+poly:api"

    with pytest.raises(ValueError):
        engine.round_trip_cost(
            Pair(), "sideways", kalshi_price=0.5, polymarket_price=0.5, size=1
        )


def test_engine_unknown_market_is_worstcase_never_zero():
    engine = engine_with(_StubPolyProvider(healthy=False))
    # Polymarket provider down -> configured fallback bps, flagged as fallback.
    fee = engine.leg_fee(
        "polymarket", market_id="tok", price=0.5, size=100, liquidity="taker"
    )
    assert fee.source == "flat_fallback"
    assert fee.total_usd > 0

    # Missing token id -> same worst case.
    fee = engine.leg_fee(
        "polymarket", market_id="", price=0.5, size=100, liquidity="taker"
    )
    assert fee.source == "flat_fallback"
    assert fee.total_usd > 0

    # Unknown kalshi series -> general formula (worst case), never zero.
    fee = engine.leg_fee(
        "kalshi", market_id="ZZUNKNOWN-26-X", price=0.5, size=100, liquidity="taker"
    )
    assert fee.total_usd == pytest.approx(1.75)

    with pytest.raises(ValueError):
        engine.leg_fee("nyse", market_id="x", price=0.5, size=1, liquidity="taker")
    with pytest.raises(ValueError):
        engine.leg_fee("kalshi", market_id="x", price=0.5, size=1, liquidity="hold")


def test_engine_clamps_extreme_prices_and_maker_poly_is_free():
    engine = engine_with(_StubPolyProvider(base_fee=300))
    # Price 1.0 would blow up the formulas; the clamp keeps a nonzero fee.
    fee = engine.leg_fee(
        "kalshi", market_id="KXTEST-26-A", price=1.0, size=100, liquidity="taker"
    )
    assert fee.total_usd > 0
    fee = engine.leg_fee(
        "polymarket", market_id="tok", price=0.0, size=100, liquidity="taker"
    )
    assert fee.total_usd > 0

    maker = engine.leg_fee(
        "polymarket", market_id="tok", price=0.5, size=100, liquidity="maker"
    )
    assert maker.total_usd == 0.0  # makers are never charged
