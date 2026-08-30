# Scope: BOT_RUNTIME — Read-only-by-type enforcement for every account client.
"""HARD RAIL: no class under ``arbx.accounts`` may ever grow an order-shaped
method. This scan imports every module in the package (so a new client is
covered the day it is added), collects every class defined there, and fails on
any public method whose name contains an order/transfer term.

``open_orders`` (a read) is the single allowed exception, whitelisted by exact
name in ``arbx.accounts.types.ALLOWED_ACCOUNT_METHODS_WITH_TERM``.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import arbx.accounts
from arbx.accounts.types import (
    ALLOWED_ACCOUNT_METHODS_WITH_TERM,
    FORBIDDEN_ACCOUNT_METHOD_TERMS,
    AccountClient,
)


def _account_classes():
    classes = []
    for module_info in pkgutil.walk_packages(
        arbx.accounts.__path__, prefix="arbx.accounts."
    ):
        module = importlib.import_module(module_info.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__.startswith("arbx.accounts"):
                classes.append(cls)
    return classes


def _violations(cls) -> list[str]:
    found = []
    for name, value in inspect.getmembers(cls):
        if name.startswith("_") or not callable(value):
            continue
        if name in ALLOWED_ACCOUNT_METHODS_WITH_TERM:
            continue
        for term in FORBIDDEN_ACCOUNT_METHOD_TERMS:
            if term in name.lower():
                found.append(f"{cls.__module__}.{cls.__name__}.{name} contains {term!r}")
    return found


def test_no_account_class_exposes_order_capability():
    classes = _account_classes()
    # Non-vacuous: the scan must actually see the real client and protocol.
    names = {cls.__name__ for cls in classes}
    assert "KalshiAccountClient" in names
    assert AccountClient in classes or "AccountClient" in names
    violations = [v for cls in classes for v in _violations(cls)]
    assert not violations, (
        "account classes must stay read-only by type:\n" + "\n".join(violations)
    )


def test_account_source_has_no_mutating_http_verbs():
    """The account package must never issue POST/PUT/DELETE — reads only."""
    from pathlib import Path

    package_dir = Path(arbx.accounts.__path__[0])
    offenders = []
    for path in sorted(package_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for verb in ('"POST"', "'POST'", '"PUT"', "'PUT'", '"DELETE"', "'DELETE'"):
            if verb in text:
                offenders.append(f"{path.name}: {verb}")
    assert not offenders, "mutating HTTP verbs in arbx.accounts:\n" + "\n".join(offenders)
