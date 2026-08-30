# Scope: BOT_RUNTIME — F-T5: UI-layer extension of the executable safety boundary.
"""Executable proof the UI cannot place an order or flip mode (F-T5).

Extends the P1-T9 boundary (``tests/test_safety_boundary.py``) to the UI seam,
per ``docs/SAFETY.md``:
the UI is a thin facade over named operations; no path may convert a paper
opportunity into a venue order, and no UI route may change mode, enable real
orders, or place an order.

1. No module under ``src/arbx/ui`` or ``src/arbx/services`` imports
   ``arbx.venues``, ``arbx.capture``, or (future) ``arbx.exec.live_*``
   directly — services compose them behind the seam, the UI layer never does.
2. No API route performs order placement or mode mutation: route-name scan of
   the full app plus method-name scan of the ``LiveController`` Protocol and
   the seam-operation table.

These tests are part of the safety net itself; nothing meta-tests them.
"""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from arbx.services.contracts import LiveController, iter_seam_operations
from arbx.ui.app import ServiceRegistry, create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
UI_LAYER_DIRS = (SRC_ROOT / "arbx" / "ui", SRC_ROOT / "arbx" / "services")

# The UI/services layer must reach venues and capture only through the
# services *behind* the seam (e.g. arbx.scanner), never directly; the gated
# live clients must never be reachable from this layer at all.
FORBIDDEN_IMPORT_ROOTS = ("arbx.venues", "arbx.capture")
FORBIDDEN_LIVE_PREFIX = "arbx.exec.live_"

# No route path or operation/method name may look like order placement or
# mode mutation. "mode" covers set_mode-style routes; get_app_status reports
# mode read-only without carrying the word in its path.
FORBIDDEN_ROUTE_TERMS = ("order", "place", "trade", "mode", "enable")
FORBIDDEN_METHOD_TERMS = ("place", "order", "trade", "amend", "cancel", "set_mode")


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _ui_layer_modules() -> list[Path]:
    files = [
        path
        for layer_dir in UI_LAYER_DIRS
        for path in sorted(layer_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    # An empty walk would make this safety net silently worthless.
    assert files, "safety scan found no python modules under src/arbx/{ui,services}"
    return files


def _package_parts(path: Path) -> list[str]:
    """Dotted package the module lives in, for resolving relative imports.

    Dropping the last part is right for both cases: a regular module drops its
    own name; ``__init__.py`` drops ``__init__``, leaving the package itself.
    """
    return list(path.relative_to(SRC_ROOT).with_suffix("").parts)[:-1]


def _imported_names(path: Path) -> list[tuple[int, str]]:
    """Every absolute module name a file imports, including ``from X import y``
    targets (``y`` may itself be the submodule ``X.y``) and resolved relative
    imports."""
    module_ast = ast.parse(path.read_text(), filename=str(path))
    package = _package_parts(path)
    names: list[tuple[int, str]] = []
    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
            else:
                base = []
            base_module = ".".join(base + (node.module.split(".") if node.module else []))
            if base_module:
                names.append((node.lineno, base_module))
            for alias in node.names:
                if base_module and alias.name != "*":
                    names.append((node.lineno, f"{base_module}.{alias.name}"))
    return names


def _is_forbidden_import(name: str) -> bool:
    for root in FORBIDDEN_IMPORT_ROOTS:
        if name == root or name.startswith(root + "."):
            return True
    return name.startswith(FORBIDDEN_LIVE_PREFIX)


def test_ui_layer_never_imports_venues_capture_or_live_clients():
    violations: list[str] = []
    for path in _ui_layer_modules():
        for lineno, name in _imported_names(path):
            if _is_forbidden_import(name):
                violations.append(f"{_rel(path)}:{lineno}: imports {name!r}")
    assert not violations, (
        "the UI/services layer imports venue/capture/live internals directly "
        "(services compose them behind the seam; see docs/SAFETY.md):\n"
        + "\n".join(violations)
    )


def test_no_route_places_orders_or_mutates_mode():
    app = create_app(ServiceRegistry())
    violations: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        for term in FORBIDDEN_ROUTE_TERMS:
            if term in path.lower():
                violations.append(f"route {path!r} contains forbidden term {term!r}")
    assert not violations, "\n".join(violations)


def test_live_controller_protocol_cannot_place_or_flip_mode():
    method_names = {
        name
        for name, value in vars(LiveController).items()
        if callable(value) and not name.startswith("_")
    }
    assert method_names, "LiveController Protocol scan found no methods"
    violations = [
        f"LiveController.{name} matches forbidden term {term!r}"
        for name in sorted(method_names)
        for term in FORBIDDEN_METHOD_TERMS
        if term in name.lower()
    ]
    assert not violations, "\n".join(violations)


def test_seam_operations_have_no_order_or_mode_names():
    violations = [
        f"seam operation {op.name!r} matches forbidden term {term!r}"
        for op in iter_seam_operations()
        for term in FORBIDDEN_METHOD_TERMS + ("mode", "enable")
        if term in op.name.lower()
    ]
    assert not violations, "\n".join(violations)


# --- Local-only request boundary (cross-origin writes) -----------------------
#
# The cockpit binds loopback, but binding is not a boundary: a page in the
# operator's browser can reach 127.0.0.1, and a hostile name rebound to
# 127.0.0.1 can reach it with its own Host header. These tests pin both checks
# plus the JSON-body requirement that denies a write the no-preflight path.


def _safety_client() -> TestClient:
    return TestClient(create_app(ServiceRegistry()))


def test_cross_origin_write_is_rejected():
    response = _safety_client().post(
        "/api/save_note",
        headers={"Origin": "https://evil.example", "content-type": "application/json"},
        json={"name": "csrf_poc", "markdown": "written cross-origin"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_non_loopback_host_is_rejected():
    response = _safety_client().get(
        "/api/get_app_status",
        headers={"Host": "rebound.evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_same_origin_write_is_accepted():
    response = _safety_client().post(
        "/api/save_note",
        headers={"Origin": "http://127.0.0.1:8710", "content-type": "application/json"},
        json={"name": "ok_note", "markdown": "same origin"},
    )
    assert response.status_code == 200


def test_query_params_do_not_drive_writes():
    # A bodyless form POST is the CORS "simple request" that skips preflight;
    # writes must ignore the query string entirely so it carries no payload.
    response = _safety_client().post(
        "/api/save_note?name=csrf_poc&markdown=written_by_query_string",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_write_rejects_non_json_content_type():
    response = _safety_client().post(
        "/api/save_note",
        headers={"content-type": "text/plain"},
        content=b'{"name": "csrf_poc", "markdown": "x"}',
    )
    assert response.json()["ok"] is False
    assert response.json()["error"]["code"] == "invalid_request"
