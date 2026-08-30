# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Default not-implemented services for the UI seam (F-T4).
from __future__ import annotations

from typing import Any

from arbx.ui.envelope import OpError


def _not_implemented(name: str) -> OpError:
    return OpError("not_implemented", f"{name} is not implemented yet")


class StubDocStore:
    def list_docs(self) -> OpError:
        return _not_implemented("list_docs")

    def read_doc(self, path: str) -> OpError:
        return _not_implemented("read_doc")


class StubNotesStore:
    def list_notes(self) -> OpError:
        return _not_implemented("list_notes")

    def read_note(self, name: str) -> OpError:
        return _not_implemented("read_note")

    def save_note(self, name: str, markdown: str, expected_version: int | None = None) -> OpError:
        return _not_implemented("save_note")


class StubDataService:
    def list_soaks(
        self,
        cursor: str | None = None,
        limit: int = 50,
        edges_only: bool | None = None,
    ) -> OpError:
        return _not_implemented("list_soaks")

    def get_soak(self, soak_id: str) -> OpError:
        return _not_implemented("get_soak")

    def list_soak_rows(
        self,
        soak_id: str,
        kind: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> OpError:
        return _not_implemented("list_soak_rows")


class StubPairRegistryService:
    def list_active_pairs(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> OpError:
        return _not_implemented("list_active_pairs")

    def list_pairs_needing_approval(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> OpError:
        return _not_implemented("list_pairs_needing_approval")

    def get_pair_summary(self, pair_key: str) -> OpError:
        return _not_implemented("get_pair_summary")

    def list_archived_pairs(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> OpError:
        return _not_implemented("list_archived_pairs")

    def review_pair(
        self,
        pair_key: str,
        decision: str,
        reviewer: str,
        notes: str,
        confirm: str = "",
    ) -> OpError:
        return _not_implemented("review_pair")


class StubScannerController:
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
    ) -> OpError:
        return _not_implemented("start_scanner")

    def stop_scanner(self) -> OpError:
        return _not_implemented("stop_scanner")

    def get_scanner_status(self) -> OpError:
        return _not_implemented("get_scanner_status")


class StubAnalysisService:
    def run_full_analysis(self, soak_ids: list[str]) -> OpError:
        return _not_implemented("run_full_analysis")

    def get_analysis_status(self, job_id: str) -> OpError:
        return _not_implemented("get_analysis_status")


class StubTestSuiteRunner:
    def run_test_suite(self) -> OpError:
        return _not_implemented("run_test_suite")

    def get_test_suite_result(self, job_id: str) -> OpError:
        return _not_implemented("get_test_suite_result")

    def get_test_run_detail(self, path: str) -> OpError:
        return _not_implemented("get_test_run_detail")


class StubLiveController:
    def get_live_status(self) -> OpError:
        return _not_implemented("get_live_status")

    def engage_killswitch(self, reason: str) -> OpError:
        return _not_implemented("engage_killswitch")

    def store_credentials(self, venue: str, profile: str, fields: dict[str, Any]) -> OpError:
        return _not_implemented("store_credentials")
