"""Legacy swapped-row corrector + the crossed-books DQ gate (book_semantics_fix)."""

from arbx.data.legacy import row_is_swapped, unswap_legacy_book_row

LEGACY_ROW = {
    "venue": "kalshi",
    "market_id": "KXTEST",
    # As recorded pre-fix: labels swapped (true market: bid 0.080 / ask 0.081).
    "best_bid": 0.081,
    "best_ask": 0.080,
    "mid": 0.0805,
    "spread": -0.001,
    "bid_px_1": 0.081, "bid_sz_1": 14608.67,
    "bid_px_2": 0.084, "bid_sz_2": 492.32,
    "ask_px_1": 0.080, "ask_sz_1": 8940.76,
    "ask_px_2": 0.073, "ask_sz_2": 144.24,
    "bid_px_3": None, "bid_sz_3": None, "ask_px_3": None, "ask_sz_3": None,
    "freshness_status": "fresh",
    "recv_monotonic_ns": 123,
}


def test_unswap_recovers_venue_quote():
    fixed = unswap_legacy_book_row(LEGACY_ROW)
    assert fixed["best_bid"] == 0.080
    assert fixed["best_ask"] == 0.081
    assert fixed["spread"] > 0
    # Ladders swap wholesale, sizes included.
    assert fixed["bid_px_1"] == 0.080 and fixed["bid_sz_1"] == 8940.76
    assert fixed["ask_px_1"] == 0.081 and fixed["ask_sz_1"] == 14608.67
    assert fixed["ask_px_2"] == 0.084 and fixed["ask_sz_2"] == 492.32
    assert fixed["legacy_book_fix"] is True
    # Label-independent fields untouched.
    assert fixed["mid"] == LEGACY_ROW["mid"]
    assert fixed["freshness_status"] == "fresh"
    assert fixed["recv_monotonic_ns"] == 123
    # Original row not mutated.
    assert LEGACY_ROW["best_bid"] == 0.081


def test_unswap_is_idempotent_and_skips_correct_rows():
    fixed_once = unswap_legacy_book_row(LEGACY_ROW)
    fixed_twice = unswap_legacy_book_row(fixed_once)
    assert fixed_twice == fixed_once

    correct = {"best_bid": 0.40, "best_ask": 0.42, "bid_px_1": 0.40, "ask_px_1": 0.42}
    passed = unswap_legacy_book_row(correct)
    assert passed["best_bid"] == 0.40 and passed["ask_px_1"] == 0.42
    assert not row_is_swapped(passed)


def test_dq_crossed_books_gate(tmp_path):
    import json

    from arbx.data.quality import analyze

    book_dir = tmp_path / "raw" / "book" / "venue=kalshi"
    book_dir.mkdir(parents=True)
    rows = [
        {"venue": "kalshi", "market_id": "KXTEST", "capture_seq": 1,
         "capture_ts_utc": "2026-07-02T00:00:00+00:00", "best_bid": 0.081,
         "best_ask": 0.080, "freshness_status": "fresh"},
        {"venue": "kalshi", "market_id": "KXTEST", "capture_seq": 2,
         "capture_ts_utc": "2026-07-02T00:00:30+00:00", "best_bid": 0.080,
         "best_ask": 0.081, "freshness_status": "fresh"},
    ]
    (book_dir / "2026-07-02.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    report = analyze(tmp_path)
    ok, detail = report.threshold_results()["crossed_books"]
    assert not ok, "one swapped row must fail the crossed-books gate"
    assert "1 rows" in detail
    assert not report.recorder_health_passed()
