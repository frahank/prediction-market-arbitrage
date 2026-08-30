# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Candidate matching for actual public-market records.
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

import yaml

from arbx.pairs.discovery.models import DiscoveredMarket

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTH_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b"
)
_NEGATION_PATTERN = re.compile(r"\b(?:not|no|never|fail|fails|failed|lose|loses|lost)\b")

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "before",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "have",
    "if",
    "in",
    "is",
    "it",
    "market",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "will",
    "with",
    "yes",
    "no",
}
_ENTITY_STOP_WORDS = _STOP_WORDS | {
    "above",
    "below",
    "between",
    "event",
    "happen",
    "happens",
    "higher",
    "lower",
    "more",
    "over",
    "resolve",
    "resolves",
    "than",
    "under",
}
_LOCATION_TERMS = {
    "africa",
    "america",
    "argentina",
    "australia",
    "brazil",
    "california",
    "canada",
    "china",
    "chicago",
    "europe",
    "florida",
    "france",
    "germany",
    "india",
    "iran",
    "israel",
    "japan",
    "london",
    "mexico",
    "miami",
    "new",
    "nyc",
    "russia",
    "texas",
    "uk",
    "ukraine",
    "united",
    "washington",
    "york",
}
_GENERIC_LABELS = {"", "yes", "no", "true", "false"}


@dataclass(frozen=True)
class MatchingConfig:
    min_confidence: float = 0.82
    min_title_similarity: float = 0.72
    min_entity_overlap: float = 0.45
    max_close_delta_hours: float = 72.0
    ambiguity_margin: float = 0.04
    min_rules_similarity: float = 0.10

    def __post_init__(self) -> None:
        for value in (
            self.min_confidence,
            self.min_title_similarity,
            self.min_entity_overlap,
            self.ambiguity_margin,
            self.min_rules_similarity,
        ):
            if not 0 <= value <= 1:
                raise ValueError("matching thresholds must be between 0 and 1")
        if self.max_close_delta_hours < 0:
            raise ValueError("max_close_delta_hours cannot be negative")


@dataclass(frozen=True)
class MatchEvidence:
    title_similarity: float
    entity_overlap: float
    rule_similarity: float
    date_compatibility: float
    location_compatibility: float
    outcome_compatibility: float
    close_delta_hours: float
    shared_entities: tuple[str, ...]
    shared_dates: tuple[str, ...]
    shared_locations: tuple[str, ...]
    orientation_basis: str

    def to_record(self) -> dict:
        record = asdict(self)
        record["shared_entities"] = list(self.shared_entities)
        record["shared_dates"] = list(self.shared_dates)
        record["shared_locations"] = list(self.shared_locations)
        return record


@dataclass(frozen=True)
class CandidatePair:
    pair_key: str
    kalshi_market_id: str
    polymarket_market_id: str
    kalshi_question: str
    polymarket_question: str
    orientation: str
    confidence: float
    status: str
    auto_approved: bool
    warnings: tuple[str, ...]
    evidence: MatchEvidence
    kalshi_close_time: datetime
    polymarket_close_time: datetime
    kalshi_identifiers: dict[str, str]
    polymarket_identifiers: dict[str, str]

    def to_record(self) -> dict:
        return {
            "pair_key": self.pair_key,
            "kalshi_market_id": self.kalshi_market_id,
            "polymarket_market_id": self.polymarket_market_id,
            "kalshi_question": self.kalshi_question,
            "polymarket_question": self.polymarket_question,
            "orientation": self.orientation,
            "confidence": self.confidence,
            "status": self.status,
            "auto_approved": self.auto_approved,
            "warnings": list(self.warnings),
            "evidence": self.evidence.to_record(),
            "kalshi_close_time": _as_utc(self.kalshi_close_time).isoformat(),
            "polymarket_close_time": _as_utc(self.polymarket_close_time).isoformat(),
            "kalshi_identifiers": dict(self.kalshi_identifiers),
            "polymarket_identifiers": dict(self.polymarket_identifiers),
        }


