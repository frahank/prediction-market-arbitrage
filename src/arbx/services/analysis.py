# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — M2-T4 AnalysisService: one-button soak analysis (v1).
"""Run the full ported analysis battery over selected soaks and return the
pinned ``AnalysisSummary`` v1 (formulas in ``docs/analysis_summary_v1.md``).

Pipeline (one background job at a time, progress persisted after each stage to
``<jobs_dir>/<job_id>.json`` so the read side survives a crash):

    dq → edges → episodes → survival → fees → ev → summary

Everything here is MODEL-BASED until the replay track lands: ``profit_score``
is the episodes research/candidate score, never realized profit;
``chance_of_profit``/``chance_of_loss`` are the pinned v1 formulas over
``configs/modeling.yaml`` knobs; the verdict reuses the Phase-5 EV thresholds
(``basis: "model_v1"``). The honesty caveats (cycle-grained sampling, REST
refetch floor, legacy correction) are attached to every summary.

Legacy soaks: stored edge rows from pre-fix directories are swap artifacts and
are never read — edges are RE-DERIVED from book rows routed through
``arbx.data.legacy.unswap_legacy_book_row``. A legacy soak without book rows
contributes no rows (uncorrectable) and says so in the caveats.
"""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from arbx.analysis.edges import EdgePair, derive_edges, load_edge_pairs
from arbx.analysis.episodes import (
    BUCKET_TRANSIENT,
    classify_pairs,
    fee_sensitivity,
    is_probe_row,
    qualifies,
    rank_opportunities,
)
from arbx.data.legacy import unswap_legacy_book_row
from arbx.data.quality import DataQualityReport, analyze
from arbx.modeling.ev import EVParams, ViabilityThresholds, pair_ev, verdict
from arbx.modeling.executable import load_modeling_config, load_scenarios
from arbx.pairs.registry import PairSpec, load_pairs
from arbx.services.datastore import SoakStoreImpl
from arbx.ui.envelope import OpError
from arbx.ui.schemas import AnalysisSummary

STAGES = ("dq", "edges", "episodes", "survival", "fees", "ev", "summary")
GRAPH_MAX_POINTS = 500
_DIRECTIONS = ("kalshi_yes_poly_no", "kalshi_no_poly_yes")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TIER_MS_RE = re.compile(r"^survived_(\d+)ms$")

CAVEAT_SAMPLING = (
    "sampling resolution is cycle-grained: one snapshot per rotation cycle "
    "answers 'is there an edge now', not sub-cycle survival"
)
CAVEAT_REST_FLOOR = (
    "public REST refetch floor ~110ms: survival below the local round trip is "
    "unobservable and chance figures inherit that floor"
)
CAVEAT_LEGACY = (
    "legacy soak(s) included: edges re-derived from book rows through the "
    "book-semantics corrector (flat-fee derivation)"
)
CAVEAT_PLACEHOLDER_P = (
    "no probed qualifying rows: P(survival) uses the configs/modeling.yaml "
    "fill-probability placeholder tier, not a measurement"
)


