# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Global kill switch.
"""The operator's one hard off-switch.

Engaged means: the sentinel file exists OR the environment variable
``ARBX_KILL == "1"``. Engaging writes the sentinel atomically (temp file +
``os.replace`` in the same directory) and then awaits ``cancel_all()``.

INVARIANT — there is no ``clear()`` (or any other un-engage method) anywhere
in code. Clearing the kill switch is a manual ``rm`` of the sentinel by the
operator only; the env-var form is cleared by unsetting ``ARBX_KILL``.
``tests/test_killswitch.py::test_no_clear_method_exists`` enforces this.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Repo config: src/arbx/exec/killswitch.py -> parents[3] is the repo root.
DEFAULT_RUNTIME_YAML = Path(__file__).resolve().parents[3] / "configs" / "runtime.yaml"
DEFAULT_SENTINEL_PATH = "~/.arbx/KILL"

KILL_ENV_VAR = "ARBX_KILL"


class KillSwitchEngaged(RuntimeError):
    """Raised by :meth:`KillSwitch.check_or_raise` when the switch is engaged."""


def killswitch_path_from_config(config_path: Path = DEFAULT_RUNTIME_YAML) -> Path:
    """``killswitch_path`` from ``configs/runtime.yaml``, ``~`` expanded.

    Missing/unreadable/malformed config falls back to ``~/.arbx/KILL`` — the
    kill switch must always have a working sentinel location.
    """
    raw: Any = None
    try:
        data = yaml.safe_load(Path(config_path).read_text())
        if isinstance(data, dict):
            raw = data.get("killswitch_path")
    except (OSError, yaml.YAMLError):
        raw = None
    text = str(raw).strip() if isinstance(raw, str) and str(raw).strip() else DEFAULT_SENTINEL_PATH
    return Path(text).expanduser()


class KillSwitch:
    """Sentinel-file kill switch shared by every subsystem.

    ``cancel_all`` is the composition root's "make everything safe" hook
    (for this project: stop the scanner). It runs after
    the sentinel is durably on disk so a crash mid-cancel still leaves the
    switch engaged.
    """

    def __init__(
        self,
        sentinel_path: Path,
        cancel_all: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.sentinel_path = Path(sentinel_path).expanduser()
        self.cancel_all = cancel_all

    def engaged(self) -> bool:
        return self.sentinel_path.exists() or os.environ.get(KILL_ENV_VAR) == "1"

    async def engage(self, reason: str) -> None:
        """Write the sentinel atomically, then await ``cancel_all()`` if set."""
        payload = {
            "reason": str(reason),
            "engaged_at": datetime.now(timezone.utc).isoformat(),
        }
        directory = self.sentinel_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".KILL.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.sentinel_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        if self.cancel_all is not None:
            await self.cancel_all()

    def check_or_raise(self) -> None:
        if self.engaged():
            reason = self.reason()
            detail = f": {reason}" if reason else ""
            raise KillSwitchEngaged(
                f"kill switch is engaged{detail} "
                f"(sentinel {self.sentinel_path} — operator must remove it manually)"
            )

    def reason(self) -> str | None:
        """The recorded engage reason, or ``None`` when not engaged."""
        try:
            text = self.sentinel_path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            try:
                data = json.loads(text)
                if isinstance(data, dict) and data.get("reason"):
                    return str(data["reason"])
            except json.JSONDecodeError:
                pass
            return text
        if self.sentinel_path.exists():
            return "engaged (sentinel present, no reason recorded)"
        if os.environ.get(KILL_ENV_VAR) == "1":
            return f"env {KILL_ENV_VAR}=1"
        return None

    def status(self) -> dict[str, Any]:
        engaged = self.engaged()
        return {
            "engaged": engaged,
            "reason": self.reason() if engaged else None,
            "sentinel_path": str(self.sentinel_path),
        }


def default_killswitch() -> KillSwitch:
    """A KillSwitch on the configured sentinel path, with no cancel hook."""
    return KillSwitch(killswitch_path_from_config())