@dataclass(frozen=True)
class CandidateQueue:
    generated_at: datetime
    candidates: tuple[CandidatePair, ...]
    kalshi_markets_seen: int
    polymarket_markets_seen: int
    comparisons_run: int
    rejected_comparisons: int

    def to_record(self) -> dict:
        return {
            "version": 1,
            "generated_at": _as_utc(self.generated_at).isoformat(),
            "queue_type": "candidate_only",
            "auto_approval_enabled": False,
            "kalshi_markets_seen": self.kalshi_markets_seen,
            "polymarket_markets_seen": self.polymarket_markets_seen,
            "comparisons_run": self.comparisons_run,
            "rejected_comparisons": self.rejected_comparisons,
            "candidates": [candidate.to_record() for candidate in self.candidates],
        }


def generate_candidate_pairs(
    kalshi_markets: Iterable[DiscoveredMarket],
    polymarket_markets: Iterable[DiscoveredMarket],
    *,
    config: MatchingConfig | None = None,
    generated_at: datetime | None = None,
) -> CandidateQueue:
    active_config = config or MatchingConfig()
    kalshi = tuple(kalshi_markets)
    polymarket = tuple(polymarket_markets)
    comparisons = 0
    rejected = 0
    selected: list[CandidatePair] = []

    for kalshi_market in kalshi:
        viable = []
        for poly_market in polymarket:
            comparisons += 1
            candidate = _assess_pair(kalshi_market, poly_market, active_config)
            if candidate is None:
                rejected += 1
            else:
                viable.append(candidate)

        viable.sort(key=lambda candidate: candidate.confidence, reverse=True)
        if not viable:
            continue

        best = viable[0]
        warnings = list(best.warnings)
        if len(viable) > 1 and best.confidence - viable[1].confidence <= active_config.ambiguity_margin:
            warnings.extend(
                [
                    "ambiguous_competing_match",
                    f"competing_market:{viable[1].polymarket_market_id}",
                ]
            )
        selected.append(_with_warnings(best, warnings))

    selected = _deduplicate_polymarket_matches(selected)
    selected.sort(key=lambda candidate: (candidate.confidence, candidate.pair_key), reverse=True)
    return CandidateQueue(
        generated_at=generated_at or datetime.now(timezone.utc),
        candidates=tuple(selected),
        kalshi_markets_seen=len(kalshi),
        polymarket_markets_seen=len(polymarket),
        comparisons_run=comparisons,
        rejected_comparisons=rejected,
    )


