"""PolymarketFeeModel against the per-token fee-rate endpoint.

The worked-example assertions mirror docs/fees_polymarket.md: on-chain
formula ``size x (base_fee_bps / 10_000) x min(P, 1 - P)``, makers never
charged, gasless CLOB fills, worst-case fallback on provider failure.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbx.core.models import VenueHealth
from arbx.fees.polymarket import (
    SOURCE_API,
    SOURCE_FALLBACK,
    PolymarketFeeConfig,
    PolymarketFeeModel,
)
from arbx.venues.http import HttpResult

FEES_POLYMARKET_YAML = (
    Path(__file__).resolve().parents[1] / "configs" / "fees_polymarket.yaml"
)


def _result(payload, *, healthy=True, status_code=200) -> HttpResult:
    return HttpResult(
        payload=payload,
        status_code=status_code,
        attempts=1,
        health=VenueHealth(
            venue="polymarket",
            is_healthy=healthy,
            last_checked=datetime.now(timezone.utc),
            reason="ok" if healthy else "degraded",
        ),
    )


class StubProvider:
    """PolymarketFeeProvider stub that counts fee-rate calls."""

    def __init__(self, base_fee=None, *, healthy=True):
        self.base_fee = base_fee
        self.healthy = healthy
        self.fee_rate_calls = 0

    def fetch_market_info_json(self, condition_id: str) -> HttpResult:
        raise AssertionError("fee model must not need /clob-markets")

    def fetch_fee_rate_json(self, token_id: str) -> HttpResult:
        self.fee_rate_calls += 1
        if not self.healthy:
            return _result(None, healthy=False, status_code=503)
        return _result({"base_fee": self.base_fee})


def model_for(provider, *, now=None) -> PolymarketFeeModel:
    config = PolymarketFeeConfig.load(FEES_POLYMARKET_YAML)
    if now is None:
        return PolymarketFeeModel(provider=provider, config=config)
    return PolymarketFeeModel(provider=provider, config=config, now=now)


def test_config_loads_and_pins_worst_case():
    config = PolymarketFeeConfig.load(FEES_POLYMARKET_YAML)
    assert config.source_urls  # every fee number traceable
    assert config.fallback_bps >= 1000  # >= highest observed base_fee, never zero
    assert config.cache_ttl_hours == 6


def test_zero_bps_market_zero_fee():
    fee = model_for(StubProvider(base_fee=0)).taker_fee(
        token_id="tok-geo", price=0.50, size=100
    )
    assert fee.taker_fee_usd == 0.0
    assert fee.total_usd == 0.0
    assert fee.per_unit_usd == 0.0
    assert fee.venue == "polymarket"
    assert fee.source == SOURCE_API  # observed zero, not an assumed zero


def test_nonzero_bps_math():
    # docs/fees_polymarket.md example 1: 100 @ $0.50, 300 bps -> $1.50.
    fee = model_for(StubProvider(base_fee=300)).taker_fee(
        token_id="tok-sports", price=0.50, size=100
    )
    assert fee.taker_fee_usd == pytest.approx(1.50)
    assert fee.total_usd == pytest.approx(1.50)
    assert fee.per_unit_usd == pytest.approx(0.015)
    assert fee.maker_fee_usd == 0.0  # makers are never charged
    assert fee.settlement_fee_usd == 0.0
    assert fee.gas_usd == 0.0  # gasless CLOB fills
    assert fee.source == SOURCE_API

    # Example 2: 100 @ $0.05, 700 bps -> min(P, 1-P) base -> $0.35.
    fee = model_for(StubProvider(base_fee=700)).taker_fee(
        token_id="tok-crypto", price=0.05, size=100
    )
    assert fee.taker_fee_usd == pytest.approx(0.35)

    # min(P, 1-P) is symmetric: $0.95 costs the same as $0.05.
    fee_high = model_for(StubProvider(base_fee=700)).taker_fee(
        token_id="tok-crypto", price=0.95, size=100
    )
    assert fee_high.taker_fee_usd == pytest.approx(0.35)


def test_provider_failure_uses_worstcase_fallback():
    # docs/fees_polymarket.md example 4: 100 @ $0.50, 1000 bps fallback -> $5.00.
    fee = model_for(StubProvider(healthy=False)).taker_fee(
        token_id="tok-down", price=0.50, size=100
    )
    assert fee.source == SOURCE_FALLBACK
    assert fee.taker_fee_usd == pytest.approx(5.00)
    assert fee.taker_fee_usd > 0  # worst-case is never zero

    # Malformed payload and negative values also fall back, never zero.
    for bad in ({"nope": 1}, {"base_fee": "junk"}, {"base_fee": -5}):
        provider = StubProvider(base_fee=0)
        provider.fetch_fee_rate_json = lambda token_id, bad=bad: _result(bad)
        fee = model_for(provider).taker_fee(token_id="tok-bad", price=0.5, size=10)
        assert fee.source == SOURCE_FALLBACK
        assert fee.taker_fee_usd > 0


def test_cache_hits_within_ttl():
    provider = StubProvider(base_fee=300)
    clock = {"t": 0.0}
    model = model_for(provider, now=lambda: clock["t"])

    model.taker_fee(token_id="tok-a", price=0.5, size=10)
    model.taker_fee(token_id="tok-a", price=0.6, size=20)
    assert provider.fee_rate_calls == 1  # second call served from cache

    clock["t"] = 5 * 3600.0  # still inside the 6h TTL
    model.taker_fee(token_id="tok-a", price=0.5, size=10)
    assert provider.fee_rate_calls == 1

    clock["t"] = 7 * 3600.0  # past the TTL -> re-resolve
    model.taker_fee(token_id="tok-a", price=0.5, size=10)
    assert provider.fee_rate_calls == 2

    # Distinct tokens are cached independently.
    model.taker_fee(token_id="tok-b", price=0.5, size=10)
    assert provider.fee_rate_calls == 3


def test_fallback_results_are_not_cached():
    provider = StubProvider(base_fee=300, healthy=False)
    model = model_for(provider)
    assert model.taker_fee(token_id="tok-x", price=0.5, size=1).source == SOURCE_FALLBACK
    provider.healthy = True  # endpoint recovers
    fee = model.taker_fee(token_id="tok-x", price=0.5, size=1)
    assert fee.source == SOURCE_API
    assert provider.fee_rate_calls == 2


def test_input_validation():
    model = model_for(StubProvider(base_fee=0))
    with pytest.raises(ValueError):
        model.taker_fee(token_id="tok", price=0.0, size=10)
    with pytest.raises(ValueError):
        model.taker_fee(token_id="tok", price=1.0, size=10)
    with pytest.raises(ValueError):
        model.taker_fee(token_id="tok", price=0.5, size=0)
    with pytest.raises(ValueError):
        model.taker_fee(token_id="", price=0.5, size=10)


# --- ported regression tests for resolve_polymarket_fee_config -------------
# FeeConfig assertions
# were dropped with the paper engine, the fail-closed semantics are kept.

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)


def resolve_provider_for(market_info, fee_rate):
    from arbx.venues.http import HttpResponse, MockableHttpClient
    from arbx.venues.polymarket_public import PolymarketApiProvider

    def response(url, _headers):
        if "/clob-markets/" in url:
            return HttpResponse(200, market_info)
        if "/fee-rate?" in url:
            return HttpResponse(200, fee_rate)
        raise AssertionError(f"unexpected URL: {url}")

    return PolymarketApiProvider(
        http_client=MockableHttpClient(response_provider=response)
    )


def test_resolve_fee_enabled_market_verifies():
    from arbx.fees.polymarket_source import resolve_polymarket_fee_config

    result = resolve_polymarket_fee_config(
        resolve_provider_for(
            {"tbf": 1000, "mbf": 1000, "fd": {"r": 0.05, "e": 1, "to": True}},
            {"base_fee": 1000},
        ),
        condition_id="0xcondition",
        token_id="123",
        checked_at=NOW,
    )
    assert result.verified is True
    assert result.base_fee_bps == 1000
    assert result.fee_rate == pytest.approx(0.05)
    assert result.taker_only is True


def test_resolve_fee_free_market_verifies():
    from arbx.fees.polymarket_source import resolve_polymarket_fee_config

    result = resolve_polymarket_fee_config(
        resolve_provider_for(
            {"tbf": 0, "mbf": 0, "fd": {"r": 0.0, "e": 1, "to": True}},
            {"base_fee": 0},
        ),
        condition_id="0xfree",
        token_id="456",
        checked_at=NOW,
    )
    assert result.verified is True
    assert result.base_fee_bps == 0
    assert result.fee_rate == 0.0


def test_resolve_conflicting_public_fee_endpoints_fail_closed():
    from arbx.fees.polymarket_source import resolve_polymarket_fee_config

    result = resolve_polymarket_fee_config(
        resolve_provider_for(
            {"tbf": 1000, "fd": {"r": 0.05, "e": 1, "to": True}},
            {"base_fee": 500},
        ),
        condition_id="0xconflict",
        token_id="789",
        checked_at=NOW,
    )
    assert result.verified is False
    assert result.reason == "fee_endpoint_mismatch"
