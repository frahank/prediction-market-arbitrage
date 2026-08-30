# ScannerController service.
from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from arbx.pairs.registry import write_registry_integrity
from arbx.services.datastore import SoakStoreImpl
from arbx.services.scanner import ScannerControllerImpl
from arbx.ui.envelope import OpError

ROOT = Path(__file__).resolve().parents[1]


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.returncode = 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        return "[run_scanner] fake stdout", ""


class _PopenFactory:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.processes: list[_FakeProcess] = []

    def __call__(self, command: list[str], **kwargs: Any) -> _FakeProcess:
        self.commands.append(command)
        process = _FakeProcess()
        self.processes.append(process)
        assert kwargs["cwd"] == ROOT
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["text"] is True
        return process


def _pair_entry(
    pair_key: str,
    *,
    approve: bool,
    status: str = "approved_for_paper",
) -> dict[str, Any]:
    return {
        "pair_key": pair_key,
        "kalshi_market_id": pair_key.split("|")[0],
        "orientation": "same",
        "status": status,
        "include_in_strategy_metrics": True,
        "polymarket_identifiers": {
            "condition_id": pair_key.split("|")[1],
            "yes_token_id": f"yes-{pair_key}",
            "no_token_id": f"no-{pair_key}",
        },
        "taxonomy": {
            "resolution_structure": "objective_single_event",
            "grouping_alignment": "n/a",
            "persistence_cause": "none",
        },
        "equivalence": {"status": "verified_equivalent"},
        "decision_log": [
            {
                "at": "2026-07-05T00:00:00+00:00",
                "decision": "approve" if approve else "reject",
                "rationale": "fixture",
                "auditor": "test",
            }
        ],
        "display_name": pair_key,
    }


