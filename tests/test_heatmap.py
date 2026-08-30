# Scope: TEST — Unit tests for Phase 5 heatmap builders.
from __future__ import annotations

from arbx.analysis.heatmap import (
    edge_heatmap,
    latency_heatmap,
    render_html_page,
    survival_heatmap,
)


def _book(venue, hour, ms):
    return {"venue": venue, "fetch_elapsed_ms": ms,
            "capture_ts_utc": f"2026-06-28T{hour:02d}:00:00+00:00"}


def test_latency_heatmap_median(tmp_path=None):
    rows = [_book("kalshi", 1, 100), _book("kalshi", 1, 200), _book("polymarket", 2, 50)]
    hm = latency_heatmap(rows)
    assert not hm.empty
    assert hm.cells[("kalshi", "01")] == 150.0  # median of 100,200
    assert hm.cells[("polymarket", "02")] == 50.0


def test_latency_heatmap_empty():
    assert latency_heatmap([]).empty
    assert "no data" in latency_heatmap([]).render_text()


def _edge(pair, hour, fee_adj, *, strat=False, bm=None, survived=None):
    return {
        "pair_key": pair, "fee_adj_edge": fee_adj,
        "capture_ts_utc": f"2026-06-28T{hour:02d}:00:00+00:00",
        "include_in_strategy_metrics": strat, "benchmark_ms": bm, "survived": survived,
    }


def test_edge_heatmap_positive_rate():
    rows = [_edge("pk", 1, 0.1), _edge("pk", 1, -0.1), _edge("pk", 1, 0.2)]
    hm = edge_heatmap(rows)
    # 2 of 3 positive at hour 01
    assert hm.cells[("pk", "01")] == 100.0 * 2 / 3


def test_edge_heatmap_strategy_only_filters():
    rows = [_edge("conn", 1, 0.1, strat=False), _edge("real", 1, 0.1, strat=True)]
    hm = edge_heatmap(rows, strategy_only=True)
    assert hm.rows == ["real"]


def test_survival_heatmap_empty_without_benchmarks():
    rows = [_edge("pk", 1, 0.1)]  # no benchmark_ms/survived
    assert survival_heatmap(rows).empty


def test_survival_heatmap_with_benchmarks():
    rows = [
        _edge("pk", 1, 0.1, bm=100, survived=True),
        _edge("pk", 1, 0.1, bm=100, survived=False),
        _edge("pk", 1, 0.1, bm=250, survived=True),
    ]
    hm = survival_heatmap(rows)
    assert hm.cells[("pk", "100")] == 50.0
    assert hm.cells[("pk", "250")] == 100.0


def test_render_html_contains_table():
    rows = [_book("kalshi", 1, 100)]
    page = render_html_page([latency_heatmap(rows)])
    assert "<table" in page
    assert "kalshi" in page
