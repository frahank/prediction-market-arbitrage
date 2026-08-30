# Scope: TEST — M2-T5 TestSuiteRunner service.
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from arbx.services.testsuite import TestSuiteRunnerImpl
from arbx.ui.envelope import OpError


def _runner(
    tmp_path: Path,
    *,
    run_func=subprocess.run,
    timeout_s: float = 30.0,
    pytest_args: tuple[str, ...] | None = None,
) -> TestSuiteRunnerImpl:
    args = pytest_args or (
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "--rootdir",
        str(tmp_path),
        str(tmp_path),
    )
    return TestSuiteRunnerImpl(
        tmp_path,
        tmp_path / "reports" / "test_runs",
        timeout_s=timeout_s,
        pytest_args=args,
        run_func=run_func,
    )


def _wait(runner: TestSuiteRunnerImpl, job_id: str, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = runner.get_test_suite_result(job_id)
        assert not isinstance(status, OpError)
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("test-suite job did not finish")


def _write_test(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_pass_and_fail_parsing(tmp_path: Path):
    _write_test(
        tmp_path / "test_mini.py",
        "def test_pass():\n    assert True\n\n"
        "def test_fail():\n    assert False\n",
    )
    runner = _runner(tmp_path)

    start = runner.run_test_suite()
    assert not isinstance(start, OpError)
    status = _wait(runner, start["job_id"])

    assert status["state"] == "failed"
    result = status["result"]
    assert result["passed"] is False
    assert result["total"] == 2
    assert result["failures"] == 1
    assert result["errors"] == 0
    assert result["message"] == "The bot is NOT healthy: 1 failing tests."


def test_detail_file_written(tmp_path: Path):
    _write_test(tmp_path / "test_ok.py", "def test_ok():\n    assert 1 == 1\n")
    runner = _runner(tmp_path)

    start = runner.run_test_suite()
    assert not isinstance(start, OpError)
    status = _wait(runner, start["job_id"])

    assert status["state"] == "done"
    result = status["result"]
    assert result["passed"] is True
    assert result["message"] == "The bot is working properly."
    detail = runner.get_test_run_detail(result["detail_path"])
    assert not isinstance(detail, OpError)
    assert "command:" in detail["text"]
    assert "1 passed" in detail["text"]


def test_single_instance_conflict(tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    def slow_run(*args, **kwargs):
        started.set()
        release.wait(timeout=5.0)
        return subprocess.CompletedProcess(args[0], 0, stdout="1 passed in 0.01s")

    runner = _runner(tmp_path, run_func=slow_run)

    first = runner.run_test_suite()
    assert not isinstance(first, OpError)
    assert started.wait(timeout=2.0)
    second = runner.run_test_suite()
    assert isinstance(second, OpError)
    assert second.code == "conflict"

    release.set()
    status = _wait(runner, first["job_id"])
    assert status["state"] == "done"


def test_timeout_maps_to_failed(tmp_path: Path):
    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=0.01, output="partial output\n")

    runner = _runner(tmp_path, run_func=timeout_run, timeout_s=0.01)

    start = runner.run_test_suite()
    assert not isinstance(start, OpError)
    status = _wait(runner, start["job_id"])

    assert status["state"] == "failed"
    assert "timed out" in status["error"]
    result = status["result"]
    assert result["passed"] is False
    assert result["errors"] == 1
    detail = runner.get_test_run_detail(result["detail_path"])
    assert not isinstance(detail, OpError)
    assert "partial output" in detail["text"]
    assert "timed out" in detail["text"]
