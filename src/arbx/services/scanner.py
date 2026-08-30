# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# ScannerController service backing the Paper tab.
"""Subprocess-backed controller for the paper scanner.

The service layer intentionally does not import venue or capture internals. It
starts the already-audited scanner CLI with an allowlisted command, writes the
contract soak manifest first, and keeps lifecycle state behind the UI facade.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from arbx.core.redact import redact_text
from arbx.exec.killswitch import KillSwitch, KillSwitchEngaged, default_killswitch
from arbx.pairs.registry import (
    SCANNABLE_STATUSES,
    PairSpec,
    load_pairs,
    write_registry_integrity,
)
from arbx.scanner.rotation import effective_cadence_s
from arbx.services.datastore import SoakStoreImpl
from arbx.ui.envelope import SCHEMA_VERSION, OpError

ACTIVE_STATES = {"starting", "running", "stopping"}
TERMINAL_STATES = {"completed", "failed", "stopped"}
DEFAULT_SCANNER_DURATION_S = 7 * 24 * 60 * 60
# The only script this controller may launch, as a repo-root-relative POSIX
# path. Checked against the resolved candidate so neither a relocated repo_root
# nor a symlinked script can redirect the subprocess.
ALLOWED_SCANNER_SCRIPTS = frozenset({"scripts/run_scanner.py"})

# The scanner's streams go to files in the run directory, never to pipes. A
# pipe the parent does not drain fills its OS buffer (~64 KB) and blocks the
# child forever on its next write; this controller only reads after the process
# exits, so it could not drain one. Files remove the deadlock entirely and keep
# the full log for the operator. Only this much is read back for the UI.
SCANNER_LOG_TAIL_CHARS = 4000
SCANNER_STDOUT_LOG = "scan_stdout.log"
SCANNER_STDERR_LOG = "scan_stderr.log"


def _tail_text(path: Path | None, limit: int = SCANNER_LOG_TAIL_CHARS) -> str:
    """Last ``limit`` characters of ``path``, reading only the tail off disk.

    Seeks from the end so a multi-gigabyte log costs the same as an empty one.
    Over-reads by 4x to stay safe on a multi-byte character boundary, then
    trims; a partial leading character is dropped by ``errors="replace"``.
    """
    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit * 4), os.SEEK_SET)
            raw = handle.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")[-limit:]


@dataclass(slots=True)
class ScannerRunState:
    state: str = "idle"
    run_id: str | None = None
    soak_id: str | None = None
    soak_path: str | None = None
    pair_count: int = 0
    batch_size: int = 20
    tick_s: float = 1.0
    effective_cadence_s: float = 0.0
    record_books: bool = True
    edges_only: bool = False
    started_at: str | None = None
    stopped_at: str | None = None
    completed_at: str | None = None
    return_code: int | None = None
    last_error: str | None = None
    sanitized_stdout: str = ""
    sanitized_stderr: str = ""
    summary: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["running"] = self.state in ACTIVE_STATES
        record["real_orders"] = 0
        record["paper_only"] = True
        if self.started_at:
            started = datetime.fromisoformat(self.started_at)
            end = (
                datetime.fromisoformat(self.completed_at)
                if self.completed_at
                else datetime.now(timezone.utc)
            )
            record["elapsed_seconds"] = max(0.0, (end - started).total_seconds())
        else:
            record["elapsed_seconds"] = 0.0
        return record


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_param(value: bool | str | None, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean parameter must be true or false")


def _int_param(value: int | str | None, default: int, *, min_value: int = 1) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("integer parameter is invalid") from exc
    if parsed < min_value:
        raise ValueError(f"integer parameter must be >= {min_value}")
    return parsed


def _float_param(value: float | int | str | None, default: float, *, min_value: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("float parameter is invalid") from exc
    if parsed < min_value:
        raise ValueError(f"float parameter must be >= {min_value}")
    return parsed


def _optional_int_param(value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    return _int_param(value, 0, min_value=1)


def _pair_keys_param(value: list[str] | tuple[str, ...] | str | None) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    raise ValueError("pairs must be a list or comma-separated string")


class ScannerControllerImpl:
    """Start/stop facade for the live scanner subprocess."""

    def __init__(
        self,
        *,
        repo_root: Path,
        pair_registry_path: Path,
        soak_store: SoakStoreImpl,
        config_path: Path,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        stop_timeout_s: float = 5.0,
        monitor_interval_s: float = 0.25,
        killswitch: KillSwitch | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.pair_registry_path = Path(pair_registry_path).resolve()
        self.soak_store = soak_store
        self.config_path = Path(config_path).resolve()
        self.killswitch = killswitch if killswitch is not None else default_killswitch()
        self.popen_factory = popen_factory
        self.stop_timeout_s = stop_timeout_s
        self.monitor_interval_s = monitor_interval_s
        self._lock = threading.Lock()
        self._finalize_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._log_handles: list[Any] = []
        self._stdout_log: Path | None = None
        self._stderr_log: Path | None = None
        self._state = ScannerRunState()
        self._manifest_path: Path | None = None
        self._summary_path: Path | None = None

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
    ) -> dict[str, Any] | OpError:
        # The kill switch refuses every start, before any validation or I/O.
        try:
            self.killswitch.check_or_raise()
        except KillSwitchEngaged as exc:
            return OpError("killswitch_engaged", redact_text(str(exc)))
        try:
            config = self._load_config()
            record_value = _bool_param(record, True)
            edges_only_value = _bool_param(edges_only, False)
            if edges_only_value and not record_value:
                return OpError("invalid_request", "edges_only requires record=true")
            batch_value = _int_param(
                batch_size if batch_size is not None else batch,
                int(config["default_batch_size"]),
            )
            tick_value = _float_param(
                tick_s if tick_s is not None else tick,
                float(config["default_tick_s"]),
                min_value=0.001,
            )
            confirm_value = _optional_int_param(confirm_survival_ms)
            self._check_rate_limit(batch_value, tick_value, config)
            selected = self._select_pairs(_pair_keys_param(pair_keys if pair_keys is not None else pairs))
        except ValueError as exc:
            return OpError("invalid_request", str(exc))
        except Exception as exc:  # noqa: BLE001 - facade boundary returns safe errors
            return OpError("invalid_request", redact_text(str(exc)))

        if not selected:
            return OpError("invalid_request", "no approved strategy pairs selected")

        with self._lock:
            if self._state.state in ACTIVE_STATES:
                return OpError("conflict", "a scanner is already active")

        started_at = _utc_now()
        soak_id, soak_path = self._prepare_output(
            selected,
            started_at=started_at,
            record=record_value,
            edges_only=edges_only_value,
        )
        run_id = soak_id if soak_id is not None else f"scan_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        data_dir = soak_path if soak_path is not None else self.soak_store.soaks_root / ".cache" / "scanner" / run_id
        data_dir.mkdir(parents=True, exist_ok=True)
        pairs_path = self._write_selected_registry(data_dir, selected)
        command = self._scanner_command(
            pairs_path,
            data_dir,
            run_id=run_id,
            batch_size=batch_value,
            tick_s=tick_value,
            min_arb_edge=float(config["min_arb_edge"]),
            record_books=record_value and not edges_only_value,
            edges_only=edges_only_value,
            confirm_survival_ms=confirm_value,
        )

        with self._lock:
            self._state = ScannerRunState(
                state="starting",
                run_id=run_id,
                soak_id=soak_id,
                soak_path=soak_path.as_posix() if soak_path is not None else None,
                pair_count=len(selected),
                batch_size=batch_value,
                tick_s=tick_value,
                effective_cadence_s=effective_cadence_s(len(selected), batch_value, tick_value),
                record_books=record_value and not edges_only_value,
                edges_only=edges_only_value,
                started_at=started_at,
            )
            self._summary_path = data_dir / "scan_summary.json"
            self._stdout_log = data_dir / SCANNER_STDOUT_LOG
            self._stderr_log = data_dir / SCANNER_STDERR_LOG
            try:
                stdout_handle = self._stdout_log.open("w", encoding="utf-8")
                stderr_handle = self._stderr_log.open("w", encoding="utf-8")
                self._log_handles = [stdout_handle, stderr_handle]
                self._process = self.popen_factory(
                    command,
                    cwd=self.repo_root,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    env=_safe_environment(),
                )
            except OSError as exc:
                self._close_log_handles()
                self._state.state = "failed"
                self._state.last_error = redact_text(str(exc))
                self._finalize_manifest(ended_at=_utc_now())
                return OpError("internal_error", "scanner process could not start")
            self._state.state = "running"
            threading.Thread(target=self._monitor, daemon=True).start()
            return self._start_response_unlocked()

    def stop_scanner(self) -> dict[str, Any] | OpError:
        with self._lock:
            process = self._process
            if process is None or self._state.state not in ACTIVE_STATES:
                return OpError("invalid_request", "no active scanner")
            self._state.state = "stopping"
        stopped_at = _utc_now()
        try:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=self.stop_timeout_s)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self.stop_timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.stop_timeout_s)
        state = self._finalize_process(process, stopped_at=stopped_at, stopped=True)
        return {
            "run_id": state["run_id"],
            "stopped_at": state["stopped_at"] or state["completed_at"],
            "summary": state["summary"],
        }

    def get_scanner_status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is not None:
            self._finalize_process(process)
        with self._lock:
            state = self._state.to_record()
        summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
        return {
            "running": bool(state["running"]),
            "state": state["state"],
            "run_id": state["run_id"],
            "soak_id": state["soak_id"],
            "pair_count": state["pair_count"],
            "batch_size": state["batch_size"],
            "tick_s": state["tick_s"],
            "effective_cadence_s": state["effective_cadence_s"],
            "ticks": int(summary.get("ticks") or 0),
            "snapshots": int(summary.get("snapshots") or 0),
            "arbs_detected": int(summary.get("arbs_detected") or 0),
            "qualifying": int(summary.get("qualifying") or 0),
            "fetch_errors": int(summary.get("fetch_errors") or 0),
            "last_tick_at": summary.get("generated_at") or state["completed_at"] or state["started_at"],
            "edges_written": self._edges_written(),
            "return_code": state["return_code"],
            "last_error": state["last_error"],
        }

    def _load_config(self) -> dict[str, Any]:
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        return {
            "default_batch_size": int(data.get("default_batch_size", 20)),
            "default_tick_s": float(data.get("default_tick_s", 1.0)),
            "max_calls_per_s_per_venue": float(data.get("max_calls_per_s_per_venue", 20)),
            "min_arb_edge": float(data.get("min_arb_edge", 0.0)),
        }

    @staticmethod
    def _check_rate_limit(batch_size: int, tick_s: float, config: dict[str, Any]) -> None:
        per_venue = batch_size / tick_s
        if per_venue > float(config["max_calls_per_s_per_venue"]):
            raise ValueError("scanner rate limit would be exceeded")

    def _select_pairs(self, requested: list[str] | None) -> list[PairSpec]:
        """Pairs this scanner may record, by the same rule on both paths.

        Scannability and strategy eligibility answer different questions. A pair
        rejected during review was judged untradeable — long carry, no fee-band
        edge — which says nothing about whether its public book may be observed,
        and reproducing the research requires exactly that observation. So the
        gate here is ``status``, and ``include_in_strategy_metrics`` is left to
        do its real job downstream: ``arbx.scanner.edges_writer`` stamps it onto
        every edge row, and the analysis and heatmap layers filter the reported
        metrics on that stamp. Honesty is enforced where the numbers are
        produced, not by refusing to collect data.
        """
        pairs = load_pairs(self.pair_registry_path)
        scannable = [pair for pair in pairs if pair.status in SCANNABLE_STATUSES]
        if requested is None:
            if not scannable:
                raise ValueError(
                    f"no scannable pairs in {self.pair_registry_path.name}: "
                    f"{len(pairs)} present, none with status "
                    f"{sorted(SCANNABLE_STATUSES)[0]!r}"
                )
            return scannable
        by_key = {pair.pair_key: pair for pair in scannable}
        missing = [pair_key for pair_key in requested if pair_key not in by_key]
        if missing:
            raise ValueError(f"unknown or non-scannable pair_key: {missing[0]}")
        return [by_key[pair_key] for pair_key in requested]

    def _prepare_output(
        self,
        selected: list[PairSpec],
        *,
        started_at: str,
        record: bool,
        edges_only: bool,
    ) -> tuple[str | None, Path | None]:
        self._manifest_path = None
        if not record:
            return None, None
        suffix = "_EDGES" if edges_only else ""
        base = f"scan_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}{suffix}"
        soak_path = self.soak_store.soaks_root / base
        counter = 1
        while soak_path.exists():
            soak_path = self.soak_store.soaks_root / f"{base}_{counter}"
            counter += 1
        soak_path.mkdir(parents=True)
        (soak_path / "scan" / "opportunities").mkdir(parents=True)
        if not edges_only:
            (soak_path / "raw" / "book").mkdir(parents=True)
        manifest = {
            "soak_id": soak_path.name,
            "label": soak_path.name,
            "started_at": started_at,
            "ended_at": None,
            "pair_keys": [pair.pair_key for pair in selected],
            "edges_only": edges_only,
            "record_books": not edges_only,
            "schema_version": SCHEMA_VERSION,
        }
        self._manifest_path = soak_path / "manifest.json"
        self._manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return soak_path.name, soak_path

    @staticmethod
    def _write_selected_registry(data_dir: Path, selected: list[PairSpec]) -> Path:
        path = data_dir / "pairs.selected.yaml"
        payload = {
            "schema_version": 2.1,
            "registry_type": "scanner_selected_pairs",
            "pairs": [pair.raw for pair in selected],
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        write_registry_integrity(path)
        return path

    def _scanner_command(
        self,
        pairs_path: Path,
        data_dir: Path,
        *,
        run_id: str,
        batch_size: int,
        tick_s: float,
        min_arb_edge: float,
        record_books: bool,
        edges_only: bool,
        confirm_survival_ms: int | None,
    ) -> list[str]:
        script = (self.repo_root / "scripts" / "run_scanner.py").resolve()
        try:
            relative = script.relative_to(self.repo_root).as_posix()
        except ValueError:
            raise ValueError("scanner command resolves outside the repository") from None
        if relative not in ALLOWED_SCANNER_SCRIPTS:
            raise ValueError("scanner command is not allowlisted")
        if not script.is_file():
            raise ValueError(f"scanner script is missing: {relative}")
        command = [
            sys.executable,
            str(script),
            "--pairs",
            str(pairs_path),
            "--data-dir",
            str(data_dir),
            "--duration",
            str(DEFAULT_SCANNER_DURATION_S),
            "--batch-size",
            str(batch_size),
            "--tick-s",
            str(tick_s),
            "--min-arb-edge",
            str(min_arb_edge),
            "--run-id",
            run_id,
        ]
        if edges_only:
            command.append("--edges-only")
        elif not record_books:
            command.append("--no-record-books")
        if confirm_survival_ms is not None:
            command.extend(["--confirm-survival-ms", str(confirm_survival_ms)])
        return command

    def _start_response_unlocked(self) -> dict[str, Any]:
        return {
            "run_id": self._state.run_id,
            "soak_id": self._state.soak_id,
            "soak_path": self._state.soak_path,
            "pair_count": self._state.pair_count,
            "effective_cadence_s": self._state.effective_cadence_s,
            "started_at": self._state.started_at,
        }

    def _monitor(self) -> None:
        while True:
            with self._lock:
                process = self._process
                state = self._state.state
            if process is None or state not in ACTIVE_STATES:
                return
            if state == "stopping":
                return
            if process.poll() is not None:
                self._finalize_process(process)
                return
            time.sleep(self.monitor_interval_s)

    def _finalize_process(
        self,
        process: subprocess.Popen[str],
        *,
        stopped_at: str | None = None,
        stopped: bool = False,
    ) -> dict[str, Any]:
        with self._finalize_lock:
            with self._lock:
                if self._process is not process and self._state.state in TERMINAL_STATES:
                    return self._state.to_record()
            # Every caller reaches here only after the process has exited, so
            # there is nothing to wait for and nothing to drain: the streams
            # were written straight to disk.
            self._close_log_handles()
            stdout = _tail_text(self._stdout_log)
            stderr = _tail_text(self._stderr_log)
            completed_at = _utc_now()
            summary = self._read_or_write_summary(
                completed_at=completed_at,
                return_code=process.returncode,
                stopped_at=stopped_at,
            )
            self._finalize_manifest(ended_at=completed_at)
            with self._lock:
                self._state.return_code = process.returncode
                self._state.sanitized_stdout = redact_text(stdout)[-SCANNER_LOG_TAIL_CHARS:]
                self._state.sanitized_stderr = redact_text(stderr)[-SCANNER_LOG_TAIL_CHARS:]
                self._state.completed_at = completed_at
                self._state.stopped_at = stopped_at
                self._state.summary = summary
                if stopped:
                    self._state.state = "stopped"
                elif process.returncode == 0:
                    self._state.state = "completed"
                else:
                    self._state.state = "failed"
                    self._state.last_error = (
                        self._state.sanitized_stderr.strip()
                        or f"scanner exited with code {process.returncode}"
                    )
                self._process = None
                return self._state.to_record()

    def _close_log_handles(self) -> None:
        """Release the run's log files; safe to call more than once."""
        for handle in self._log_handles:
            try:
                handle.close()
            except OSError:
                pass
        self._log_handles = []

    def _read_or_write_summary(
        self,
        *,
        completed_at: str,
        return_code: int | None,
        stopped_at: str | None,
    ) -> dict[str, Any]:
        summary_path = self._summary_path
        if summary_path is not None and summary_path.exists():
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        edges_written = self._edges_written()
        with self._lock:
            summary = {
                "run_id": self._state.run_id,
                "soak_id": self._state.soak_id,
                "pairs": self._state.pair_count,
                "batch_size": self._state.batch_size,
                "tick_s": self._state.tick_s,
                "cycle_time_s": self._state.effective_cadence_s,
                "record_books": self._state.record_books,
                "edges_only": self._state.edges_only,
                "ticks": 0,
                "snapshots": 0,
                "fetch_errors": 0,
                "arbs_detected": 0,
                "qualifying": 0,
                "edges_written": edges_written,
                "return_code": return_code,
                "stopped_at": stopped_at,
                "generated_at": completed_at,
            }
        if summary_path is not None:
            try:
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
        return summary

    def _finalize_manifest(self, *, ended_at: str) -> None:
        path = self._manifest_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["ended_at"] = ended_at
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            return

    def _edges_written(self) -> int:
        with self._lock:
            soak_path_text = self._state.soak_path
        if not soak_path_text:
            return 0
        total = 0
        for path in Path(soak_path_text).glob("EDGES_*.jsonl"):
            try:
                with path.open("rb") as fh:
                    total += sum(1 for line in fh if line.strip())
            except OSError:
                continue
        return total
