"""Extended sub-110ms survival rungs: new tiers resolve, old rows still classify."""

from arbx.analysis.survival import (
    STREAMING_PROBE_DELAYS_MS,
    SURVIVAL_TIER_COLORS,
    classify_survival_tiers,
)


def _probe_rows(direction: str, survived_through: set[int], rungs) -> list[dict]:
    rows = [{"direction": direction, "benchmark_ms": 0, "survived": True}]
    rows += [
        {"direction": direction, "benchmark_ms": ms, "survived": ms in survived_through}
        for ms in rungs
    ]
    return rows


def test_new_rungs_produce_new_tiers():
    # Contiguous survival through 100ms, dead at 250ms -> survived_100ms.
    rows = _probe_rows("kalshi_yes_poly_no", {25, 50, 100}, STREAMING_PROBE_DELAYS_MS)
    classify_survival_tiers(rows)
    assert all(r["survival_tier"] == "survived_100ms" for r in rows)
    assert all(r["survived_through_ms"] == 100 for r in rows)

    # Survived only the 25ms rung -> survived_25ms.
    rows = _probe_rows("kalshi_no_poly_yes", {25}, STREAMING_PROBE_DELAYS_MS)
    classify_survival_tiers(rows)
    assert all(r["survival_tier"] == "survived_25ms" for r in rows)

    # Every tier has a pinned UI color.
    assert {"survived_25ms", "survived_100ms"} <= set(SURVIVAL_TIER_COLORS)


def test_old_rows_still_classify():
    old_rungs = (50, 100, 250, 500, 1000)
    # Old-style probe surviving through 1000ms keeps its tier.
    rows = _probe_rows("kalshi_yes_poly_no", set(old_rungs), old_rungs)
    classify_survival_tiers(rows)
    assert all(r["survival_tier"] == "survived_1000ms" for r in rows)

    # Old-style probe that died at the first delayed rung stays gray.
    rows = _probe_rows("kalshi_yes_poly_no", set(), old_rungs)
    classify_survival_tiers(rows)
    assert all(r["survival_tier"] == "expired_lt_250ms" for r in rows)
    assert all(r["survived_through_ms"] == 0 for r in rows)

    # No edge at baseline -> no tier (unchanged semantics).
    rows = [{"direction": "kalshi_yes_poly_no", "benchmark_ms": 0, "survived": False}]
    classify_survival_tiers(rows)
    assert rows[0]["survival_tier"] is None
