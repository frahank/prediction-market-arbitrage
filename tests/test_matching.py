from datetime import datetime, timedelta, timezone

import yaml

from arbx.pairs.discovery.matching import (
    MatchingConfig,
    generate_candidate_pairs,
    save_candidate_queue,
)
from arbx.pairs.discovery.models import DiscoveredMarket

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def market(
    venue: str,
    market_id: str,
    *,
    question: str = "Will Kraken complete an IPO by December 31, 2026?",
    rules: str = (
        "Resolves Yes if Kraken completes an initial public offering by "
        "December 31, 2026, confirmed by official reporting."
    ),
    close_time: datetime = datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc),
    yes_label: str = "Yes",
    no_label: str = "No",
):
    return DiscoveredMarket(
        venue=venue,
        market_id=market_id,
        event_id=f"{venue}-event",
        series_id=f"{venue}-series",
        slug=market_id.lower(),
        question=question,
        yes_label=yes_label,
        no_label=no_label,
        close_time=close_time,
        updated_at=NOW,
        rules=rules,
        status="active",
        volume_24h=100,
        volume_total=1000,
        open_interest=500,
        liquidity=500,
        spread=0.02,
        best_yes_bid=0.40,
        best_yes_ask=0.42,
        best_no_bid=0.58,
        best_no_ask=0.60,
        yes_depth=100,
        no_depth=100,
        identifiers={"market_id": market_id},
    )


def test_known_good_pair_becomes_candidate_but_is_never_approved():
    queue = generate_candidate_pairs(
        [market("kalshi", "KXKRAKEN")],
        [
            market(
                "polymarket",
                "POLY-KRAKEN",
                question="Will Kraken complete its IPO by December 31, 2026?",
                rules=(
                    "The market resolves Yes if Kraken completes an initial public "
                    "offering by December 31, 2026, based on official reporting."
                ),
            )
        ],
        generated_at=NOW,
    )

    assert len(queue.candidates) == 1
    candidate = queue.candidates[0]
    assert candidate.orientation == "same"
    assert candidate.status == "candidate"
    assert candidate.auto_approved is False
    assert candidate.confidence >= 0.82
    assert "kraken" in candidate.evidence.shared_entities
    assert "2026" in candidate.evidence.shared_dates
    assert "manual_resolution_review_required" in candidate.warnings


def test_inverted_outcome_labels_are_detected_without_auto_approval():
    kalshi = market(
        "kalshi",
        "KXELECTION",
        question="Who will win the 2026 governor election?",
        rules="Resolves according to the certified winner of the 2026 governor election.",
        yes_label="Republican",
        no_label="Democrat",
    )
    polymarket = market(
        "polymarket",
        "POLY-ELECTION",
        question="Who will win the 2026 governor election?",
        rules="Resolves according to the certified winner of the 2026 governor election.",
        yes_label="Democrat",
        no_label="Republican",
    )

    queue = generate_candidate_pairs([kalshi], [polymarket], generated_at=NOW)

    assert len(queue.candidates) == 1
    candidate = queue.candidates[0]
    assert candidate.orientation == "inverted"
    assert candidate.evidence.orientation_basis == "crossed_outcome_labels"
    assert candidate.auto_approved is False


def test_obvious_different_events_are_rejected():
    queue = generate_candidate_pairs(
        [market("kalshi", "KXKRAKEN")],
        [
            market(
                "polymarket",
                "POLY-WEATHER",
                question="Will New York City exceed 90 degrees in July 2026?",
                rules="Resolves from the official New York City weather station in July 2026.",
            )
        ],
        generated_at=NOW,
    )

    assert queue.candidates == ()
    assert queue.rejected_comparisons == 1


def test_conflicting_dates_are_rejected_even_when_event_text_is_similar():
    queue = generate_candidate_pairs(
        [market("kalshi", "KXKRAKEN")],
        [
            market(
                "polymarket",
                "POLY-KRAKEN-2027",
                question="Will Kraken complete an IPO by December 31, 2027?",
                rules=(
                    "Resolves Yes if Kraken completes an initial public offering "
                    "by December 31, 2027, confirmed by official reporting."
                ),
                close_time=datetime(2028, 1, 1, tzinfo=timezone.utc),
            )
        ],
        config=MatchingConfig(max_close_delta_hours=24_000),
        generated_at=NOW,
    )

    assert queue.candidates == ()


