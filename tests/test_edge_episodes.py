from __future__ import annotations

from arbx.analysis.episodes import (
    BUCKET_BASIS,
    BUCKET_TRANSIENT,
    BUCKET_UNUSABLE,
    build_episodes,
    classify_pairs,
    episode_to_dict,
    fee_sensitivity,
    pair_persistence,
    qualifies,
    rank_opportunities,
)


def _row(ts, *, fee=0.03, depth=0.03, size=1000.0, skew=100.0, fresh=True,
         pair="KXFOO|0xtok", direction="kalshi_no_poly_yes"):
    return {
        "pair_key": pair,
        "direction": direction,
        "capture_ts_utc": ts,
        "capture_skew_ms": skew,
        "books_fresh": fresh,
        "fee_adj_edge": fee,
        "depth_adj_edge": depth,
        "max_profitable_size": size,
    }


def test_qualifies_folds_in_fees_spread_skew_and_size():
    assert qualifies(_row("t")) is True
    assert qualifies(_row("t", fee=0.005)) is False       # below 1c after fees
    assert qualifies(_row("t", depth=0.0)) is False        # spread/depth erases it
    assert qualifies(_row("t", skew=400.0)) is False       # too much capture skew
    assert qualifies(_row("t", fresh=False)) is False      # stale book
    assert qualifies(_row("t", size=0.0)) is False         # nothing fillable
    assert qualifies(_row("t", fee=4e-17, depth=4e-17)) is False  # float break-even noise


def test_pair_persistence_flags_basis_suspect():
    # 4 cycles, in-edge on 3 of them => 75% > 25% cutoff => basis-suspect
    rows = [
        _row("2026-06-30T00:00:00+00:00", fee=0.03),
        _row("2026-06-30T00:00:30+00:00", fee=0.03),
        _row("2026-06-30T00:01:00+00:00", fee=0.03),
        _row("2026-06-30T00:01:30+00:00", fee=0.0),  # not in edge
    ]
    prof = pair_persistence(rows)["KXFOO"]
    assert prof.total_cycles == 4
    assert prof.in_edge_cycles == 3
    assert prof.is_basis_suspect is True


def test_build_episodes_splits_on_time_gap():
    rows = [
        _row("2026-06-30T00:00:00+00:00"),
        _row("2026-06-30T00:00:30+00:00"),
        _row("2026-06-30T00:01:00+00:00"),
        # >45s gap -> new episode
        _row("2026-06-30T01:00:00+00:00"),
    ]
    eps = build_episodes(rows)
    assert len(eps) == 2
    long_ep = max(eps, key=lambda e: e.snapshots)
    assert long_ep.snapshots == 3
    assert long_ep.peak_edge == 0.03


def test_rank_opportunities_excludes_basis_and_orders_by_score():
    # basis pair: continuously in edge (all 5 cycles)
    basis = [_row(f"2026-06-30T00:0{i}:00+00:00", pair="KXBASIS|0x", fee=0.02, size=100)
             for i in range(5)]
    # transient pair: mostly quiet (8 non-edge cycles) with a short 2-snapshot
    # dislocation of higher edge + bigger size => below the 25% basis cutoff
    trans = [_row(f"2026-06-30T00:1{i}:00+00:00", pair="KXTRANS|0x", fee=0.0, size=0)
             for i in range(8)]
    trans += [
        _row("2026-06-30T00:20:00+00:00", pair="KXTRANS|0x", fee=0.05, size=50000),
        _row("2026-06-30T00:20:30+00:00", pair="KXTRANS|0x", fee=0.05, size=50000),
    ]
    ranked = rank_opportunities(basis + trans)
    markets = {e.kalshi_market for e in ranked}
    assert "KXBASIS" not in markets     # persistent => excluded
    assert "KXTRANS" in markets
    # including basis surfaces it but flagged
    with_basis = rank_opportunities(basis + trans, include_basis=True)
    assert any(e.kalshi_market == "KXBASIS" and e.is_basis_suspect for e in with_basis)


def test_annotate_survival_is_direction_specific():
    from arbx.analysis.episodes import annotate_survival, build_episodes
    # two episodes on the same market, opposite directions
    rows = (
        [_row(f"2026-06-30T00:0{i}:00+00:00", pair="KXM|0x", direction="kalshi_no_poly_yes") for i in range(2)]
        + [_row(f"2026-06-30T01:0{i}:00+00:00", pair="KXM|0x", direction="kalshi_yes_poly_no") for i in range(2)]
    )
    episodes = build_episodes(rows)
    probes = [
        {"pair_key": "KXM|0x", "direction": "kalshi_no_poly_yes", "public_probe": True,
         "benchmark_ms": 1000, "survived_through_ms": 1000, "survival_tier": "survived_1000ms"},
        {"pair_key": "KXM|0x", "direction": "kalshi_yes_poly_no", "public_probe": True,
         "benchmark_ms": 250, "survived_through_ms": 250, "survival_tier": "survived_250ms"},
    ]
    annotate_survival(episodes, probes)
    by_dir = {ep.direction: ep.best_survival_tier for ep in episodes}
    assert by_dir["kalshi_no_poly_yes"] == "survived_1000ms"
    assert by_dir["kalshi_yes_poly_no"] == "survived_250ms"  # not cross-contaminated


