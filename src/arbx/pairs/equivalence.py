# Scope: BOT_RUNTIME — P4-T3 pair-equivalence workflow: prescreen + AI audit prompt.
"""Deterministic equivalence pre-screens and the LLM rules-diff audit prompt.

Pair equivalence is the one failure mode that can silently lose unbounded
money (a "hedge" that is a different bet). The workflow is three layers:

1. ``prescreen`` — deterministic R2/R4/R5 checks over the pinned rules
   snapshot. It can only *flag*, never clear: the machine defaults to
   ``needs_human``.
2. ``build_audit_prompt`` — a fixed rules-diff prompt for an LLM auditor,
   anchored on the worked examples from the 2026-07-01 deep audit. The model
   answers with a JSON verdict parsed by ``parse_audit_verdict``.
3. The human checklist (docs/pair_equivalence_checklist.md) — the final,
   signed layer; nothing is tradeable on machine + model output alone.

``record_decision`` / ``archive_pair`` (P4-T5) are the recorded decision
workflow: append-only ``decision_log`` entries and the archive path, both
re-hashing the sha256 sidecar so unaudited manual edits stay detectable.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from arbx.pairs.registry import (
    PairSpec,
    RegistryIntegrityError,
    verify_registry_integrity,
    write_registry_integrity,
)
from arbx.pairs.rules_snapshot import RulesSnapshot

R4_MAX_CUTOFF_DELTA_HOURS = 1.0
R5_LONG_DATED_DAYS = 90.0

_SUBJECTIVE_PATTERNS = (
    "definitively state", "announce", "confirm", "declare", "normalize",
    "credible reporting", "consensus of", "in the judgment", "sole discretion",
)
_GROUPING_PATTERNS = (
    "continent", "confederation", "region", "grouping", "conference",
    "category", "one of the following",
)
_DATE_PATTERNS = ("before ", "by the end of", "on or before", "prior to", "deadline")


@dataclass(frozen=True)
class PrescreenResult:
    pair_key: str
    structure: str          # R2 heuristic: subjective|categorical_grouping|date_cutoff|objective_single_event|needs_human
    flags: list[str]
    score: str              # "flagged" when any deterministic check fired, else "needs_human"

    def to_dict(self) -> dict[str, Any]:
        return {"pair_key": self.pair_key, "structure": self.structure,
                "flags": list(self.flags), "score": self.score}


def _classify_structure(text: str) -> str:
    """R2 keyword heuristic. Subjective dominates (highest basis risk)."""
    lowered = text.lower()
    if not lowered.strip():
        return "needs_human"
    if any(p in lowered for p in _SUBJECTIVE_PATTERNS):
        return "subjective"
    if any(p in lowered for p in _GROUPING_PATTERNS):
        return "categorical_grouping"
    if any(p in lowered for p in _DATE_PATTERNS):
        return "date_cutoff"
    return "objective_single_event"


def _parse_raw_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def prescreen(pair: PairSpec, snapshot: RulesSnapshot,
              *, now: datetime | None = None) -> PrescreenResult:
    """Deterministic R2/R4/R5 checks. R7 (liquidity) needs soak data — not here."""
    now = now or datetime.now(timezone.utc)
    flags: list[str] = []

    combined = "\n".join((snapshot.kalshi_rules_text, snapshot.poly_description))
    structure = _classify_structure(combined)
    if structure == "subjective":
        flags.append("R2: subjective resolution language — high basis risk even "
                     "with identical wording")
    elif structure == "needs_human":
        flags.append("R2: no rules text available to classify")

    # R4 — exact cutoff comparison; snapshot first, registry close times as fallback.
    k_close = snapshot.kalshi_close_time or _parse_raw_ts(pair.raw.get("kalshi_close_time"))
    p_close = snapshot.poly_end_date or _parse_raw_ts(pair.raw.get("polymarket_close_time"))
    if k_close and p_close:
        delta_h = (k_close - p_close).total_seconds() / 3600.0
        if abs(delta_h) > R4_MAX_CUTOFF_DELTA_HOURS:
            flags.append(f"R4: cutoff delta {delta_h:+.1f}h (kalshi − poly) exceeds "
                         f"±{R4_MAX_CUTOFF_DELTA_HOURS:.0f}h — timing-basis window")
    else:
        flags.append("R4: could not compare cutoffs (missing close time on a leg)")

    # R5 — carry filter.
    if k_close is not None:
        ttr_days = (k_close - now).total_seconds() / 86400.0
        if ttr_days > R5_LONG_DATED_DAYS:
            flags.append(f"R5: long-dated ({ttr_days:.0f}d to resolution) — carry "
                         "dominates fee-band edges")

    for w in snapshot.warnings:
        flags.append(f"snapshot: {w}")

    return PrescreenResult(
        pair_key=pair.pair_key,
        structure=structure,
        flags=flags,
        score="flagged" if flags else "needs_human",
    )


_AUDIT_TEMPLATE = """\
You are auditing whether two prediction-market contracts are THE SAME BET. A pair
that resolves differently on any realistic outcome is a "basis" pair — hedging one
leg with the other silently loses the full notional at settlement. Audit strictly
from the rule texts below; do not assume good intent from matching titles.

