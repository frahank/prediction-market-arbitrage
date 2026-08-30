"""Executable safety boundary.

Enforcement layer for the invariants stated in docs/SAFETY.md:

1. No file under ``src/arbx`` may contain a venue order-mutation endpoint
   string ("/portfolio/orders" on Kalshi, "clob.polymarket.com/order" on
   Polymarket CLOB) or combine "POST" with "order". The allowlist is
   deliberately empty; a future live tier would add exactly the two import-guarded
   live-client files and nothing else.
2. ``configs/runtime.yaml`` ships in paper mode with real orders disabled.
3. No module under ``src/arbx`` imports ``arb_bot``, the private predecessor
   codebase this release was extracted from. It must never become a runtime
   dependency; everything needed to run is in this repository.

These tests are the safety net itself; nothing meta-tests them. Keep them
strict: a false positive costs an allowlist entry, a false negative is a
hole in the paper/real boundary.

``tests/test_ui_safety.py`` extends the boundary to the UI seam and holds
the UI-layer import walk and route/Protocol scans, and
``test_tree_scan_covers_ui_and_services_layers`` below proves this file's
tree-scan reaches ``src/arbx/ui`` and ``src/arbx/services`` (templates and
static assets included — the walk is not limited to ``.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_TREE = REPO_ROOT / "src" / "arbx"
RUNTIME_YAML = REPO_ROOT / "configs" / "runtime.yaml"

# Repo-root-relative posix paths allowed to contain order-mutation endpoint
# strings. Exactly one entry today: the read-only Kalshi account client, which
# needs GET /portfolio/orders (listing resting orders — Kalshi's *placement*
# endpoint is POST on the same path). Its read-only-ness is enforced by
# tests/test_accounts_readonly.py: no order-shaped method names anywhere in
# arbx.accounts and no POST/PUT/DELETE strings in its source. A future live tier adds
# exactly "src/arbx/exec/live_kalshi.py" and "src/arbx/exec/live_polymarket.py"
# and nothing else.
ORDER_ENDPOINT_ALLOWLIST: frozenset[str] = frozenset({"src/arbx/accounts/kalshi.py"})

KALSHI_ORDER_ENDPOINT = "/portfolio/orders"
POLYMARKET_ORDER_ENDPOINT = "clob.polymarket.com/order"


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _tree_files() -> list[Path]:
    """Every scannable file under src/arbx (skip bytecode caches)."""
    files = [
        path
        for path in sorted(SRC_TREE.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    ]
    # An empty walk would make this safety net silently worthless.
    assert files, f"safety scan found no files under {SRC_TREE}"
    return files


def test_no_order_endpoints_in_tree():
    violations: list[str] = []
    for path in _tree_files():
        if _rel(path) in ORDER_ENDPOINT_ALLOWLIST:
            continue
        text = path.read_text(errors="replace")
        if KALSHI_ORDER_ENDPOINT in text:
            violations.append(f"{_rel(path)}: contains {KALSHI_ORDER_ENDPOINT!r}")
        if POLYMARKET_ORDER_ENDPOINT in text:
            violations.append(f"{_rel(path)}: contains {POLYMARKET_ORDER_ENDPOINT!r}")
        if "POST" in text and "order" in text.lower():
            violations.append(f'{_rel(path)}: contains "POST" combined with "order"')
    assert not violations, (
        "order-mutation endpoint strings found outside the allowlist "
        "(see docs/SAFETY.md invariant 2):\n" + "\n".join(violations)
    )


def test_tree_scan_covers_ui_and_services_layers():
    """The order-endpoint scan must reach the UI seam.

    If ``src/arbx/ui`` or ``src/arbx/services`` ever fell out of the walk
    (moved, excluded, renamed), the endpoint scan above would silently stop
    guarding the layer closest to the browser.
    """
    scanned = {_rel(path) for path in _tree_files()}
    assert "src/arbx/ui/app.py" in scanned
    assert "src/arbx/services/contracts.py" in scanned
    assert any(name.startswith("src/arbx/ui/templates/") for name in scanned), (
        "tree-scan no longer sees the UI templates"
    )


def test_runtime_config_is_paper():
    config = yaml.safe_load(RUNTIME_YAML.read_text())
    assert config["mode"] == "paper"
    assert config["real_orders"] == 0
    assert config["enable_real_orders"] is False


def test_no_source_repo_imports():
    violations: list[str] = []
    py_files = [path for path in _tree_files() if path.suffix == ".py"]
    assert py_files, f"safety scan found no python modules under {SRC_TREE}"
    for path in py_files:
        module_ast = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(module_ast):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "arb_bot" or name.startswith("arb_bot."):
                    violations.append(f"{_rel(path)}:{node.lineno}: imports {name!r}")
    assert not violations, (
        "modules import the private predecessor codebase (arb_bot must never "
        "be a runtime dependency):\n" + "\n".join(violations)
    )