def test_rank_annotates_survival_tier_from_probe_rows():
    trans = [_row(f"2026-06-30T00:1{i}:00+00:00", pair="KXTRANS|0x", fee=0.0, size=0)
             for i in range(8)]
    trans += [
        _row("2026-06-30T00:20:00+00:00", pair="KXTRANS|0x", fee=0.05),
        _row("2026-06-30T00:20:30+00:00", pair="KXTRANS|0x", fee=0.05),
    ]
    probe = {"pair_key": "KXTRANS|0x", "direction": "kalshi_no_poly_yes",
             "public_probe": True, "benchmark_ms": 1000, "survived": True,
             "survived_through_ms": 1000, "survival_tier": "survived_1000ms"}
    ranked = rank_opportunities(trans + [probe])
    top = next(e for e in ranked if e.kalshi_market == "KXTRANS")
    assert top.best_survival_tier == "survived_1000ms"
    assert top.survived_through_ms == 1000


def _quiet(pair, n=8, start_min=1):
    # non-edge filler cycles so a pair stays below the 25% basis cutoff
    return [_row(f"2026-06-30T00:{start_min+i:02d}:00+00:00", pair=pair, fee=0.0, size=0)
            for i in range(n)]


def test_classify_pairs_labels_basis_transient_unusable_with_reasons():
    rows = []
    # basis: in-edge every cycle
    rows += [_row(f"2026-06-30T00:0{i}:00+00:00", pair="KXBASIS|0x", fee=0.02) for i in range(5)]
    # transient: mostly quiet + a 2-snapshot dislocation
    rows += _quiet("KXTRANS|0x", n=8, start_min=10)
    rows += [_row("2026-06-30T00:20:00+00:00", pair="KXTRANS|0x", fee=0.05),
             _row("2026-06-30T00:20:30+00:00", pair="KXTRANS|0x", fee=0.05)]
    # unusable: exists but every row is negative after fees
    rows += [_row(f"2026-06-30T00:3{i}:00+00:00", pair="KXBAD|0x", fee=-0.01, depth=-0.01, size=0) for i in range(4)]

    cls = {c["kalshi_market"]: c for c in classify_pairs(rows)}
    assert cls["KXBASIS"]["bucket"] == BUCKET_BASIS
    assert "persistent basis" in cls["KXBASIS"]["reason"]
    assert cls["KXTRANS"]["bucket"] == BUCKET_TRANSIENT
    assert cls["KXBAD"]["bucket"] == BUCKET_UNUSABLE
    assert "fee" in cls["KXBAD"]["reason"] or "threshold" in cls["KXBAD"]["reason"]


def test_fee_sensitivity_shrinks_as_fee_rises():
    # stored fee_adj=1.5c (at the 2c base) => 3.5c gross gap: clears 1c and 2c
    # net thresholds, but dies at a 3c fee (3.5c-3c = 0.5c < 1c).
    rows = _quiet("KXP|0x", n=15, start_min=1)
    rows += [_row(f"2026-06-30T00:20:{30*i:02d}+00:00", pair="KXP|0x", fee=0.015, depth=0.015)
             for i in range(3)]
    out = fee_sensitivity(rows, fee_levels=(0.01, 0.02, 0.03))
    counts = {round(o["fee"], 3): o["candidate_rows"] for o in out}
    assert counts[0.01] >= counts[0.02] >= counts[0.03]
    assert counts[0.02] == 3 and counts[0.03] == 0
    assert isinstance(out[0]["candidate_pairs"], list)


def test_episode_dict_exposes_research_fields():
    rows = _quiet("KXQ|0x", n=15, start_min=1)
    # 30s-spaced so the three snapshots merge into ONE episode (not blips)
    rows += [_row(f"2026-06-30T00:20:{30*i:02d}+00:00", pair="KXQ|0x", fee=0.03, depth=0.025)
             for i in range(3)]
    top = rank_opportunities(rows)[0]
    d = episode_to_dict(top)
    for key in ("bucket", "reason", "recurrence_count", "median_depth_edge",
                "survival_adjusted_edge", "max_abs_skew_ms", "score"):
        assert key in d
    # un-probed => survival-adjusted edge is null, not a fabricated number
    assert d["survival_adjusted_edge"] is None
    assert d["bucket"] == BUCKET_TRANSIENT
