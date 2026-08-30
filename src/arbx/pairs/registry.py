# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Detects unaudited manual changes to paper-pair registries.
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RegistryIntegrity:
    path: str
    current_hash: str
    recorded_hash: str | None
    status: str

    @property
    def audited(self) -> bool:
        return self.status == "verified"

    def to_record(self) -> dict[str, str | bool | None]:
        return {
            "path": self.path,
            "current_hash": self.current_hash,
            "recorded_hash": self.recorded_hash,
            "status": self.status,
            "audited": self.audited,
        }


def registry_hash_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.sha256")


def calculate_registry_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_registry_integrity(path: Path) -> RegistryIntegrity:
    current = calculate_registry_hash(path) if path.exists() else ""
    sidecar = registry_hash_path(path)
    if not sidecar.exists():
        return RegistryIntegrity(str(path), current, None, "untracked")
    recorded = sidecar.read_text(encoding="utf-8").strip()
    status = "verified" if recorded == current else "unreviewed_external_change"
    return RegistryIntegrity(str(path), current, recorded, status)


def write_registry_integrity(path: Path) -> Path:
    sidecar = registry_hash_path(path)
    sidecar.write_text(calculate_registry_hash(path) + "\n", encoding="utf-8")
    return sidecar


class RegistryIntegrityError(RuntimeError):
    """Raised when a registry file fails its recorded sha256 integrity check."""


@dataclass(frozen=True)
class PairTaxonomy:
    """R1–R7 taxonomy (schema v2). Defaults are the honest v1 unknowns."""

    resolution_structure: str = "unknown"   # R2: objective_single_event|categorical_grouping|date_cutoff|subjective|unknown
    grouping_alignment: str = "n/a"         # R3: clean|dirty|n/a
    date_cutoff_delta_hours: float | None = None   # R4: kalshi_close − poly_close
    time_to_resolution_days: float | None = None   # R5
    persistence_cause: str = "unknown"      # R1: contract_basis|price_stickiness|none|unknown


@dataclass(frozen=True)
class EquivalenceRecord:
    status: str = "unreviewed"  # verified_equivalent|tail_divergence_documented|basis|unreviewed
    audited_at: str | None = None
    auditor: str | None = None
    rules_snapshot_sha256: str | None = None
    notes: str = ""
    tail_risks: tuple[str, ...] = ()


_CONTINENT_LABELS = {
    "AFR": "Africa (CAF)",
    "EUR": "Europe (UEFA)",
    "NA": "North America (CONCACAF)",
    "SA": "South America (CONMEBOL)",
}


def generate_display_name(entry: dict) -> str:
    """Return a non-empty human label for a registry entry.

    Existing ``display_name`` values win so operators can hand-edit YAML labels.
    Missing values are generated from the clearest human text in the registry,
    then from known Kalshi ticker families, and finally from ``pair_key``.
    """
    existing = str(entry.get("display_name") or "").strip()
    if existing:
        return existing

    question = _best_question(entry)
    label = _label_from_question(question)
    if label:
        return label

    kalshi_id = _kalshi_market_id(entry)
    label = _label_from_ticker(kalshi_id)
    if label:
        return label

    return str(entry.get("pair_key") or kalshi_id or "").strip()


