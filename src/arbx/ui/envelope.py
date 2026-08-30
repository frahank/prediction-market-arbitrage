# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Standard UI operation envelope.
"""The response envelope used by every named UI operation.

Every named operation returns exactly this shape:

    {"ok": true, "data": {...}, "error": null,
     "meta": {"schema_version": 1, "generated_at": "<ISO-8601 UTC>"}}

Errors use ``ok: false``, no partial mutation, a stable machine code, and a
safe message — never provider internals, secrets, or tracebacks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class OpError:
    code: str          # stable machine code, e.g. "not_found", "invalid_request",
                       # "not_implemented", "conflict", "safety_violation"
    message: str       # safe, human-readable; never provider internals or secrets
    details: dict[str, Any] | None = None


def envelope(data: Any = None, *, error: OpError | None = None) -> dict[str, Any]:
    """Wrap an operation result (or :class:`OpError`) in the standard envelope."""
    return {
        "ok": error is None,
        "data": data,
        "error": asdict(error) if error is not None else None,
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