def test_conflicting_locations_are_rejected():
    kalshi = market(
        "kalshi",
        "KXWEATHER-NY",
        question="Will the high temperature in New York exceed 90 degrees in July 2026?",
        rules="Resolves using the official New York weather station for July 2026.",
    )
    polymarket = market(
        "polymarket",
        "POLY-WEATHER-CHI",
        question="Will the high temperature in Chicago exceed 90 degrees in July 2026?",
        rules="Resolves using the official Chicago weather station for July 2026.",
    )

    queue = generate_candidate_pairs(
        [kalshi],
        [polymarket],
        config=MatchingConfig(min_entity_overlap=0.20),
        generated_at=NOW,
    )

    assert queue.candidates == ()


def test_conflicting_numeric_thresholds_are_rejected():
    kalshi = market(
        "kalshi",
        "KXWEATHER-89",
        question="Will New York exceed 89 degrees in July 2026?",
        rules="Resolves using the official New York weather station in July 2026.",
    )
    polymarket = market(
        "polymarket",
        "POLY-WEATHER-90",
        question="Will New York exceed 90 degrees in July 2026?",
        rules="Resolves using the official New York weather station in July 2026.",
    )

    queue = generate_candidate_pairs(
        [kalshi],
        [polymarket],
        config=MatchingConfig(min_entity_overlap=0.20),
        generated_at=NOW,
    )

    assert queue.candidates == ()


def test_close_time_incompatibility_is_rejected():
    queue = generate_candidate_pairs(
        [market("kalshi", "KXKRAKEN")],
        [
            market(
                "polymarket",
                "POLY-KRAKEN",
                close_time=datetime(2027, 2, 1, tzinfo=timezone.utc),
            )
        ],
        generated_at=NOW,
    )

    assert queue.candidates == ()


def test_ambiguous_lookalikes_are_flagged_for_manual_review():
    kalshi = market("kalshi", "KXKRAKEN")
    first = market("polymarket", "POLY-KRAKEN-A")
    second = market(
        "polymarket",
        "POLY-KRAKEN-B",
        close_time=market("polymarket", "unused").close_time + timedelta(hours=1),
    )

    queue = generate_candidate_pairs([kalshi], [first, second], generated_at=NOW)

    assert len(queue.candidates) == 1
    candidate = queue.candidates[0]
    assert "ambiguous_competing_match" in candidate.warnings
    assert any(warning.startswith("competing_market:") for warning in candidate.warnings)
    assert candidate.status == "candidate"


def test_duplicate_polymarket_target_keeps_strongest_candidate_and_warns():
    strongest = market("kalshi", "KXKRAKEN-EXACT")
    weaker = market(
        "kalshi",
        "KXKRAKEN-WEAKER",
        rules=(
            "Resolves Yes when Kraken completes its initial public offering by "
            "December 31, 2026, according to official reporting."
        ),
    )
    polymarket = market("polymarket", "POLY-KRAKEN")

    queue = generate_candidate_pairs([strongest, weaker], [polymarket], generated_at=NOW)

    assert len(queue.candidates) == 1
    assert queue.candidates[0].kalshi_market_id == "KXKRAKEN-EXACT"
    assert "multiple_kalshi_candidates_for_polymarket" in queue.candidates[0].warnings


def test_candidate_queue_round_trips_to_yaml_without_approval(tmp_path):
    queue = generate_candidate_pairs(
        [market("kalshi", "KXKRAKEN")],
        [market("polymarket", "POLY-KRAKEN")],
        generated_at=NOW,
    )
    path = tmp_path / "pairs.candidates.yaml"

    save_candidate_queue(path, queue)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert payload["queue_type"] == "candidate_only"
    assert payload["auto_approval_enabled"] is False
    assert payload["candidates"][0]["status"] == "candidate"
    assert payload["candidates"][0]["auto_approved"] is False
    assert payload["candidates"][0]["orientation"] == "same"
    assert payload["candidates"][0]["warnings"]


def test_committed_candidate_file_contains_no_approved_pairs():
    path = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "configs"
        / "pairs.candidates.yaml"
    )
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert payload["queue_type"] == "candidate_only"
    assert payload["auto_approval_enabled"] is False
    assert all(candidate.get("status") == "candidate" for candidate in payload["candidates"])
    assert all(candidate.get("auto_approved") is False for candidate in payload["candidates"])