def _best_question(entry: dict) -> str:
    review = entry.get("review_snapshot") or {}
    kalshi_review = review.get("kalshi") or {}
    for value in (
        entry.get("display_title"),
        entry.get("title"),
        entry.get("kalshi_question"),
        kalshi_review.get("question"),
        entry.get("polymarket_question"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _kalshi_market_id(entry: dict) -> str:
    return str(
        entry.get("kalshi_market_id")
        or (entry.get("kalshi_identifiers") or {}).get("market_ticker")
        or ""
    )


def _label_from_question(question: str) -> str:
    compact = " ".join(question.replace("—", "-").split())
    if not compact:
        return ""

    nominee = re.match(
        r"Will (?P<name>.+?) be the Democratic Presidential nominee in (?P<year>\d{4})\?",
        compact,
    )
    if nominee:
        return f"{nominee['year']} Democratic nominee - {nominee['name']}"

    world_cup = re.match(
        r"Will (?P<outcome>.+?) win the (?P<year>\d{4}) (?:Men's |FIFA )?World Cup\?",
        compact,
    )
    if world_cup:
        return f"World Cup {world_cup['year']} - {world_cup['outcome']} wins"

    f1 = re.match(r"Will (?P<name>.+?) win the F1 Drivers Championship\?", compact)
    if f1:
        return f"F1 Drivers Championship - {f1['name']}"

    if compact == "Will the U.S. confirm that aliens exist before 2027?":
        return "U.S. confirms aliens before 2027"
    if compact == "Will the US confirm that aliens exist before 2027?":
        return "U.S. confirms aliens before 2027"
    if compact == "Will Israel and Lebanon normalize relations before Jan 1, 2027?":
        return "Israel-Lebanon relations before Jan 1, 2027"
    if compact == "Israel and Lebanon normalize relations before 2027?":
        return "Israel-Lebanon relations before 2027"
    if compact == "Will OpenAI announce the creation of AGI? - Before 2027":
        return "OpenAI AGI before 2027"

    return compact[:-1] if compact.endswith("?") else compact


def _label_from_ticker(kalshi_id: str) -> str:
    parts = kalshi_id.split("-")
    if len(parts) >= 3 and parts[0] == "KXWCCONTINENT":
        year = f"20{parts[1]}" if len(parts[1]) == 2 else parts[1]
        return f"World Cup {year} - {_CONTINENT_LABELS.get(parts[2], parts[2])} wins"
    if len(parts) >= 3 and parts[0] == "KXF1":
        year = f"20{parts[1]}" if len(parts[1]) == 2 else parts[1]
        return f"F1 {year} Drivers Championship - {parts[2]}"
    if len(parts) >= 3 and parts[0] == "KXPRESNOMD":
        year = f"20{parts[1]}" if len(parts[1]) == 2 else parts[1]
        return f"{year} Democratic nominee - {parts[2]}"
    if kalshi_id == "KXALIENS-27":
        return "U.S. confirms aliens before 2027"
    if kalshi_id.startswith("KXABRAHAMSA-27-JAN01-LEB"):
        return "Israel-Lebanon relations before Jan 1, 2027"
    if kalshi_id == "OAIAGI-26":
        return "OpenAI AGI before 2027"
    return ""


# Which pairs the scanner may record. Deliberately separate from
# ``include_in_strategy_metrics``: observing a public order book is not the same
# decision as counting a pair toward strategy results, so a pair rejected as
# untradeable remains legitimate to capture and re-analyse.
SCANNABLE_STATUSES = frozenset({"approved_for_paper"})


@dataclass(frozen=True)
class PairSpec:
    pair_key: str
    kalshi_market_id: str
    polymarket_condition_id: str
    polymarket_yes_token_id: str
    polymarket_no_token_id: str
    orientation: str
    status: str
    include_in_strategy_metrics: bool
    raw: dict
    taxonomy: PairTaxonomy = PairTaxonomy()
    equivalence: EquivalenceRecord = EquivalenceRecord()
    orientation_confirmed: dict = field(default_factory=dict)
    decision_log: tuple[dict, ...] = ()
    display_name: str = ""

    @property
    def latest_decision(self) -> str | None:
        return str(self.decision_log[-1]["decision"]) if self.decision_log else None


def load_pairs(path: Path, *, verify_sha256: bool = True) -> list[PairSpec]:
    """Load a pair registry YAML into typed ``PairSpec`` records.

    When ``verify_sha256`` is true (the default, deny-by-default), the file's
    recorded ``.sha256`` sidecar must match its current hash or
    :class:`RegistryIntegrityError` is raised — an unaudited or missing hash is
    treated as a failure. Field names follow the YAML keys: ``kalshi_market_id``
    and ``polymarket_identifiers.{condition_id,yes_token_id,no_token_id}``.

    Both schema versions load: v1 entries get ``equivalence.status="unreviewed"``
    and unknown/null taxonomy; v2 (``schema_version: 2``) entries carry the
    R1–R7 ``taxonomy``, ``equivalence``, ``orientation_confirmed``, and
    ``decision_log`` blocks.

    Deny-by-default strategy gate (P4-T5): ``include_in_strategy_metrics`` is
    forced ``False`` unless the pair's equivalence status is
    ``verified_equivalent``/``tail_divergence_documented`` AND its latest
    decision-log entry is ``approve``. The YAML flag alone is never sufficient.

    That gate governs *reported strategy metrics*, not data collection. It rides
    on each edge row written by ``arbx.scanner.edges_writer`` and is filtered on
    by the analysis and heatmap layers, so a pair excluded here can still be
    captured and re-analysed — it simply never counts toward a strategy result.
    Whether the scanner may record a pair at all is a separate question answered
    by ``SCANNABLE_STATUSES``.
    """
    path = Path(path)
    if verify_sha256:
        integrity = verify_registry_integrity(path)
        if not integrity.audited:
            raise RegistryIntegrityError(
                f"registry integrity check failed for {path}: status={integrity.status}"
            )

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("pairs") or data.get("candidates") or []

    specs: list[PairSpec] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        poly = entry.get("polymarket_identifiers") or {}
        kalshi_id = _kalshi_market_id(entry)
        tax = entry.get("taxonomy") or {}
        eq = entry.get("equivalence") or {}
        log = tuple(entry.get("decision_log") or ())
        equivalence_ok = str(eq.get("status") or "unreviewed") in {
            "verified_equivalent", "tail_divergence_documented"}
        approved = bool(log) and str(log[-1].get("decision")) == "approve"
        specs.append(
            PairSpec(
                pair_key=str(entry.get("pair_key", "")),
                kalshi_market_id=str(kalshi_id or ""),
                polymarket_condition_id=str(poly.get("condition_id", "")),
                polymarket_yes_token_id=str(poly.get("yes_token_id", "")),
                polymarket_no_token_id=str(poly.get("no_token_id", "")),
                orientation=str(entry.get("orientation", "")),
                status=str(entry.get("status", "")),
                include_in_strategy_metrics=(
                    bool(entry.get("include_in_strategy_metrics", False))
                    and equivalence_ok and approved
                ),
                raw=entry,
                taxonomy=PairTaxonomy(
                    resolution_structure=str(tax.get("resolution_structure") or "unknown"),
                    grouping_alignment=str(tax.get("grouping_alignment") or "n/a"),
                    date_cutoff_delta_hours=tax.get("date_cutoff_delta_hours"),
                    time_to_resolution_days=tax.get("time_to_resolution_days"),
                    persistence_cause=str(tax.get("persistence_cause") or "unknown"),
                ),
                equivalence=EquivalenceRecord(
                    status=str(eq.get("status") or "unreviewed"),
                    audited_at=eq.get("audited_at"),
                    auditor=eq.get("auditor"),
                    rules_snapshot_sha256=eq.get("rules_snapshot_sha256"),
                    notes=str(eq.get("notes") or ""),
                    tail_risks=tuple(str(t) for t in (eq.get("tail_risks") or ())),
                ),
                orientation_confirmed=dict(entry.get("orientation_confirmed") or {}),
                decision_log=log,
                display_name=generate_display_name(entry),
            )
        )
    return specs