## Pair under audit
- pair_key: {pair_key}
- Kalshi market: {kalshi_market_id} (close: {kalshi_close})
- Polymarket condition: {condition_id} (end: {poly_end})
- rules_snapshot_sha256: {sha256}

## Kalshi resolution rules (verbatim)
{kalshi_rules}

## Polymarket resolution description (verbatim)
{poly_description}

## Polymarket resolution source
{poly_source}

## Your procedure (do every step, in order)
1. QUOTE the operative resolution criterion of each venue in one sentence each.
2. ENUMERATE the realistic outcomes (list actual entities: countries, candidates,
   teams) and for each mark Kalshi YES/NO and Polymarket YES/NO.
3. GROUPING CHECK (R3): if either venue resolves by a grouping scheme
   (confederation, continent, category), name BOTH schemes and list every member
   of the realistic outcome set whose label differs between schemes.
4. DATE CHECK (R4): compare the exact cutoff timestamps; state the boundary
   window and what event inside it would resolve the venues apart.
5. CLASSIFY per R2: objective_single_event | categorical_grouping | date_cutoff |
   subjective. Subjective language ("definitively states", "announces",
   "normalizes") is high basis risk even when wording matches verbatim.
6. VERDICT.

## Calibration examples (from the 2026-07-01 audited registry)
- KXWCCONTINENT-26-SA vs Poly continent-of-winner=South-America:
  verified_equivalent. CONMEBOL ≈ geographic South America for every realistic
  winner; the only divergence is a geographically-SA CONCACAF member
  (Guyana/Suriname) winning (probability ≈ 0) → documented tail, not basis.
- KXWCCONTINENT-26-EUR vs Poly =Europe: basis. UEFA ≠ geographic Europe —
  transcontinental members (Turkey, Israel, Georgia, ...) count as Asia for
  Polymarket's source, so a realistic-set enumeration shows label mismatches.
- KXPRESNOMD-28-* nominee pairs: verified_equivalent (near-identical wording,
  same close date) — but flag time-to-resolution; equivalence does not make a
  2.5-year carry tradeable.

## Output
End with EXACTLY one fenced JSON block:
```json
{{"status": "verified_equivalent|tail_divergence_documented|basis|unreviewed",
  "tail_risks": ["..."],
  "grouping_check": "one-paragraph finding",
  "date_check": "one-paragraph finding",
  "confidence": 0.0}}
```
"""


def build_audit_prompt(pair: PairSpec, snapshot: RulesSnapshot) -> str:
    return _AUDIT_TEMPLATE.format(
        pair_key=pair.pair_key,
        kalshi_market_id=pair.kalshi_market_id,
        condition_id=pair.polymarket_condition_id,
        kalshi_close=snapshot.kalshi_close_time.isoformat()
        if snapshot.kalshi_close_time else "unknown",
        poly_end=snapshot.poly_end_date.isoformat()
        if snapshot.poly_end_date else "unknown (registry fallback: "
        + str(pair.raw.get("polymarket_close_time", "n/a")) + ")",
        sha256=snapshot.sha256,
        kalshi_rules=snapshot.kalshi_rules_text or "(missing — audit cannot proceed)",
        poly_description=snapshot.poly_description or "(missing — audit cannot proceed)",
        poly_source=snapshot.poly_resolution_source or "(not stated — UMA-resolved)",
    )


_VERDICT_STATUSES = {"verified_equivalent", "tail_divergence_documented",
                     "basis", "unreviewed"}


def parse_audit_verdict(response_text: str) -> dict[str, Any]:
    """Extract and validate the JSON verdict from a model's audit response.

    Raises ``ValueError`` on a missing/malformed block — an unparseable audit
    must never be silently treated as a pass.
    """
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", response_text, flags=re.DOTALL)
    if not blocks:
        raise ValueError("no fenced JSON verdict block in audit response")
    verdict = json.loads(blocks[-1])
    if verdict.get("status") not in _VERDICT_STATUSES:
        raise ValueError(f"invalid verdict status: {verdict.get('status')!r}")
    if not isinstance(verdict.get("tail_risks"), list):
        raise ValueError("tail_risks must be a list")
    confidence = verdict.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(f"confidence must be in [0,1], got {confidence!r}")
    for key in ("grouping_check", "date_check"):
        if not isinstance(verdict.get(key), str) or not verdict[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    return verdict


VALID_DECISIONS = {"approve", "reject", "needs_more_data", "archive"}


def _load_verified_registry(registry_path: Path) -> dict:
    integrity = verify_registry_integrity(registry_path)
    if not integrity.audited:
        raise RegistryIntegrityError(
            f"refusing to modify {registry_path}: integrity status={integrity.status}")
    return yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}


def _write_registry(registry_path: Path, data: dict) -> None:
    registry_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8")
    write_registry_integrity(registry_path)


def _find_entry(data: dict, pair_key: str) -> dict:
    for entry in data.get("pairs", []):
        if entry.get("pair_key") == pair_key or entry.get("kalshi_market_id") == pair_key:
            return entry
    raise KeyError(f"pair {pair_key!r} not found in registry")


def record_decision(registry_path: Path, pair_key: str, decision: str,
                    rationale: str, auditor: str) -> None:
    """Append to the pair's decision_log (append-only) and re-hash the sidecar."""
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")
    if not rationale.strip():
        raise ValueError("a decision requires a non-empty rationale")
    registry_path = Path(registry_path)
    data = _load_verified_registry(registry_path)
    entry = _find_entry(data, pair_key)
    entry.setdefault("decision_log", []).append({
        "at": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "rationale": rationale,
        "auditor": auditor,
    })
    _write_registry(registry_path, data)