def _write_registry(path: Path) -> None:
    payload = {
        "schema_version": 2.1,
        "pairs": [
            _pair_entry("KXONE|0x1", approve=True),
            # Rejected as a strategy candidate but still recordable.
            _pair_entry("KXTWO|0x2", approve=False),
            _pair_entry("KXTHREE|0x3", approve=True),
            # Archived: out of the registry's working set, so never scannable.
            _pair_entry("KXFOUR|0x4", approve=True, status="archived"),
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    write_registry_integrity(path)


def _write_config(path: Path, *, cap: int = 20) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "default_batch_size": 20,
                "default_tick_s": 1.0,
                "max_calls_per_s_per_venue": cap,
                "min_arb_edge": 0.0,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _controller(tmp_path: Path, popen: _PopenFactory | None = None) -> ScannerControllerImpl:
    registry = tmp_path / "pairs.approved.yaml"
    config = tmp_path / "scanner.yaml"
    _write_registry(registry)
    _write_config(config)
    store = SoakStoreImpl(tmp_path / "data" / "soaks", [])
    return ScannerControllerImpl(
        repo_root=ROOT,
        pair_registry_path=registry,
        soak_store=store,
        config_path=config,
        popen_factory=popen or _PopenFactory(),
        stop_timeout_s=0.01,
        monitor_interval_s=60.0,
    )


def _assert_ok(result: dict[str, Any] | OpError) -> dict[str, Any]:
    assert not isinstance(result, OpError), result
    return result


def test_start_creates_contract_layout(tmp_path: Path):
    popen = _PopenFactory()
    controller = _controller(tmp_path, popen)

    result = _assert_ok(controller.start_scanner(pair_keys=["KXONE|0x1"], batch_size=1, tick_s=1.0))

    soak_path = Path(result["soak_path"])
    assert soak_path.name == result["soak_id"]
    assert (soak_path / "manifest.json").exists()
    assert (soak_path / "raw" / "book").is_dir()
    assert (soak_path / "scan" / "opportunities").is_dir()
    manifest = json.loads((soak_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pair_keys"] == ["KXONE|0x1"]
    assert manifest["record_books"] is True
    assert manifest["ended_at"] is None

    command = popen.commands[0]
    assert command[0] == sys.executable
    assert str(ROOT / "scripts" / "run_scanner.py") in command
    assert "--run-id" in command
    assert "--no-record-books" not in command
    selected = yaml.safe_load((soak_path / "pairs.selected.yaml").read_text(encoding="utf-8"))
    assert [entry["pair_key"] for entry in selected["pairs"]] == ["KXONE|0x1"]

    listed, _cursor = controller.soak_store.list_soaks()
    assert listed and listed[0].soak_id == result["soak_id"]


def test_edges_only_requires_record(tmp_path: Path):
    controller = _controller(tmp_path)

    result = controller.start_scanner(record=False, edges_only=True)

    assert isinstance(result, OpError)
    assert result.code == "invalid_request"


def test_rate_limit_refusal(tmp_path: Path):
    controller = _controller(tmp_path)

    result = controller.start_scanner(batch_size=30, tick_s=1.0)

    assert isinstance(result, OpError)
    assert result.code == "invalid_request"
    assert "rate limit" in result.message


def test_second_start_conflicts(tmp_path: Path):
    controller = _controller(tmp_path)

    _assert_ok(controller.start_scanner(pair_keys=["KXONE|0x1"], batch_size=1, tick_s=1.0))
    result = controller.start_scanner(pair_keys=["KXTHREE|0x3"], batch_size=1, tick_s=1.0)

    assert isinstance(result, OpError)
    assert result.code == "conflict"


def test_stop_finalizes_manifest_and_summary(tmp_path: Path):
    popen = _PopenFactory()
    controller = _controller(tmp_path, popen)
    start = _assert_ok(controller.start_scanner(pair_keys=["KXONE|0x1"], batch_size=1, tick_s=1.0))

    stopped = _assert_ok(controller.stop_scanner())

    assert popen.processes[0].signals == [signal.SIGINT]
    assert stopped["run_id"] == start["run_id"]
    manifest = json.loads((Path(start["soak_path"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ended_at"] is not None
    summary = json.loads((Path(start["soak_path"]) / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["run_id"] == start["run_id"]
    assert stopped["summary"]["run_id"] == start["run_id"]
    status = controller.get_scanner_status()
    assert status["running"] is False
    assert status["return_code"] == 0


def test_default_scans_every_paper_approved_pair(tmp_path: Path):
    """The default run records what may be observed, not what was tradeable.

    ``KXTWO`` was rejected as a strategy candidate. That verdict governs whether
    it counts toward strategy metrics, not whether its public book may be
    captured, so it belongs in a default scan. ``KXFOUR`` is archived and so
    leaves the working set entirely.
    """
    popen = _PopenFactory()
    controller = _controller(tmp_path, popen)

    result = _assert_ok(controller.start_scanner(batch_size=2, tick_s=1.0))

    expected = ["KXONE|0x1", "KXTWO|0x2", "KXTHREE|0x3"]
    manifest = json.loads((Path(result["soak_path"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pair_keys"] == expected
    assert result["pair_count"] == 3
    selected = yaml.safe_load((Path(result["soak_path"]) / "pairs.selected.yaml").read_text(encoding="utf-8"))
    assert [entry["pair_key"] for entry in selected["pairs"]] == expected


def test_archived_pair_is_unreachable_by_either_path(tmp_path: Path):
    """Both selection paths apply the same predicate.

    Requesting a pair explicitly used to skip the default path's filter
    entirely, so a pair the default would never choose could still be scanned.
    """
    controller = _controller(tmp_path)

    default_keys = {pair.pair_key for pair in controller._select_pairs(None)}
    assert "KXFOUR|0x4" not in default_keys

    result = controller.start_scanner(pair_keys=["KXFOUR|0x4"], batch_size=1, tick_s=1.0)
    assert isinstance(result, OpError)
    assert result.code == "invalid_request"


def test_strategy_eligibility_is_independent_of_scannability(tmp_path: Path):
    """Scanning a pair never implies it counts toward strategy metrics."""
    controller = _controller(tmp_path)

    selected = {pair.pair_key: pair for pair in controller._select_pairs(None)}

    assert selected["KXTWO|0x2"].include_in_strategy_metrics is False
    assert selected["KXONE|0x1"].include_in_strategy_metrics is True


def test_scanner_command_allowlist_rejects_missing_script(tmp_path: Path):
    """The allowlist must be able to fail — it was previously a tautology
    (``script`` and ``expected`` were the same expression), so it could not."""
    registry = tmp_path / "pairs.approved.yaml"
    config = tmp_path / "scanner.yaml"
    _write_registry(registry)
    _write_config(config)
    controller = ScannerControllerImpl(
        repo_root=tmp_path,  # a root with no scripts/run_scanner.py
        pair_registry_path=registry,
        soak_store=SoakStoreImpl(tmp_path / "data" / "soaks", []),
        config_path=config,
        popen_factory=_PopenFactory(),
        stop_timeout_s=0.01,
        monitor_interval_s=60.0,
    )
    with pytest.raises(ValueError, match="scanner script is missing"):
        controller._scanner_command(
            tmp_path / "pairs.yaml",
            tmp_path / "data",
            run_id="r1",
            batch_size=1,
            tick_s=1.0,
            min_arb_edge=0.01,
            record_books=False,
            edges_only=False,
            confirm_survival_ms=None,
        )