def save_candidate_queue(path: str | Path, queue: CandidateQueue) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(queue.to_record(), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _assess_pair(
    kalshi: DiscoveredMarket,
    polymarket: DiscoveredMarket,
    config: MatchingConfig,
) -> CandidatePair | None:
    if kalshi.venue != "kalshi" or polymarket.venue != "polymarket":
        return None

    kalshi_title = _normalize(kalshi.question)
    poly_title = _normalize(polymarket.question)
    title_similarity = _text_similarity(kalshi_title, poly_title)
    if title_similarity < config.min_title_similarity:
        return None

    kalshi_entities = _entity_tokens(kalshi.question)
    poly_entities = _entity_tokens(polymarket.question)
    entity_overlap, shared_entities = _overlap(kalshi_entities, poly_entities)
    if entity_overlap < config.min_entity_overlap:
        return None

    kalshi_dates = _date_markers(f"{kalshi.question} {kalshi.rules}")
    poly_dates = _date_markers(f"{polymarket.question} {polymarket.rules}")
    date_compatibility, shared_dates = _compatibility(kalshi_dates, poly_dates)
    if _dates_conflict(
        f"{kalshi.question} {kalshi.rules}",
        f"{polymarket.question} {polymarket.rules}",
    ):
        return None
    if kalshi_dates and poly_dates and not shared_dates:
        return None

    kalshi_locations = _location_tokens(f"{kalshi.question} {kalshi.rules}")
    poly_locations = _location_tokens(f"{polymarket.question} {polymarket.rules}")
    location_compatibility, shared_locations = _compatibility(kalshi_locations, poly_locations)
    if kalshi_locations and poly_locations and not shared_locations:
        return None

    if _conflicting_numbers(kalshi.question, polymarket.question):
        return None

    close_delta_hours = abs(
        (_as_utc(kalshi.close_time) - _as_utc(polymarket.close_time)).total_seconds()
    ) / 3600
    if close_delta_hours > config.max_close_delta_hours:
        return None

    rule_similarity = _rule_similarity(kalshi.rules, polymarket.rules)
    if rule_similarity < config.min_rules_similarity:
        return None

    orientation, outcome_compatibility, orientation_basis, orientation_warnings = _orientation(
        kalshi,
        polymarket,
    )
    if orientation is None:
        return None

    confidence = round(
        (title_similarity * 0.38)
        + (entity_overlap * 0.22)
        + (rule_similarity * 0.18)
        + (date_compatibility * 0.10)
        + (location_compatibility * 0.05)
        + (outcome_compatibility * 0.07),
        6,
    )
    if confidence < config.min_confidence:
        return None

    warnings = list(orientation_warnings)
    if close_delta_hours > 24:
        warnings.append("close_times_differ_over_24h")
    if rule_similarity < 0.35:
        warnings.append("low_rule_similarity")
    if not kalshi_dates or not poly_dates:
        warnings.append("date_not_explicit_on_both_markets")
    if not shared_locations:
        warnings.append("location_not_explicit_on_both_markets")
    warnings.append("manual_resolution_review_required")

    evidence = MatchEvidence(
        title_similarity=title_similarity,
        entity_overlap=entity_overlap,
        rule_similarity=rule_similarity,
        date_compatibility=date_compatibility,
        location_compatibility=location_compatibility,
        outcome_compatibility=outcome_compatibility,
        close_delta_hours=round(close_delta_hours, 3),
        shared_entities=tuple(sorted(shared_entities)),
        shared_dates=tuple(sorted(shared_dates)),
        shared_locations=tuple(sorted(shared_locations)),
        orientation_basis=orientation_basis,
    )
    return CandidatePair(
        pair_key=f"{kalshi.market_id}|{polymarket.market_id}",
        kalshi_market_id=kalshi.market_id,
        polymarket_market_id=polymarket.market_id,
        kalshi_question=kalshi.question,
        polymarket_question=polymarket.question,
        orientation=orientation,
        confidence=confidence,
        status="candidate",
        auto_approved=False,
        warnings=tuple(dict.fromkeys(warnings)),
        evidence=evidence,
        kalshi_close_time=kalshi.close_time,
        polymarket_close_time=polymarket.close_time,
        kalshi_identifiers=dict(kalshi.identifiers),
        polymarket_identifiers=dict(polymarket.identifiers),
    )


def _orientation(
    kalshi: DiscoveredMarket,
    polymarket: DiscoveredMarket,
) -> tuple[str | None, float, str, tuple[str, ...]]:
    labels = (
        _normalize(kalshi.yes_label),
        _normalize(kalshi.no_label),
        _normalize(polymarket.yes_label),
        _normalize(polymarket.no_label),
    )
    labels_are_generic = all(label in _GENERIC_LABELS for label in labels)

    if not labels_are_generic:
        same_score = (
            _text_similarity(labels[0], labels[2])
            + _text_similarity(labels[1], labels[3])
        ) / 2
        inverted_score = (
            _text_similarity(labels[0], labels[3])
            + _text_similarity(labels[1], labels[2])
        ) / 2
        if abs(same_score - inverted_score) < 0.20:
            return None, 0.0, "outcome_labels_ambiguous", ()
        if inverted_score > same_score:
            return "inverted", round(inverted_score, 6), "crossed_outcome_labels", ()
        return "same", round(same_score, 6), "aligned_outcome_labels", ()

    kalshi_negated = bool(_NEGATION_PATTERN.search(_normalize(kalshi.question)))
    poly_negated = bool(_NEGATION_PATTERN.search(_normalize(polymarket.question)))
    if kalshi_negated != poly_negated:
        if _text_similarity(_without_negation(kalshi.question), _without_negation(polymarket.question)) >= 0.85:
            return (
                "inverted",
                0.80,
                "opposite_question_polarity",
                ("orientation_inferred_from_question_negation",),
            )
        return None, 0.0, "question_polarity_conflict", ()

    return (
        "same",
        0.85,
        "aligned_binary_questions",
        ("generic_outcome_labels",),
    )


def _deduplicate_polymarket_matches(candidates: list[CandidatePair]) -> list[CandidatePair]:
    by_poly: dict[str, list[CandidatePair]] = {}
    for candidate in candidates:
        by_poly.setdefault(candidate.polymarket_market_id, []).append(candidate)

    result = []
    for group in by_poly.values():
        group.sort(key=lambda candidate: candidate.confidence, reverse=True)
        winner = group[0]
        if len(group) > 1:
            winner = _with_warnings(
                winner,
                [
                    *winner.warnings,
                    "multiple_kalshi_candidates_for_polymarket",
                    *(
                        f"competing_kalshi_market:{candidate.kalshi_market_id}"
                        for candidate in group[1:]
                    ),
                ],
            )
        result.append(winner)
    return result


def _with_warnings(candidate: CandidatePair, warnings: Iterable[str]) -> CandidatePair:
    return CandidatePair(
        pair_key=candidate.pair_key,
        kalshi_market_id=candidate.kalshi_market_id,
        polymarket_market_id=candidate.polymarket_market_id,
        kalshi_question=candidate.kalshi_question,
        polymarket_question=candidate.polymarket_question,
        orientation=candidate.orientation,
        confidence=candidate.confidence,
        status=candidate.status,
        auto_approved=candidate.auto_approved,
        warnings=tuple(dict.fromkeys(warnings)),
        evidence=candidate.evidence,
        kalshi_close_time=candidate.kalshi_close_time,
        polymarket_close_time=candidate.polymarket_close_time,
        kalshi_identifiers=candidate.kalshi_identifiers,
        polymarket_identifiers=candidate.polymarket_identifiers,
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(_TOKEN_PATTERN.findall(normalized.lower()))


def _without_negation(value: str) -> str:
    return _NEGATION_PATTERN.sub("", _normalize(value))


def _text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    token_score, _ = _overlap(left_tokens, right_tokens)
    return round((sequence * 0.55) + (token_score * 0.45), 6)


def _entity_tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize(value).split()
        if token not in _ENTITY_STOP_WORDS and not token.isdigit() and len(token) > 2
    }


def _date_markers(value: str) -> set[str]:
    normalized = _normalize(value)
    return set(_YEAR_PATTERN.findall(normalized)) | set(_MONTH_PATTERN.findall(normalized))


def _dates_conflict(left: str, right: str) -> bool:
    left_normalized = _normalize(left)
    right_normalized = _normalize(right)
    left_years = set(_YEAR_PATTERN.findall(left_normalized))
    right_years = set(_YEAR_PATTERN.findall(right_normalized))
    left_months = set(_MONTH_PATTERN.findall(left_normalized))
    right_months = set(_MONTH_PATTERN.findall(right_normalized))
    if left_years and right_years and left_years.isdisjoint(right_years):
        return True
    if left_months and right_months and left_months.isdisjoint(right_months):
        return True
    return False


def _location_tokens(value: str) -> set[str]:
    return set(_normalize(value).split()) & _LOCATION_TERMS


def _compatibility(left: set[str], right: set[str]) -> tuple[float, set[str]]:
    if not left and not right:
        return 0.75, set()
    if not left or not right:
        return 0.50, set()
    return _overlap(left, right)


def _overlap(left: set[str], right: set[str]) -> tuple[float, set[str]]:
    if not left and not right:
        return 1.0, set()
    union = left | right
    shared = left & right
    return (len(shared) / len(union) if union else 0.0), shared


def _rule_similarity(left: str, right: str) -> float:
    left_tokens = {
        token for token in _normalize(left).split() if token not in _STOP_WORDS
    }
    right_tokens = {
        token for token in _normalize(right).split() if token not in _STOP_WORDS
    }
    score, _ = _overlap(left_tokens, right_tokens)
    return round(score, 6)


def _conflicting_numbers(left: str, right: str) -> bool:
    left_numbers = set(_NUMBER_PATTERN.findall(_normalize(left))) - set(_YEAR_PATTERN.findall(left))
    right_numbers = set(_NUMBER_PATTERN.findall(_normalize(right))) - set(_YEAR_PATTERN.findall(right))
    return bool(left_numbers and right_numbers and left_numbers != right_numbers)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
