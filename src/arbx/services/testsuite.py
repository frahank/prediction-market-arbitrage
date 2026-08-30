# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — M2-T5 TestSuiteRunner service.
"""Background pytest runner for the Paper Dashboard health check."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from arbx.core.redact import redact_text
from arbx.ui.envelope import OpError
from arbx.ui.schemas import TestSuiteResult

RunFunc = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_PYTEST_ARGS = ("-q", "--tb=short")
DEFAULT_TIMEOUT_S = 1800.0

_SUMMARY_RE = re.compile(
    r"(?P<body>(?:\d+\s+[A-Za-z_]+)(?:,\s*\d+\s+[A-Za-z_]+)*)\s+in\s+"
    r"(?P<duration>\d+(?:\.\d+)?)s"
)


@dataclass(slots=True)
class _TestJob:
    job_id: str
    state: str
    started_at: str
    detail_path: Path
    result: TestSuiteResult | None = None
    error: str | None = None
    note: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_environment() -> dict[str, str]:
    allow = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in allow}


def _job_id() -> str:
    return f"test_{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}"


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _detail_path_for_ui(repo_root: Path, detail_path: Path) -> str:
    try:
        return detail_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return detail_path.resolve().as_posix()


def _parse_counts(output: str, *, return_code: int, fallback_duration_s: float) -> tuple[int, int, int, float]:
    """Return ``(total, failures, errors, duration_s)`` from pytest output."""
    for line in reversed(output.splitlines()):
        stripped = line.strip("= ").strip()
        match = _SUMMARY_RE.search(stripped)
        if not match:
            continue
        buckets: dict[str, int] = {}
        for part in match.group("body").split(","):
            count_text, name = part.strip().split(maxsplit=1)
            buckets[name.rstrip("s").lower()] = int(count_text)
        failures = buckets.get("failed", 0) + buckets.get("failure", 0)
        errors = buckets.get("error", 0)
        total = (
            buckets.get("passed", 0)
            + failures
            + errors
            + buckets.get("skipped", 0)
            + buckets.get("xfailed", 0)
            + buckets.get("xpassed", 0)
        )
        if return_code != 0 and failures + errors == 0:
            errors = 1
            total = max(total, 1)
        return total, failures, errors, float(match.group("duration"))

    if "no tests ran" in output.lower():
        return 0, 0, 0 if return_code == 0 else 1, fallback_duration_s
    if return_code != 0:
        return 1, 0, 1, fallback_duration_s
    return 0, 0, 0, fallback_duration_s


class TestSuiteRunnerImpl:
    """Run pytest once in the background and expose a compact health result."""

    __test__ = False

    def __init__(
        self,
        repo_root: Path,
        reports_dir: Path,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        pytest_args: Sequence[str] = DEFAULT_PYTEST_ARGS,
        run_func: RunFunc = subprocess.run,
        scanner_status: Callable[[], dict[str, Any] | OpError] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.reports_dir = Path(reports_dir).resolve()
        self.timeout_s = float(timeout_s)
        self.pytest_args = tuple(str(arg) for arg in pytest_args)
        self.run_func = run_func
        self.scanner_status = scanner_status
        self._lock = threading.Lock()
        self._jobs: dict[str, _TestJob] = {}
        self._active_job_id: str | None = None

    def run_test_suite(self) -> dict[str, str] | OpError:
        with self._lock:
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active is not None and active.state == "running":
                    return OpError("conflict", "a test-suite run is already active")

            job_id = _job_id()
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            detail_path = self.reports_dir / f"{job_id}.txt"
            job = _TestJob(
                job_id=job_id,
                state="running",
                started_at=_utc_now(),
                detail_path=detail_path,
                note=self._scanner_note(),
            )
            self._jobs[job_id] = job
            self._active_job_id = job_id

        threading.Thread(target=self._run_worker, args=(job_id,), daemon=True).start()
        return {"job_id": job_id}

    def get_test_suite_result(self, job_id: str) -> dict[str, Any] | OpError:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return OpError("not_found", "test-suite job was not found")
            return {
                "state": job.state,
                "result": job.result.to_dict() if job.result is not None else None,
                "error": job.error,
                "note": job.note,
            }

    def get_test_run_detail(self, path: str) -> dict[str, str] | OpError:
        if not path:
            return OpError("invalid_request", "path is required")
        requested = Path(path)
        candidate = requested if requested.is_absolute() else self.repo_root / requested
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.reports_dir)
        except (OSError, ValueError):
            return OpError("invalid_request", "path is outside test run reports")
        if not resolved.exists():
            return OpError("not_found", "test run detail was not found")
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError:
            return OpError("not_found", "test run detail was not found")
        return {"path": _detail_path_for_ui(self.repo_root, resolved), "text": text}

    def _run_worker(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
        command = [sys.executable, "-m", "pytest", *self.pytest_args]
        started = datetime.now(timezone.utc)
        output = ""
        return_code = 1
        error: str | None = None
        try:
            completed = self.run_func(
                command,
                cwd=self.repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.timeout_s,
                env=_safe_environment(),
            )
            return_code = int(completed.returncode)
            output = _as_text(completed.stdout)
        except subprocess.TimeoutExpired as exc:
            return_code = 124
            output = _as_text(exc.stdout) + _as_text(exc.stderr)
            error = f"test suite timed out after {self.timeout_s:g}s"
        except OSError as exc:
            return_code = 1
            error = "test suite could not start"
            output = redact_text(str(exc))

        completed_at = datetime.now(timezone.utc)
        elapsed = max(0.0, (completed_at - started).total_seconds())
        clean_output = redact_text(output)
        total, failures, errors, duration_s = _parse_counts(
            clean_output,
            return_code=return_code,
            fallback_duration_s=elapsed,
        )
        if error and return_code == 124:
            total = max(total, 1)
            errors = max(errors, 1)
        passed = return_code == 0 and failures == 0 and errors == 0
        failing_count = failures + errors
        message = (
            "The bot is working properly."
            if passed
            else f"The bot is NOT healthy: {failing_count} failing tests."
        )
        result = TestSuiteResult(
            passed=passed,
            total=total,
            failures=failures,
            errors=errors,
            duration_s=duration_s,
            message=message,
            detail_path=_detail_path_for_ui(self.repo_root, job.detail_path),
        )
        self._write_detail(
            job,
            command=command,
            return_code=return_code,
            output=clean_output,
            started_at=started.isoformat(),
            completed_at=completed_at.isoformat(),
            error=error,
        )
        with self._lock:
            job.result = result
            job.error = error
            job.state = "done" if passed else "failed"
            if self._active_job_id == job_id:
                self._active_job_id = None

    def _write_detail(
        self,
        job: _TestJob,
        *,
        command: list[str],
        return_code: int,
        output: str,
        started_at: str,
        completed_at: str,
        error: str | None,
    ) -> None:
        lines = [
            f"job_id: {job.job_id}",
            f"started_at: {started_at}",
            f"completed_at: {completed_at}",
            f"command: {' '.join(command)}",
            f"cwd: {self.repo_root}",
            f"return_code: {return_code}",
        ]
        if job.note:
            lines.append(f"note: {job.note}")
        if error:
            lines.append(f"error: {error}")
        lines.extend(["", output])
        job.detail_path.write_text("\n".join(lines), encoding="utf-8")

    def _scanner_note(self) -> str | None:
        if self.scanner_status is None:
            return None
        try:
            status = self.scanner_status()
        except Exception:  # noqa: BLE001 - health-note failure must not block tests
            return None
        if isinstance(status, OpError):
            return None
        if status.get("running"):
            run_id = status.get("run_id") or "unknown"
            return f"scanner was running during test-suite start (run_id={run_id})"
        return None
