# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Read-only account-state contract. No order capability by type.
"""AccountClient: the whole authenticated account surface this repo permits.

Four read-only views — balance, positions, open orders, fills. There is
deliberately no place/cancel/amend anywhere in this Protocol or in any class
implementing it; ``tests/test_accounts_readonly.py`` scans every class under
``arbx.accounts`` and fails the suite if an order-shaped method ever appears.
Execution (if it is ever unfrozen) is a separate gated track, not an extension
of these clients.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# Method-name fragments no arbx.accounts class may ever expose. Consumed by
# the read-only-by-type scan test; listed here so the contract and its
# enforcement share one source of truth.
FORBIDDEN_ACCOUNT_METHOD_TERMS = (
    "place",
    "create",
    "submit",
    "cancel",
    "amend",
    "trade",
    "buy",
    "sell",
    "order",  # open_orders (read) is the one allowed exception, see the test
    "withdraw",
    "transfer",
)
ALLOWED_ACCOUNT_METHODS_WITH_TERM = ("open_orders",)


@runtime_checkable
class AccountClient(Protocol):
    """Read-only authenticated account state for one venue."""

    venue: str

    async def balance_usd(self) -> float: ...

    async def positions(self) -> list[dict[str, Any]]: ...

    async def open_orders(self) -> list[dict[str, Any]]: ...

    async def fills(self, since: datetime | None = None) -> list[dict[str, Any]]: ...
