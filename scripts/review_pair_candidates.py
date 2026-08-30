#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Human approval workflow for the actual paper bot.
"""Human-only review workflow for paper-trading market pairs."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.pairs.registry import (
    write_registry_integrity,  # noqa: E402 — import after sys.path setup
)

DEFAULT_CANDIDATES = ROOT / "configs" / "pairs.candidates.yaml"
DEFAULT_APPROVED = ROOT / "configs" / "pairs.approved.yaml"
DEFAULT_KALSHI_DISCOVERY = ROOT / "logs" / "kalshi_discovered_markets.json"
DEFAULT_POLYMARKET_DISCOVERY = ROOT / "logs" / "polymarket_discovered_markets.json"

APPROVE_CONFIRMATION = "APPROVE FOR PAPER"
REJECT_CONFIRMATION = "REJECT PAIR"


class PairApprovalError(ValueError):
    """Raised when a review or approved-pair registry is unsafe or invalid."""


def load_candidate_queue(path: str | Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    if data.get("queue_type") != "candidate_only":
        raise PairApprovalError("candidate queue must have queue_type=candidate_only")
    if data.get("auto_approval_enabled") is not False:
        raise PairApprovalError("candidate queue must explicitly disable auto approval")

    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise PairApprovalError("candidate queue candidates must be a list")

    seen: set[str] = set()
    for candidate in candidates:
        _validate_candidate(candidate)
        pair_key = candidate["pair_key"]
        if pair_key in seen:
            raise PairApprovalError(f"duplicate candidate pair_key: {pair_key}")
        seen.add(pair_key)
    return copy.deepcopy(candidates)


def load_discovery_index(path: str | Path, expected_venue: str) -> dict[str, dict[str, Any]]:
    discovery_path = Path(path)
    if not discovery_path.exists():
        return {}
    try:
        data = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PairApprovalError(f"cannot read discovery file: {discovery_path}") from exc

    if data.get("venue") != expected_venue:
        raise PairApprovalError(
            f"discovery file {discovery_path} is not for venue {expected_venue}"
        )
    markets = data.get("markets")
    if not isinstance(markets, list):
        raise PairApprovalError("discovery markets must be a list")
    return {
        str(market["market_id"]): copy.deepcopy(market)
        for market in markets
        if isinstance(market, dict) and market.get("market_id")
    }


def build_review_context(
    candidate: dict[str, Any],
    kalshi_markets: dict[str, dict[str, Any]],
    polymarket_markets: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    return {
        "kalshi": kalshi_markets.get(str(candidate.get("kalshi_market_id", ""))),
        "polymarket": polymarket_markets.get(str(candidate.get("polymarket_market_id", ""))),
    }


def render_candidate(
    candidate: dict[str, Any],
    context: dict[str, dict[str, Any] | None],
) -> str:
    kalshi = context.get("kalshi") or {}
    polymarket = context.get("polymarket") or {}
    warnings = candidate.get("warnings") or []
    evidence = candidate.get("evidence") or {}

    lines = [
        f"Pair: {candidate.get('pair_key', '<missing>')}",
        f"Orientation: {candidate.get('orientation', '<missing>')}",
        f"Candidate confidence: {candidate.get('confidence', '<missing>')}",
        "",
        "KALSHI",
        f"  Market ID: {candidate.get('kalshi_market_id', '<missing>')}",
        f"  Question: {candidate.get('kalshi_question', '<missing>')}",
        f"  Rules: {_display_value(kalshi.get('rules'))}",
        f"  Close time: {candidate.get('kalshi_close_time', '<missing>')}",
        f"  Liquidity: {_display_value(kalshi.get('liquidity'))}",
        f"  24h volume: {_display_value(kalshi.get('volume_24h'))}",
        f"  YES/NO depth: {_display_value(kalshi.get('yes_depth'))} / "
        f"{_display_value(kalshi.get('no_depth'))}",
        "",
        "POLYMARKET",
        f"  Market ID: {candidate.get('polymarket_market_id', '<missing>')}",
        f"  Question: {candidate.get('polymarket_question', '<missing>')}",
        f"  Rules: {_display_value(polymarket.get('rules'))}",
        f"  Close time: {candidate.get('polymarket_close_time', '<missing>')}",
        f"  Liquidity: {_display_value(polymarket.get('liquidity'))}",
        f"  24h volume: {_display_value(polymarket.get('volume_24h'))}",
        f"  YES/NO depth: {_display_value(polymarket.get('yes_depth'))} / "
        f"{_display_value(polymarket.get('no_depth'))}",
        "",
        "WARNINGS",
        *(f"  - {warning}" for warning in warnings),
        "",
        "MATCH EVIDENCE",
        *(f"  {key}: {value}" for key, value in sorted(evidence.items())),
    ]
    if not warnings:
        lines.insert(lines.index("MATCH EVIDENCE") - 1, "  - none supplied")
    return "\n".join(lines)


def new_approved_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "registry_type": "approved_for_paper_only",
        "live_trading_allowed": False,
        "pairs": [],
        "review_decisions": [],
    }


def load_approved_registry(path: str | Path) -> dict[str, Any]:
    approved_path = Path(path)
    if not approved_path.exists():
        return new_approved_registry()
    data = _load_yaml(approved_path)
    validate_approved_registry(data)
    return copy.deepcopy(data)


def load_approved_pairs(path: str | Path) -> list[dict[str, Any]]:
    """Return only explicitly human-approved paper pairs for future runners."""
    registry = load_approved_registry(path)
    return copy.deepcopy(registry["pairs"])


def validate_approved_registry(data: dict[str, Any]) -> None:
    if data.get("version") != 1:
        raise PairApprovalError("approved registry version must be 1")
    if data.get("registry_type") != "approved_for_paper_only":
        raise PairApprovalError("registry_type must be approved_for_paper_only")
    if data.get("live_trading_allowed") is not False:
        raise PairApprovalError("approved registry must prohibit live trading")

    pairs = data.get("pairs")
    decisions = data.get("review_decisions")
    if not isinstance(pairs, list):
        raise PairApprovalError("approved registry pairs must be a list")
    if not isinstance(decisions, list):
        raise PairApprovalError("approved registry review_decisions must be a list")

    pair_keys: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise PairApprovalError("approved pair records must be mappings")
        _require_text(pair, "pair_key")
        _require_text(pair, "kalshi_market_id")
        _require_text(pair, "polymarket_market_id")
        if pair.get("orientation") not in {"same", "inverted"}:
            raise PairApprovalError("approved pair orientation must be same or inverted")
        if pair.get("status") != "approved_for_paper":
            raise PairApprovalError("runner input may contain only approved_for_paper pairs")
        if pair.get("human_approved") is not True:
            raise PairApprovalError("approved pairs must be explicitly human approved")
        if pair.get("contract_equivalent", True) is False:
            if pair.get("simulation_scope") != "connectivity_only":
                raise PairApprovalError(
                    "non-equivalent pairs must use simulation_scope=connectivity_only"
                )
            if pair.get("include_in_strategy_metrics") is not False:
                raise PairApprovalError(
                    "non-equivalent pairs must be excluded from strategy metrics"
                )
        _require_text(pair, "reviewer")
        if len(_require_text(pair, "reviewer_notes").strip()) < 20:
            raise PairApprovalError("approved pair reviewer_notes must be at least 20 characters")
        _validate_timestamp(_require_text(pair, "approved_at"))
        _validate_review_snapshot(pair.get("review_snapshot"))

        pair_key = pair["pair_key"]
        if pair_key in pair_keys:
            raise PairApprovalError(f"duplicate approved pair_key: {pair_key}")
        pair_keys.add(pair_key)

    for decision in decisions:
        if not isinstance(decision, dict):
            raise PairApprovalError("review decisions must be mappings")
        _require_text(decision, "pair_key")
        if decision.get("decision") not in {"approved", "rejected"}:
            raise PairApprovalError("review decision must be approved or rejected")
        _require_text(decision, "reviewer")
        _require_text(decision, "reviewer_notes")
        _validate_timestamp(_require_text(decision, "reviewed_at"))


def apply_review_decision(
    registry: dict[str, Any],
    candidate: dict[str, Any],
    context: dict[str, dict[str, Any] | None],
    *,
    decision: str,
    reviewer: str,
    notes: str,
    confirmation: str,
    reviewed_at: datetime | None = None,
) -> dict[str, Any]:
    validate_approved_registry(registry)
    _validate_candidate(candidate)

    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approve", "reject"}:
        raise PairApprovalError("decision must be approve or reject")
    if len(reviewer.strip()) < 2:
        raise PairApprovalError("reviewer name is required")

    minimum_notes = 20 if normalized_decision == "approve" else 10
    if len(notes.strip()) < minimum_notes:
        raise PairApprovalError(
            f"{normalized_decision} notes must be at least {minimum_notes} characters"
        )

    expected_confirmation = (
        APPROVE_CONFIRMATION if normalized_decision == "approve" else REJECT_CONFIRMATION
    )
    if confirmation != expected_confirmation:
        raise PairApprovalError(f"confirmation must exactly equal {expected_confirmation!r}")

    timestamp = _as_utc(reviewed_at or datetime.now(timezone.utc)).isoformat()
    pair_key = candidate["pair_key"]
    result = copy.deepcopy(registry)
    result["pairs"] = [pair for pair in result["pairs"] if pair.get("pair_key") != pair_key]
    result["review_decisions"] = [
        item for item in result["review_decisions"] if item.get("pair_key") != pair_key
    ]

    stored_decision = {
        "pair_key": pair_key,
        "decision": "approved" if normalized_decision == "approve" else "rejected",
        "reviewer": reviewer.strip(),
        "reviewer_notes": notes.strip(),
        "reviewed_at": timestamp,
    }
    result["review_decisions"].append(stored_decision)

    if normalized_decision == "approve":
        kalshi = _require_market_context(context.get("kalshi"), "Kalshi")
        polymarket = _require_market_context(context.get("polymarket"), "Polymarket")
        result["pairs"].append(
            {
                "pair_key": pair_key,
                "kalshi_market_id": candidate["kalshi_market_id"],
                "polymarket_market_id": candidate["polymarket_market_id"],
                "kalshi_question": candidate["kalshi_question"],
                "polymarket_question": candidate["polymarket_question"],
                "orientation": candidate["orientation"],
                "status": "approved_for_paper",
                "human_approved": True,
                "reviewer": reviewer.strip(),
                "reviewer_notes": notes.strip(),
                "approved_at": timestamp,
                "candidate_confidence": candidate["confidence"],
                "candidate_warnings": copy.deepcopy(candidate.get("warnings", [])),
                "evidence": copy.deepcopy(candidate.get("evidence", {})),
                "kalshi_close_time": candidate["kalshi_close_time"],
                "polymarket_close_time": candidate["polymarket_close_time"],
                "kalshi_identifiers": copy.deepcopy(candidate.get("kalshi_identifiers", {})),
                "polymarket_identifiers": copy.deepcopy(
                    candidate.get("polymarket_identifiers", {})
                ),
                "review_snapshot": {
                    "kalshi": _review_market_snapshot(kalshi),
                    "polymarket": _review_market_snapshot(polymarket),
                },
            }
        )

    result["pairs"].sort(key=lambda pair: pair["pair_key"])
    result["review_decisions"].sort(key=lambda item: item["pair_key"])
    validate_approved_registry(result)
    return result


def save_approved_registry(path: str | Path, registry: dict[str, Any]) -> None:
    validate_approved_registry(registry)
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(registry, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    write_registry_integrity(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review candidate pairs for public-data paper simulation."
    )
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--approved", type=Path, default=DEFAULT_APPROVED)
    parser.add_argument("--kalshi-discovery", type=Path, default=DEFAULT_KALSHI_DISCOVERY)
    parser.add_argument(
        "--polymarket-discovery",
        type=Path,
        default=DEFAULT_POLYMARKET_DISCOVERY,
    )
    parser.add_argument("--pair-key")
    parser.add_argument("--decision", choices=("approve", "reject"))
    parser.add_argument("--reviewer")
    parser.add_argument("--notes")
    parser.add_argument("--confirmation")
    parser.add_argument("--list", action="store_true", help="Display candidates without deciding.")
    parser.add_argument(
        "--validate-approved",
        action="store_true",
        help="Validate that the approved registry is paper-only runner input.",
    )
    args = parser.parse_args(argv)

    try:
        if args.validate_approved:
            approved = load_approved_pairs(args.approved)
            print(f"Valid paper-only approved registry: {len(approved)} pair(s)")
            return 0

        candidates = load_candidate_queue(args.candidates)
        kalshi_index = load_discovery_index(args.kalshi_discovery, "kalshi")
        polymarket_index = load_discovery_index(args.polymarket_discovery, "polymarket")

        if not candidates:
            print("No candidate pairs are available for human review.")
            return 0

        if args.list:
            for candidate in candidates:
                context = build_review_context(candidate, kalshi_index, polymarket_index)
                print(render_candidate(candidate, context))
                print("\n" + "=" * 72 + "\n")
            return 0

        candidate = _select_candidate(candidates, args.pair_key)
        context = build_review_context(candidate, kalshi_index, polymarket_index)
        print(render_candidate(candidate, context))
        print()

        decision = args.decision or input("Decision (approve/reject): ").strip().lower()
        reviewer = args.reviewer or input("Reviewer name: ").strip()
        notes = args.notes or input("Reviewer notes: ").strip()
        expected = APPROVE_CONFIRMATION if decision == "approve" else REJECT_CONFIRMATION
        confirmation = args.confirmation or input(f"Type {expected!r} to confirm: ")

        registry = load_approved_registry(args.approved)
        updated = apply_review_decision(
            registry,
            candidate,
            context,
            decision=decision,
            reviewer=reviewer,
            notes=notes,
            confirmation=confirmation,
        )
        save_approved_registry(args.approved, updated)
        print(f"Recorded {decision} decision for {candidate['pair_key']}")
        return 0
    except PairApprovalError as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 2


def _select_candidate(
    candidates: list[dict[str, Any]], pair_key: str | None
) -> dict[str, Any]:
    if pair_key is not None:
        for candidate in candidates:
            if candidate["pair_key"] == pair_key:
                return candidate
        raise PairApprovalError(f"candidate pair_key not found: {pair_key}")

    for index, candidate in enumerate(candidates, start=1):
        print(
            f"{index}. {candidate['pair_key']} "
            f"({candidate['orientation']}, confidence={candidate['confidence']})"
        )
    raw_selection = input("Candidate number to review: ").strip()
    try:
        selected_index = int(raw_selection) - 1
    except ValueError as exc:
        raise PairApprovalError("candidate selection must be a number") from exc
    if selected_index < 0 or selected_index >= len(candidates):
        raise PairApprovalError("candidate selection is out of range")
    return candidates[selected_index]


def _validate_candidate(candidate: Any) -> None:
    if not isinstance(candidate, dict):
        raise PairApprovalError("candidate records must be mappings")
    for field in (
        "pair_key",
        "kalshi_market_id",
        "polymarket_market_id",
        "kalshi_question",
        "polymarket_question",
        "kalshi_close_time",
        "polymarket_close_time",
    ):
        _require_text(candidate, field)
    if candidate.get("orientation") not in {"same", "inverted"}:
        raise PairApprovalError("candidate orientation must be same or inverted")
    if candidate.get("status") != "candidate":
        raise PairApprovalError("only candidate-status records may be reviewed")
    if candidate.get("auto_approved") is not False:
        raise PairApprovalError("candidate must explicitly have auto_approved=false")
    try:
        confidence = float(candidate["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PairApprovalError("candidate confidence must be numeric") from exc
    if not 0 <= confidence <= 1:
        raise PairApprovalError("candidate confidence must be between 0 and 1")
    if not isinstance(candidate.get("warnings"), list):
        raise PairApprovalError("candidate warnings must be a list")
    if not isinstance(candidate.get("evidence"), dict):
        raise PairApprovalError("candidate evidence must be a mapping")


def _require_market_context(market: Any, venue: str) -> dict[str, Any]:
    if not isinstance(market, dict):
        raise PairApprovalError(
            f"{venue} discovery record is required before a pair can be approved"
        )
    for field in ("market_id", "question", "rules", "close_time"):
        _require_text(market, field)
    for field in ("liquidity", "volume_24h", "yes_depth", "no_depth"):
        if field not in market or not isinstance(market[field], int | float):
            raise PairApprovalError(f"{venue} discovery record requires numeric {field}")
    return market


def _review_market_snapshot(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": market["market_id"],
        "question": market["question"],
        "rules": market["rules"],
        "close_time": market["close_time"],
        "liquidity": float(market["liquidity"]),
        "volume_24h": float(market["volume_24h"]),
        "yes_depth": float(market["yes_depth"]),
        "no_depth": float(market["no_depth"]),
    }


def _validate_review_snapshot(snapshot: Any) -> None:
    if not isinstance(snapshot, dict):
        raise PairApprovalError("approved pair requires a review_snapshot")
    _require_market_context(snapshot.get("kalshi"), "Kalshi review snapshot")
    _require_market_context(snapshot.get("polymarket"), "Polymarket review snapshot")


def _require_text(mapping: dict[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PairApprovalError(f"{field} is required")
    return value


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PairApprovalError(f"invalid review timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise PairApprovalError("review timestamps must include a timezone")


def _load_yaml(path: str | Path) -> dict[str, Any]:
    input_path = Path(path)
    try:
        data = yaml.safe_load(input_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PairApprovalError(f"cannot read YAML file: {input_path}") from exc
    if not isinstance(data, dict):
        raise PairApprovalError(f"YAML root must be a mapping: {input_path}")
    return data


def _display_value(value: Any) -> str:
    return "not available" if value in (None, "") else str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
