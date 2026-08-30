# Scope: BOT_RUNTIME - M3-T2 Pair registry service over registry v2.1.
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from arbx.pairs.equivalence import (
    VALID_DECISIONS,
    archive_pair,
    record_candidate_decision,
    record_decision,
)
from arbx.pairs.registry import (
    PairSpec,
    RegistryIntegrityError,
    load_pairs,
)
from arbx.ui.envelope import OpError
from arbx.ui.schemas import PairSummary

_APPROVABLE_EQUIVALENCE = {"verified_equivalent", "tail_divergence_documented"}
_EVIDENCE_FILE_ORDER = (
    "rules_snapshot.json",
    "prescreen.json",
    "ai_audit.md",
    "human_checklist.md",
    "dq_summary.json",
    "episodes.json",
    "survival_summary.json",
    "liquidity_profile.json",
    "decision.json",
    "manifest.json",
    "strike_map.html",
    "audit_prompt.txt",
)
_CONNECTIVITY_SCOPES = {"connectivity", "connectivity_only", "connectivity-only"}


class PairRegistryServiceImpl:
    """Read pair summaries and append guarded review decisions.

    This service is intentionally a facade over the existing registry and
    equivalence workflow. It may compose summaries from YAML and evidence-pack
    JSON, but it never mutates equivalence fields. Review writes go through
    ``arbx.pairs.equivalence.record_decision`` (and ``archive_pair`` for the
    archive decision) so the append-only log and sha256 sidecars remain the
    single decision workflow.
    """

    def __init__(
        self,
        approved_path: Path,
        candidates_path: Path,
        archived_path: Path,
        evidence_root: Path,
        extra_candidates_paths: list[Path] | None = None,
    ) -> None:
        self.approved_path = Path(approved_path)
        self.candidates_path = Path(candidates_path)
        self.archived_path = Path(archived_path)
        self.evidence_root = Path(evidence_root)
        # Additional candidate sources (e.g. the imported equivalence sweep).
        # Curated candidates win on pair_key collisions.
        self.extra_candidates_paths = [Path(p) for p in (extra_candidates_paths or [])]
        self.repo_root = self._infer_repo_root(self.approved_path)

    def list_active_pairs(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | OpError:
        """Approved-registry entries, with strategy eligibility left explicit."""
        loaded = self._load_approved()
        if isinstance(loaded, OpError):
            return loaded
        pairs = sorted(loaded, key=self._sort_key)
        return self._page_dict(pairs, cursor, limit)

    def list_pairs_needing_approval(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | OpError:
        """Candidates plus approved entries still waiting on review decisions."""
        approved = self._load_approved()
        if isinstance(approved, OpError):
            return approved
        candidates = self._load_candidates()
        if isinstance(candidates, OpError):
            return candidates

        # Drop candidates that have already been promoted into the approved
        # registry (by pair_key or shared kalshi ticker) so an approved pair
        # leaves the review queue.
        approved_ids = {pair.pair_key for pair in approved}
        approved_ids |= {pair.kalshi_market_id for pair in approved if pair.kalshi_market_id}
        needed: list[PairSpec] = [
            pair for pair in candidates
            if pair.pair_key not in approved_ids and pair.kalshi_market_id not in approved_ids
        ]
        seen = {pair.pair_key for pair in needed}
        for pair in approved:
            if pair.pair_key in seen:
                continue
            if pair.equivalence.status == "unreviewed" or pair.latest_decision == "needs_more_data":
                needed.append(pair)
                seen.add(pair.pair_key)

        needed.sort(key=self._sort_key)
        return self._page_dict(needed, cursor, limit)

    def get_pair_summary(self, pair_key: str) -> PairSummary | OpError:
        found = self._find_any_pair(pair_key)
        if isinstance(found, OpError):
            return found
        pair, source = found
        return self._summary(pair, status_override="archived" if source == "archived" else None)

    def list_archived_pairs(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | OpError:
        """Pairs that were rejected/archived out of the review workflow."""
        loaded = self._load_archived()
        if isinstance(loaded, OpError):
            return loaded
        pairs = sorted(loaded, key=self._sort_key)
        return self._page_dict(pairs, cursor, limit, status_override="archived")

    def review_pair(
        self,
        pair_key: str,
        decision: str,
        reviewer: str,
        notes: str,
        confirm: str,
    ) -> dict[str, Any] | OpError:
        decision = str(decision or "").strip()
        reviewer = str(reviewer or "").strip()
        notes = str(notes or "").strip()
        confirm = str(confirm or "")
        if decision not in VALID_DECISIONS:
            return OpError("invalid_request", f"decision must be one of {sorted(VALID_DECISIONS)}")
        if not reviewer:
            return OpError("invalid_request", "reviewer is required")
        if not notes:
            return OpError("invalid_request", "notes are required")

        found = self._find_approved_pair(pair_key)
        if isinstance(found, OpError):
            candidate = self._find_candidate_pair(pair_key)
            if candidate is not None:
                return self._review_candidate(candidate, decision, reviewer, notes, confirm)
            return found
        pair = found
        expected = f"{decision.upper()} {pair.pair_key}"
        if confirm != expected:
            return OpError(
                "invalid_request",
                "confirmation phrase mismatch",
                {"expected": expected},
            )

        if decision == "approve":
            block_reason = self._approval_block_reason(pair)
            if block_reason:
                return OpError("safety_violation", block_reason)

        try:
            record_decision(self.approved_path, pair.pair_key, decision, notes, reviewer)
            archived_to: str | None = None
            if decision == "archive":
                archived_path = archive_pair(self.approved_path, pair.pair_key, self.archived_path)
                archived_to = self._repo_rel(archived_path)
        except RegistryIntegrityError:
            return OpError("safety_violation", "registry integrity check failed; refusing to write")
        except KeyError:
            return OpError("not_found", "pair was not found in the approved registry")
        except ValueError as exc:
            return OpError("invalid_request", str(exc))

        refreshed = self._find_any_pair(pair.pair_key)
        latest = None
        status = "archived" if decision == "archive" else pair.status
        if not isinstance(refreshed, OpError):
            refreshed_pair, refreshed_source = refreshed
            status = "archived" if refreshed_source == "archived" else refreshed_pair.status
            latest = self._latest_decision(refreshed_pair)
        return {
            "pair_key": pair.pair_key,
            "decision": decision,
            "reviewer": reviewer,
            "latest_decision": latest,
            "status": status,
            "registry_path": self._repo_rel(self.approved_path),
            "archived_path": archived_to,
        }

    def _review_candidate(
        self,
        pair: PairSpec,
        decision: str,
        reviewer: str,
        notes: str,
        confirm: str,
    ) -> dict[str, Any] | OpError:
        """First human decision on an imported candidate: approve promotes it into
        the approved registry; reject/archive files it into the archived registry."""
        expected = f"{decision.upper()} {pair.pair_key}"
        if confirm != expected:
            return OpError("invalid_request", "confirmation phrase mismatch", {"expected": expected})
        if decision == "needs_more_data":
            return OpError(
                "invalid_request",
                "needs_more_data applies to approved-registry pairs; use approve or reject on a candidate",
            )
        if decision == "approve":
            block_reason = self._approval_block_reason(pair)
            if block_reason:
                return OpError("safety_violation", block_reason)
        try:
            destination = record_candidate_decision(
                self.approved_path,
                pair.raw,
                decision,
                notes,
                reviewer,
                archived_path=self.archived_path,
            )
        except RegistryIntegrityError:
            return OpError("safety_violation", "registry integrity check failed; refusing to write")
        except ValueError as exc:
            return OpError("invalid_request", str(exc))

        registry_path = self.approved_path if destination == "approved" else self.archived_path
        status = "approved_for_paper" if destination == "approved" else "archived"
        return {
            "pair_key": pair.pair_key,
            "decision": decision,
            "reviewer": reviewer,
            "latest_decision": {"at": None, "decision": decision, "rationale": notes, "auditor": reviewer},
            "status": status,
            "registry_path": self._repo_rel(registry_path),
            "archived_path": self._repo_rel(self.archived_path) if destination == "archived" else None,
            "promoted": destination == "approved",
        }

    def _load_approved(self) -> list[PairSpec] | OpError:
        try:
            return load_pairs(self.approved_path)
        except RegistryIntegrityError:
            return OpError("safety_violation", "approved registry integrity check failed")

    def _load_candidates(self) -> list[PairSpec] | OpError:
        merged: list[PairSpec] = []
        seen: set[str] = set()
        for path in [self.candidates_path, *self.extra_candidates_paths]:
            if not path.exists():
                continue
            try:
                loaded = load_pairs(path, verify_sha256=False)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                return OpError("invalid_request", f"candidate registry could not be loaded: {exc}")
            for pair in loaded:
                if pair.pair_key in seen:
                    continue
                seen.add(pair.pair_key)
                merged.append(pair)
        return merged

    def _load_archived(self) -> list[PairSpec] | OpError:
        if not self.archived_path.exists():
            return []
        try:
            return load_pairs(self.archived_path)
        except RegistryIntegrityError:
            return OpError("safety_violation", "archived registry integrity check failed")

    def _find_any_pair(self, pair_key: str) -> tuple[PairSpec, str] | OpError:
        for source, pairs in (
            ("approved", self._load_approved()),
            ("candidate", self._load_candidates()),
            ("archived", self._load_archived()),
        ):
            if isinstance(pairs, OpError):
                return pairs
            found = self._find_in(pairs, pair_key)
            if found is not None:
                return found, source
        return OpError("not_found", "pair was not found")

    def _find_approved_pair(self, pair_key: str) -> PairSpec | OpError:
        pairs = self._load_approved()
        if isinstance(pairs, OpError):
            return pairs
        found = self._find_in(pairs, pair_key)
        if found is None:
            return OpError("not_found", "pair was not found in the approved registry")
        return found

    def _find_candidate_pair(self, pair_key: str) -> PairSpec | None:
        candidates = self._load_candidates()
        if isinstance(candidates, OpError):
            return None
        return self._find_in(candidates, pair_key)

    @staticmethod
    def _find_in(pairs: list[PairSpec], pair_key: str) -> PairSpec | None:
        needle = str(pair_key or "")
        for pair in pairs:
            if needle in {pair.pair_key, pair.kalshi_market_id, pair.polymarket_condition_id}:
                return pair
        return None

    def _page_dict(
        self,
        pairs: list[PairSpec],
        cursor: str | None,
        limit: int,
        status_override: str | None = None,
    ) -> dict[str, Any] | OpError:
        limit = self._coerce_limit(limit)
        if isinstance(limit, OpError):
            return limit
        start = 0
        if cursor:
            for index, pair in enumerate(pairs):
                if pair.pair_key == cursor:
                    start = index + 1
                    break
        window = pairs[start:start + limit]
        next_cursor = window[-1].pair_key if start + limit < len(pairs) and window else None
        # List rows are compact: registry fields only. Evidence packs are read
        # exclusively on selection (get_pair_summary), per the ui_data_contract
        # memory rule "load review evidence only after a row is selected".
        return {
            "items": [
                self._summary(pair, include_evidence=False, status_override=status_override).to_dict()
                for pair in window
            ],
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _coerce_limit(limit: int) -> int | OpError:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            return OpError("invalid_request", "limit must be an integer")
        return max(1, min(value, 100))

    def _summary(
        self,
        pair: PairSpec,
        *,
        status_override: str | None = None,
        include_evidence: bool = True,
    ) -> PairSummary:
        pack = self._latest_evidence_pack(pair) if include_evidence else None
        liquidity = self._read_json(pack / "liquidity_profile.json") if pack else None
        edge_behavior = self._edge_behavior(pack) if pack else None
        evidence_links = self._evidence_links(pair, pack) if include_evidence else []
        return PairSummary(
            pair_key=pair.pair_key,
            display_name=pair.display_name or pair.pair_key,
            status=status_override or pair.status,
            kalshi_market_id=pair.kalshi_market_id,
            polymarket_condition_id=pair.polymarket_condition_id,
            polymarket_yes_token_id=pair.polymarket_yes_token_id,
            resolution_structure=pair.taxonomy.resolution_structure,
            grouping_alignment=pair.taxonomy.grouping_alignment,
            date_cutoff_delta_hours=pair.taxonomy.date_cutoff_delta_hours,
            time_to_resolution_days=pair.taxonomy.time_to_resolution_days,
            persistence_cause=pair.taxonomy.persistence_cause,
            equivalence={
                "status": pair.equivalence.status,
                "audited_at": pair.equivalence.audited_at,
                "auditor": pair.equivalence.auditor,
                "notes": pair.equivalence.notes,
                "tail_risks": list(pair.equivalence.tail_risks),
                "rules_snapshot_sha256": pair.equivalence.rules_snapshot_sha256,
            },
            orientation_confirmed=dict(pair.orientation_confirmed),
            liquidity=liquidity if isinstance(liquidity, dict) else None,
            edge_behavior=edge_behavior,
            evidence_links=tuple(evidence_links),
            latest_decision=self._latest_decision(pair),
            simulation_scope=str(pair.raw.get("simulation_scope") or "public_displayed_books"),
            contract_equivalent=pair.equivalence.status,
            include_in_strategy_metrics=pair.include_in_strategy_metrics,
        )

    def _latest_evidence_pack(self, pair: PairSpec) -> Path | None:
        roots = []
        for name in (pair.pair_key, pair.kalshi_market_id):
            root = self.evidence_root / name
            if root.exists() and root.is_dir():
                roots.append(root)
        dated_dirs: list[Path] = []
        for root in roots:
            dated_dirs.extend(path for path in root.iterdir() if path.is_dir())
        if not dated_dirs:
            return None
        # Latest by the dated dir NAME, not the full path: with both a
        # pair_key root and a kalshi_market_id root present, full-path sorting
        # would let the root name outrank the date.
        return max(dated_dirs, key=lambda path: (path.name, path.as_posix()))

    def _evidence_links(self, pair: PairSpec, pack: Path | None) -> list[str]:
        links: list[str] = []
        if pack is not None:
            for name in _EVIDENCE_FILE_ORDER:
                path = pack / name
                if path.exists():
                    links.append(self._repo_rel(path))
            for path in sorted(pack.iterdir()):
                if path.is_file():
                    rel = self._repo_rel(path)
                    if rel not in links:
                        links.append(rel)

        for url in self._venue_urls(pair):
            if url and url not in links:
                links.append(url)
        return links

    def _edge_behavior(self, pack: Path) -> dict[str, Any] | None:
        episodes = self._read_json(pack / "episodes.json")
        survival = self._read_json(pack / "survival_summary.json")
        if not isinstance(episodes, dict) and not isinstance(survival, dict):
            return None
        rows = episodes if isinstance(episodes, dict) else {}
        episode_rows = rows.get("episodes") if isinstance(rows.get("episodes"), list) else []
        buckets = Counter(str(item.get("bucket") or "unknown") for item in episode_rows)
        behavior: dict[str, Any] = {
            "edge_rows": int(rows.get("edge_rows") or 0),
            "episode_count": len(episode_rows),
            "bucket_counts": dict(sorted(buckets.items())),
        }
        if isinstance(survival, dict):
            behavior["survival"] = survival
        return behavior

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _latest_decision(pair: PairSpec) -> dict[str, Any] | None:
        if not pair.decision_log:
            return None
        latest = dict(pair.decision_log[-1])
        return {
            "at": latest.get("at"),
            "decision": latest.get("decision"),
            "rationale": latest.get("rationale"),
            "auditor": latest.get("auditor"),
        }

    @staticmethod
    def _approval_block_reason(pair: PairSpec) -> str | None:
        if pair.equivalence.status not in _APPROVABLE_EQUIVALENCE:
            return (
                "approve is blocked until equivalence.status is "
                "verified_equivalent or tail_divergence_documented"
            )
        if pair.raw.get("contract_equivalent") is False:
            return "approve is blocked for contract_equivalent=false pairs"
        scope = str(pair.raw.get("simulation_scope") or "").strip().lower()
        if scope in _CONNECTIVITY_SCOPES:
            return "approve is blocked for connectivity-only pairs"
        return None

    def _venue_urls(self, pair: PairSpec) -> tuple[str, ...]:
        review = pair.raw.get("review_snapshot") or {}
        kalshi_review = review.get("kalshi") or {}
        poly_review = review.get("polymarket") or {}
        kalshi_url = str(kalshi_review.get("url") or "").strip() or self._kalshi_url(pair)
        poly_url = str(poly_review.get("url") or "").strip() or self._polymarket_url(pair)
        return tuple(url for url in (kalshi_url, poly_url) if url)

    @staticmethod
    def _kalshi_url(pair: PairSpec) -> str:
        market_id = pair.kalshi_market_id
        if not market_id:
            return ""
        event_ticker = str((pair.raw.get("kalshi_identifiers") or {}).get("event_ticker") or "")
        event = event_ticker or market_id.rsplit("-", 1)[0]
        return f"https://kalshi.com/markets/{event}/{market_id}"

    @staticmethod
    def _polymarket_url(pair: PairSpec) -> str:
        ids = pair.raw.get("polymarket_identifiers") or {}
        for key in ("market_slug", "event_slug"):
            slug = str(ids.get(key) or "").strip()
            if slug:
                return f"https://polymarket.com/market/{slug}"
        return ""

    @staticmethod
    def _sort_key(pair: PairSpec) -> tuple[str, str]:
        return ((pair.display_name or pair.pair_key).lower(), pair.pair_key)

    @staticmethod
    def _infer_repo_root(approved_path: Path) -> Path:
        resolved = approved_path.resolve()
        if resolved.parent.name == "configs":
            return resolved.parent.parent
        return resolved.parent

    def _repo_rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            return str(path)
