# Scope: BOT_RUNTIME — UI/backend service protocols and operation seam.
"""Typed service contracts for the named UI operations.

The browser calls named operations only. The concrete service implementations
are constructed in ``scripts/run_ui.py`` (the composition root) and injected via
``arbx.ui.app.ServiceRegistry``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from arbx.ui.envelope import OpError
from arbx.ui.schemas import (
    AnalysisSummary,
    PairSummary,
    StandardizedDataRow,
    StandardizedEdgeRow,
)

ReadMethod = Literal["GET"]
WriteMethod = Literal["POST"]
HttpMethod = ReadMethod | WriteMethod


@dataclass(frozen=True, slots=True)
class SeamOperation:
    module: str
    service_attr: str
    service_object: str
    name: str
    request: str
    response: str
    method: HttpMethod


SEAM_OPERATIONS: tuple[SeamOperation, ...] = (
    SeamOperation("Shared", "facade", "facade", "get_app_status", "none", "mode/killswitch/version", "GET"),
    SeamOperation("Documents", "doc_store", "DocStore", "list_docs", "none", "doc list", "GET"),
    SeamOperation("Documents", "doc_store", "DocStore", "read_doc", "path", "markdown+html", "GET"),
    SeamOperation("Documents", "notes_store", "NotesStore", "list_notes", "none", "note list", "GET"),
    SeamOperation("Documents", "notes_store", "NotesStore", "read_note", "name", "note markdown+version", "GET"),
    SeamOperation(
        "Documents",
        "notes_store",
        "NotesStore",
        "save_note",
        "name, markdown, expected_version",
        "version",
        "POST",
    ),
    SeamOperation("Data", "data_service", "DataService", "list_soaks", "cursor, limit, edges_only", "{items: SoakFileMeta[], next_cursor}", "GET"),
    SeamOperation("Data", "data_service", "DataService", "get_soak", "soak_id", "meta+DQ", "GET"),
    SeamOperation(
        "Data",
        "data_service",
        "DataService",
        "list_soak_rows",
        "soak_id, kind, cursor, limit",
        "StandardizedEdgeRow[]/StandardizedDataRow[]",
        "GET",
    ),
    SeamOperation(
        "Pairs",
        "pair_registry_service",
        "PairRegistryService",
        "list_active_pairs",
        "cursor, limit",
        "{items: PairSummary[], next_cursor}",
        "GET",
    ),
    SeamOperation(
        "Pairs",
        "pair_registry_service",
        "PairRegistryService",
        "list_pairs_needing_approval",
        "cursor, limit",
        "{items: PairSummary[], next_cursor}",
        "GET",
    ),
    SeamOperation("Pairs", "pair_registry_service", "PairRegistryService", "get_pair_summary", "pair_key", "PairSummary", "GET"),
    SeamOperation(
        "Pairs",
        "pair_registry_service",
        "PairRegistryService",
        "list_archived_pairs",
        "cursor, limit",
        "{items: PairSummary[], next_cursor}",
        "GET",
    ),
    SeamOperation(
        "Pairs",
        "pair_registry_service",
        "PairRegistryService",
        "review_pair",
        "pair_key, decision, reviewer, notes, confirm",
        "decision record",
        "POST",
    ),
    SeamOperation(
        "Paper",
        "scanner_controller",
        "ScannerController",
        "start_scanner",
        "pairs, record, edges_only, batch, tick",
        "run info+cadence",
        "POST",
    ),
    SeamOperation("Paper", "scanner_controller", "ScannerController", "stop_scanner", "none", "stop result", "POST"),
    SeamOperation("Paper", "scanner_controller", "ScannerController", "get_scanner_status", "none", "scanner status", "GET"),
    SeamOperation("Paper", "analysis_service", "AnalysisService", "run_full_analysis", "soak_ids", "job_id", "POST"),
    SeamOperation(
        "Paper",
        "analysis_service",
        "AnalysisService",
        "get_analysis_status",
        "job_id",
        "progress/AnalysisSummary",
        "GET",
    ),
    SeamOperation("Paper", "test_suite_runner", "TestSuiteRunner", "run_test_suite", "none", "job_id", "POST"),
    SeamOperation(
        "Paper",
        "test_suite_runner",
        "TestSuiteRunner",
        "get_test_suite_result",
        "job_id",
        "TestSuiteResult",
        "GET",
    ),
    SeamOperation("Paper", "test_suite_runner", "TestSuiteRunner", "get_test_run_detail", "path", "text", "GET"),
    SeamOperation("Safety", "live_controller", "LiveController", "get_live_status", "none", "live status", "GET"),
    SeamOperation("Safety", "live_controller", "LiveController", "engage_killswitch", "reason", "engage result", "POST"),
    SeamOperation(
        "Safety",
        "live_controller",
        "LiveController",
        "store_credentials",
        "venue, profile, fields",
        "credential status",
        "POST",
    ),
)


def iter_seam_operations() -> tuple[SeamOperation, ...]:
    return SEAM_OPERATIONS


class DocStore(Protocol):
    def list_docs(self) -> list[dict[str, Any]] | OpError: ...
    def read_doc(self, path: str) -> dict[str, Any] | OpError: ...


class NotesStore(Protocol):
    def list_notes(self) -> list[dict[str, Any]] | OpError: ...
    def read_note(self, name: str) -> dict[str, Any] | OpError: ...
    def save_note(self, name: str, markdown: str, expected_version: int | None = None) -> dict[str, Any] | OpError: ...


class DataService(Protocol):
    def list_soaks(
        self,
        cursor: str | None = None,
        limit: int = 50,
        edges_only: bool | None = None,
    ) -> dict[str, Any] | OpError: ...
    def get_soak(self, soak_id: str) -> dict[str, Any] | OpError: ...
    def list_soak_rows(
        self,
        soak_id: str,
        kind: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[StandardizedEdgeRow | StandardizedDataRow] | OpError: ...


class PairRegistryService(Protocol):
    def list_active_pairs(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | OpError: ...
    def list_pairs_needing_approval(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | OpError: ...
    def get_pair_summary(self, pair_key: str) -> PairSummary | OpError: ...
    def list_archived_pairs(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | OpError: ...
    def review_pair(
        self,
        pair_key: str,
        decision: str,
        reviewer: str,
        notes: str,
        confirm: str,
    ) -> dict[str, Any] | OpError: ...


class ScannerController(Protocol):
    def start_scanner(
        self,
        pairs: list[str] | str | None = None,
        pair_keys: list[str] | str | None = None,
        record: bool | str = True,
        edges_only: bool | str = False,
        batch: int | str | None = None,
        batch_size: int | str | None = None,
        tick: float | str | None = None,
        tick_s: float | str | None = None,
        confirm_survival_ms: int | str | None = None,
    ) -> dict[str, Any] | OpError: ...
    def stop_scanner(self) -> dict[str, Any] | OpError: ...
    def get_scanner_status(self) -> dict[str, Any] | OpError: ...


class AnalysisService(Protocol):
    def run_full_analysis(self, soak_ids: list[str]) -> dict[str, Any] | OpError: ...
    def get_analysis_status(self, job_id: str) -> dict[str, Any] | AnalysisSummary | OpError: ...


class TestSuiteRunner(Protocol):
    def run_test_suite(self) -> dict[str, Any] | OpError: ...
    def get_test_suite_result(self, job_id: str) -> dict[str, Any] | OpError: ...
    def get_test_run_detail(self, path: str) -> dict[str, str] | OpError: ...


class LiveController(Protocol):
    def get_live_status(self) -> dict[str, Any] | OpError: ...
    def engage_killswitch(self, reason: str) -> dict[str, Any] | OpError: ...
    def store_credentials(self, venue: str, profile: str, fields: dict[str, Any]) -> dict[str, Any] | OpError: ...