def record_candidate_decision(approved_path: Path, entry: dict, decision: str,
                              rationale: str, reviewer: str,
                              archived_path: Path | None = None) -> str:
    """Record the first human decision on a *candidate* (a pair not yet in the
    approved registry) and re-hash the affected sidecar(s).

    ``approve`` promotes the candidate into the approved registry (append-only,
    with an ``approve`` decision-log entry and human sign-off fields), which is
    what finally lets the loader honor ``include_in_strategy_metrics``.
    ``reject``/``archive`` file the candidate into the archived registry with the
    decision so the audit trail is preserved and it leaves the review queue.
    ``needs_more_data`` is rejected here — it belongs to approved-registry pairs.

    Returns the destination: ``"approved"`` or ``"archived"``.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")
    if not rationale.strip():
        raise ValueError("a decision requires a non-empty rationale")
    if decision == "needs_more_data":
        raise ValueError(
            "needs_more_data applies to approved-registry pairs; "
            "use approve or reject on a candidate")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ValueError("a decision requires a reviewer")

    approved_path = Path(approved_path)
    promoted = copy.deepcopy(entry)
    promoted.setdefault("decision_log", []).append({
        "at": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "rationale": rationale,
        "auditor": reviewer,
    })

    if decision == "approve":
        promoted["status"] = "approved_for_paper"
        promoted["human_approved"] = True
        promoted["reviewer"] = reviewer
        promoted["approved_at"] = datetime.now(timezone.utc).isoformat()
        data = _load_verified_registry(approved_path)
        if any(e.get("pair_key") == promoted.get("pair_key") for e in data.get("pairs", [])):
            raise ValueError("pair is already in the approved registry")
        data.setdefault("pairs", []).append(promoted)
        _write_registry(approved_path, data)
        return "approved"

    # reject / archive -> archived registry (never approved)
    archived_path = archived_path or approved_path.with_name("pairs.archived.yaml")
    promoted["status"] = "archived"
    promoted["include_in_strategy_metrics"] = False
    if archived_path.exists():
        archived = yaml.safe_load(archived_path.read_text(encoding="utf-8")) or {}
    else:
        archived = {
            "registry_type": "archived_pairs",
            "notice": "Pairs removed from consideration with their full history; "
                      "never tradeable, kept for audit.",
            "schema_version": 2.1,
            "pairs": [],
        }
    archived.setdefault("pairs", []).append(promoted)
    _write_registry(archived_path, archived)
    return "archived"


def archive_pair(registry_path: Path, pair_key: str,
                 archived_path: Path | None = None) -> Path:
    """Move a pair (full history included) to the archived registry."""
    registry_path = Path(registry_path)
    archived_path = archived_path or registry_path.with_name("pairs.archived.yaml")
    data = _load_verified_registry(registry_path)
    entry = _find_entry(data, pair_key)
    data["pairs"] = [e for e in data.get("pairs", []) if e is not entry]

    if archived_path.exists():
        archived = yaml.safe_load(archived_path.read_text(encoding="utf-8")) or {}
    else:
        archived = {
            "registry_type": "archived_pairs",
            "notice": "Pairs removed from the approved registry with their full "
                      "history; never tradeable, kept for audit.",
            "pairs": [],
        }
    archived.setdefault("pairs", []).append(entry)
    archived["schema_version"] = data.get("schema_version", 1)

    _write_registry(registry_path, data)
    _write_registry(archived_path, archived)
    return archived_path
