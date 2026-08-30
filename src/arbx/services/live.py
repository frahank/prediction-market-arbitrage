# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Read-only access and runtime-safety service.
"""Storage-only access and safety facade for the local dashboard.

This service deliberately does not know how to trade. It exposes only the
already-built safety surfaces: runtime status, the global kill switch, and
credential storage/status.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from arbx.accounts.secrets import (
    CredentialError,
    credential_status,
)
from arbx.accounts.secrets import (
    store_credentials as write_credentials,
)
from arbx.accounts.verify import verify_access
from arbx.core.mode import (
    DEFAULT_RUNTIME_YAML,
    ENABLE_ENV_VAR,
    RuntimeMode,
    current_mode,
)
from arbx.core.redact import redact_text
from arbx.exec.killswitch import KillSwitch
from arbx.ui.envelope import OpError


def _runtime_config(path: Path = DEFAULT_RUNTIME_YAML) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _credential_row(venue: str, profile: str) -> dict[str, Any] | None:
    for row in credential_status():
        if row["venue"] == venue and row["profile"] == profile:
            return dict(row)
    return None


class LiveControllerImpl:
    """Implements the inert Module 1 scaffold contract."""

    def __init__(
        self,
        killswitch: KillSwitch,
        *,
        runtime_yaml: Path = DEFAULT_RUNTIME_YAML,
        access_prober: Callable[[], dict[str, Any]] | None = None,
        access_cache_ttl_s: float = 60.0,
    ) -> None:
        self.killswitch = killswitch
        self.runtime_yaml = Path(runtime_yaml)
        # verify_access does one authenticated read when credentials exist;
        # the TTL keeps UI polling from hammering the venue.
        self._access_prober = access_prober or verify_access
        self._access_cache_ttl_s = access_cache_ttl_s
        self._access_cache: dict[str, Any] | None = None
        self._access_cached_at = 0.0

    def _accounts(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self._access_cache is None
            or now - self._access_cached_at >= self._access_cache_ttl_s
        ):
            try:
                self._access_cache = self._access_prober()
            except Exception as exc:  # noqa: BLE001 - status must never break the tab
                detail = redact_text(str(exc)) or "access check failed"
                self._access_cache = {
                    venue: {"connected": False, "read_only": True, "profile": None, "detail": detail}
                    for venue in ("kalshi", "polymarket")
                }
            self._access_cached_at = now
        return self._access_cache

    def get_live_status(self) -> dict[str, Any] | OpError:
        config = _runtime_config(self.runtime_yaml)
        mode = current_mode(self.runtime_yaml)
        env_gate = os.environ.get(ENABLE_ENV_VAR) == "1"
        enable_flag = config.get("enable_real_orders") is True
        return {
            "mode": mode.value,
            "real_orders_enabled": mode is RuntimeMode.LIVE and enable_flag and env_gate,
            "killswitch": self.killswitch.status(),
            "gates": {
                "config_live": mode is RuntimeMode.LIVE,
                "enable_flag": enable_flag,
                "env_flag": env_gate,
            },
            "credential_status": credential_status(),
            "accounts": self._accounts(),
        }

    async def engage_killswitch(self, reason: str) -> dict[str, Any] | OpError:
        reason_text = str(reason or "").strip()
        if not reason_text:
            return OpError("invalid_request", "kill-switch reason is required")
        await self.killswitch.engage(reason_text)
        return self.killswitch.status()

    def store_credentials(self, venue: str, profile: str, fields: dict[str, Any]) -> dict[str, Any] | OpError:
        try:
            write_credentials(str(venue), str(profile), fields)
        except CredentialError as exc:
            return OpError("invalid_request", redact_text(str(exc)))
        row = _credential_row(str(venue), str(profile))
        if row is None:
            return OpError("internal_error", "credential status unavailable after save")
        return row