def build_graph_payload(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """v1 graph: fee-adjusted edge over time per pair, survival-tier tagged.

    Operator-decided default (2026-07-05). Swapping the visualization is this
    ONE function: return ``{"kind": <new kind>, "payload": {...}}`` and the
    UI keys off ``kind``. Series are downsampled to ≤``GRAPH_MAX_POINTS``
    points per pair by uniform stride.
    """
    series: dict[str, list[tuple[str, float, Any]]] = {}
    for row in rows:
        if is_probe_row(row):
            continue
        pair_key = str(row.get("pair_key") or "")
        stamp = row.get("scanned_at") or row.get("capture_ts_utc")
        fee_adj = row.get("fee_adj_edge")
        if not pair_key or not stamp or not isinstance(fee_adj, (int, float)):
            continue
        series.setdefault(pair_key, []).append(
            (str(stamp), float(fee_adj), row.get("survival_tier"))
        )
    if not series:
        return None
    payload: dict[str, list[list[Any]]] = {}
    for pair_key, points in series.items():
        points.sort(key=lambda point: point[0])
        stride = max(1, math.ceil(len(points) / GRAPH_MAX_POINTS))
        payload[pair_key] = [list(point) for point in points[::stride]]
    return {"kind": "edge_timeline_v1", "payload": {"series": payload}}


def _ids_param(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    raise ValueError("soak_ids must be a list or comma-separated string")


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _row_ts(row: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(row.get("capture_ts_utc"))).timestamp()
    except (TypeError, ValueError):
        return None


def _placeholder_survival_probability(tiers: dict[str, float], latency_ms: float) -> float:
    """Smallest configured tier at/above the assumed latency; 0.0 when none."""
    candidates: list[tuple[int, float]] = []
    for name, prob in tiers.items():
        match = _TIER_MS_RE.match(str(name))
        if match and int(match.group(1)) >= latency_ms:
            candidates.append((int(match.group(1)), float(prob)))
    if not candidates:
        return 0.0
    return min(candidates)[1]


class AnalysisServiceImpl:
    """One-button analysis over selected soaks, one background job at a time."""

    def __init__(
        self,
        soak_store: SoakStoreImpl,
        jobs_dir: Path,
        *,
        registry_path: Path,
        modeling_path: Path,
        stage_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.soak_store = soak_store
        self.jobs_dir = Path(jobs_dir)
        self.registry_path = Path(registry_path)
        self.modeling_path = Path(modeling_path)
        self.stage_hook = stage_hook
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_seq = 0

    # ----- operations -------------------------------------------------------

    def run_full_analysis(self, soak_ids: list[str] | str | None = None) -> dict[str, Any] | OpError:
        try:
            ids = _ids_param(soak_ids)
        except ValueError as exc:
            return OpError("invalid_request", str(exc))
        if not ids:
            return OpError("invalid_request", "soak_ids is required")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return OpError("conflict", "an analysis job is already running")
            resolved: list[tuple[Path, bool]] = []
            for soak_id in ids:
                try:
                    resolved.append(self.soak_store.resolve_for_rows(soak_id))
                except KeyError:
                    return OpError("not_found", f"soak was not found: {soak_id}")
            self._job_seq += 1
            job_id = (
                f"analysis_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{self._job_seq}"
            )
            self._write_status(job_id, state="running", stage="queued", pct=0.0, soak_ids=ids)
            thread = threading.Thread(
                target=self._run_job, args=(job_id, ids, resolved), daemon=True
            )
            self._thread = thread
            thread.start()
        return {"job_id": job_id}

    def get_analysis_status(self, job_id: str) -> dict[str, Any] | OpError:
        job_id = str(job_id or "")
        if not _JOB_ID_RE.match(job_id):
            return OpError("invalid_request", "job_id is invalid")
        path = self.jobs_dir / f"{job_id}.json"
        if not path.exists():
            return OpError("not_found", "analysis job was not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return OpError("internal_error", "analysis job status could not be read")
        return data if isinstance(data, dict) else OpError("internal_error", "corrupt job status")

    # ----- job pipeline ------------------------------------------------------

    def _write_status(self, job_id: str, **fields: Any) -> None:
        path = self.jobs_dir / f"{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job_id,
            "state": fields.get("state", "running"),
            "progress": {"stage": fields.get("stage", ""), "pct": fields.get("pct", 0.0)},
            "summary": fields.get("summary"),
            "error": fields.get("error"),
            "soak_ids": fields.get("soak_ids"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _progress(self, job_id: str, ids: list[str], stage: str) -> None:
        pct = round((STAGES.index(stage) + 1) / len(STAGES) * 100.0, 1)
        self._write_status(job_id, state="running", stage=stage, pct=pct, soak_ids=ids)
        if self.stage_hook is not None:
            self.stage_hook(stage)

    def _run_job(self, job_id: str, ids: list[str], resolved: list[tuple[Path, bool]]) -> None:
        try:
            summary = self._analyze(job_id, ids, resolved)
            self._write_status(
                job_id, state="done", stage="summary", pct=100.0,
                summary=summary.to_dict(), soak_ids=ids,
            )
        except Exception as exc:  # noqa: BLE001 - job boundary: fail the job, never the app
            self._write_status(
                job_id, state="failed", stage="failed", pct=100.0,
                error=f"{type(exc).__name__}: {exc}", soak_ids=ids,
            )

    def _analyze(
        self, job_id: str, ids: list[str], resolved: list[tuple[Path, bool]]
    ) -> AnalysisSummary:
        caveats: list[str] = [CAVEAT_SAMPLING, CAVEAT_REST_FLOOR]

        # dq — corrector-aware: DQ reads raw books directly; crossed_books on a
        # legacy dir is expected and the edge stage handles the correction.
        dq_reports: list[tuple[str, bool, DataQualityReport | None]] = []
        for soak_id, (path, legacy) in zip(ids, resolved):
            report = (
                analyze(path, cache_dir=self.soak_store.cache_dir / "dq")
                if (path / "raw" / "book").exists()
                else None
            )
            dq_reports.append((soak_id, legacy, report))
        with_books = [report for _, _, report in dq_reports if report is not None]
        recorder_ok = all(report.recorder_health_passed() for report in with_books)
        fresh_ok = all(report.freshness_passed() for report in with_books)
        detail_parts = []
        for soak_id, legacy, report in dq_reports:
            if report is None:
                detail_parts.append(f"{soak_id}: no book rows")
            else:
                failing = [key for key, (ok, _) in report.threshold_results().items() if not ok]
                status = "pass" if report.passed() else f"fail ({', '.join(failing)})"
                detail_parts.append(f"{soak_id}: {status}" + (" [legacy]" if legacy else ""))
        dq = {
            "passed": recorder_ok,
            "recorder_health_passed": recorder_ok,
            "freshness_passed": fresh_ok,
            "detail": "; ".join(detail_parts),
        }
        self._progress(job_id, ids, "dq")

        # edges — strategy pairs only; legacy soaks re-derived via the corrector
        edge_pairs = self._edge_pairs()
        rows: list[dict[str, Any]] = []
        legacy_used = False
        for soak_id, (path, legacy) in zip(ids, resolved):
            if legacy:
                if (path / "raw" / "book").exists():
                    legacy_used = True
                    rows.extend(derive_edges(
                        path, list(edge_pairs.values()),
                        row_transform=unswap_legacy_book_row,
                    ))
                else:
                    caveats.append(
                        f"legacy soak {soak_id} has no book rows; its stored edge "
                        "rows are pre-fix swap artifacts and were skipped"
                    )
                continue
            for base in (path / "scan" / "opportunities", path / "raw" / "edge"):
                if base.exists():
                    for file in sorted(base.rglob("*.jsonl")):
                        rows.extend(_iter_jsonl(file))
        if legacy_used:
            caveats.append(CAVEAT_LEGACY)
        strategy_rows = [row for row in rows if row.get("include_in_strategy_metrics")]
        if not strategy_rows:
            caveats.append("no strategy-pair rows in the selected soaks")
        self._progress(job_id, ids, "edges")

        # episodes
        episodes = rank_opportunities(strategy_rows, include_basis=True)
        per_pair = classify_pairs(strategy_rows)
        profit_score = round(sum(
            episode.score for episode in episodes
            if episode.bucket == BUCKET_TRANSIENT and not episode.is_basis_suspect
        ), 2)
        baseline = [row for row in strategy_rows if not is_probe_row(row)]
        qualifying_rows = [row for row in baseline if qualifies(row)]
        self._progress(job_id, ids, "episodes")

        # survival — p50 of survived_through_ms across qualifying rows
        survived_values = sorted(
            float(row["survived_through_ms"]) for row in strategy_rows
            if isinstance(row.get("survived_through_ms"), (int, float)) and qualifies(row)
        )
        min_latency_needed_ms = median(survived_values) if survived_values else None
        self._progress(job_id, ids, "survival")

        # fees — candidate counts at 1c / 2c / real (stored fee_adj_edge)
        sweep = fee_sensitivity(strategy_rows, fee_levels=(0.01, 0.02))
        fee_block = {
            "1c": sweep[0]["candidate_rows"],
            "2c": sweep[1]["candidate_rows"],
            "real": len(qualifying_rows),
        }
        self._progress(job_id, ids, "fees")

        # ev — Phase-5 model + thresholds, clean-concurrency scenario
        config = load_modeling_config(self.modeling_path)
        scenarios = load_scenarios(self.modeling_path)
        scenario = scenarios.get("clean_concurrency") or next(iter(scenarios.values()))
        params = EVParams.from_config(config, scenario)
        thresholds = ViabilityThresholds.from_config(config)
        stamps = [ts for ts in (_row_ts(row) for row in baseline) if ts is not None]
        soak_days = max((max(stamps) - min(stamps)) / 86400.0, 1.0 / 1440.0) if stamps else 0.0
        pair_verdicts: list[dict[str, Any]] = []
        if soak_days > 0 and episodes:
            for spec in self._strategy_specs():
                for direction in _DIRECTIONS:
                    breakdown = pair_ev(
                        episodes, spec, params=params,
                        soak_days=soak_days, direction=direction,
                    )
                    if breakdown.qualifying_episodes:
                        pair_verdicts.append({
                            "pair_key": spec.pair_key,
                            "direction": direction,
                            "verdict": verdict(breakdown, thresholds),
                            "ev_per_day_usd": round(breakdown.ev_per_day_usd, 4),
                        })
        would = self._would_verdict(pair_verdicts, scenario.validity, thresholds)
        self._progress(job_id, ids, "ev")

        # summary — pinned v1 chance formulas
        analysis_cfg = config.get("analysis") or {}
        latency_ms = float(analysis_cfg.get("assumed_reaction_latency_ms", 250))
        probed_qualifying = [
            row for row in strategy_rows
            if qualifies(row) and isinstance(row.get("survived_through_ms"), (int, float))
        ]
        if probed_qualifying:
            p_survival = sum(
                1 for row in probed_qualifying
                if float(row["survived_through_ms"]) >= latency_ms
            ) / len(probed_qualifying)
        else:
            p_survival = _placeholder_survival_probability(
                dict(params.fill_probability_tiers), latency_ms
            )
            caveats.append(CAVEAT_PLACEHOLDER_P)
        qualifying_rate = len(qualifying_rows) / len(baseline) if baseline else 0.0
        chance_of_profit = p_survival * qualifying_rate
        chance_of_loss = 1.0 - chance_of_profit * (1.0 - params.leg_failure_prob)

        summary = AnalysisSummary(
            soak_ids=tuple(ids),
            generated_at=datetime.now(timezone.utc).isoformat(),
            profit_score=profit_score,
            min_latency_needed_ms=min_latency_needed_ms,
            chance_of_profit=round(chance_of_profit, 6),
            chance_of_loss=round(chance_of_loss, 6),
            would_have_made_money_live=would,
            dq=dq,
            fee_sensitivity=fee_block,
            per_pair=tuple(per_pair),
            sample={
                "snapshots": len(baseline),
                "qualifying_rows": len(qualifying_rows),
                "soak_hours": round(soak_days * 24.0, 2),
            },
            graph=build_graph_payload(strategy_rows),
            caveats=tuple(caveats),
        )
        self._progress(job_id, ids, "summary")
        return summary

    # ----- helpers ------------------------------------------------------------

    def _edge_pairs(self) -> dict[str, EdgePair]:
        return {pair.pair_key: pair for pair in load_edge_pairs(self.registry_path)}

    def _strategy_specs(self) -> list[PairSpec]:
        return [
            spec for spec in load_pairs(self.registry_path)
            if spec.include_in_strategy_metrics
        ]

    @staticmethod
    def _would_verdict(
        pair_verdicts: list[dict[str, Any]],
        scenario_validity: str,
        thresholds: ViabilityThresholds,
    ) -> dict[str, Any]:
        """The Phase-5 kill rule over per-(pair, direction) verdicts:
        viable if any viable; marginal if ≥2 marginal; not_viable otherwise;
        insufficient_data with zero qualifying episodes."""
        viable = sorted({v["pair_key"] for v in pair_verdicts if v["verdict"] == "viable"})
        marginal = sorted({v["pair_key"] for v in pair_verdicts if v["verdict"] == "marginal"})
        rationale = [
            f"scenario validity: {scenario_validity}",
            f"thresholds: ev/day >= ${thresholds.min_ev_per_day_usd:.2f}, "
            f"opportunities/week >= {thresholds.min_opportunities_per_week:g}",
        ]
        if not pair_verdicts:
            rationale.insert(0, "zero qualifying episodes across selected soaks")
            label = "insufficient_data"
        elif viable:
            rationale.insert(0, f"viable pairs: {', '.join(viable)}")
            label = "viable"
        elif len(marginal) >= 2:
            rationale.insert(0, f"marginal pairs: {', '.join(marginal)}")
            label = "marginal"
        else:
            rationale.insert(
                0,
                f"no viable pairs; marginal: {', '.join(marginal) if marginal else 'none'}",
            )
            label = "not_viable"
        return {"verdict": label, "rationale": rationale, "basis": "model_v1"}
