# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# FastAPI shell and facade operation wrapper.
"""Five-tab local cockpit shell.

The UI layer is intentionally thin: page routes render templates, and API
routes call named operation handlers through the standard envelope. Concrete
service protocols and stubs live in ``arbx.services``; this module keeps the registry
shape ready without importing backend internals.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from arbx.core.mode import current_mode, real_orders_enabled
from arbx.exec.killswitch import KillSwitch, default_killswitch
from arbx.services.contracts import (
    AnalysisService,
    DataService,
    DocStore,
    LiveController,
    NotesStore,
    PairRegistryService,
    ScannerController,
    TestSuiteRunner,
    iter_seam_operations,
)
from arbx.services.stubs import (
    StubAnalysisService,
    StubDataService,
    StubDocStore,
    StubLiveController,
    StubNotesStore,
    StubPairRegistryService,
    StubScannerController,
    StubTestSuiteRunner,
)
from arbx.ui.envelope import SCHEMA_VERSION, OpError, envelope

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"

PAGE_ROUTES = {
    "/live": ("live.html", "live", "Access & Safety"),
    "/paper": ("paper.html", "paper", "Paper"),
    "/pairs": ("pairs.html", "pairs", "Pairs"),
    "/data": ("data.html", "data", "Data"),
    "/docs-viewer": ("docs.html", "docs", "Documents"),
}

API_PREFIX = "/api/"

# The cockpit is a single-user tool bound to loopback (``enforce_localhost`` in
# ``arbx.launcher``). Binding alone is not a boundary, though: a page in the
# operator's browser can still reach 127.0.0.1, and a hostile name that resolves
# to 127.0.0.1 (DNS rebinding) can reach it with an off-host ``Host`` header.
# These two header checks close both routes without introducing a login.
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Every template loads its JavaScript from a separate file under /static and no
# template carries an inline <script>, style attribute, or on* handler, so the
# policy needs no 'unsafe-inline' escape hatch. Keep it that way: the docs tab
# assigns server-rendered markdown to innerHTML, and this is the backstop that
# makes that assignment safe even if the renderer's html=False were ever lost.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(slots=True)
class ServiceRegistry:
    """Composition-root registry for swappable backend services."""

    doc_store: DocStore = field(default_factory=StubDocStore)
    notes_store: NotesStore = field(default_factory=StubNotesStore)
    data_service: DataService = field(default_factory=StubDataService)
    pair_registry_service: PairRegistryService = field(default_factory=StubPairRegistryService)
    scanner_controller: ScannerController = field(default_factory=StubScannerController)
    analysis_service: AnalysisService = field(default_factory=StubAnalysisService)
    test_suite_runner: TestSuiteRunner = field(default_factory=StubTestSuiteRunner)
    live_controller: LiveController = field(default_factory=StubLiveController)
    # One kill switch, whole system: the composition root shares this exact
    # instance with the scanner controller (and later the live controller).
    killswitch: KillSwitch = field(default_factory=default_killswitch)
    paper_defaults: dict[str, Any] = field(
        default_factory=lambda: {"batch_size": 20, "tick_s": 1.0},
    )


def _safe_error(exc: Exception) -> OpError:
    if isinstance(exc, ValueError):
        return OpError("invalid_request", str(exc) or "invalid request")
    if isinstance(exc, TypeError):
        return OpError("invalid_request", "invalid request")
    if isinstance(exc, KeyError):
        return OpError("not_found", "requested resource was not found")
    return OpError("internal_error", "operation failed")


def _accepted_parameters(handler: Callable[..., Any]) -> frozenset[str] | None:
    """Names ``handler`` accepts, or ``None`` when it takes ``**kwargs``.

    Request keys are splatted into the handler, so without this every keyword
    argument of every service method would be settable by name from the query
    string. Computed once at registration.
    """
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):  # builtins and C callables
        return None
    names: set[str] = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.add(parameter.name)
    return frozenset(names)


async def _call_handler(
    handler: Callable[..., Any],
    request: Request,
    method: str,
    accepted: frozenset[str] | None = None,
) -> Any:
    if method.upper() == "GET":
        params: dict[str, Any] = dict(request.query_params)
    else:
        # Writes are JSON-body only. Query parameters are deliberately ignored:
        # a write drivable entirely from the query string needs no request body,
        # and a bodyless cross-origin form submission is a CORS "simple request"
        # that never triggers a preflight. Requiring a JSON body — and the matching
        # content type — forces the preflight that this app, which serves no
        # CORS headers, always fails.
        params = {}
        body = await request.body()
        if body:
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("write operations require Content-Type: application/json")
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            params.update(payload)

    if accepted is not None:
        unknown = sorted(set(params) - accepted)
        if unknown:
            raise ValueError(f"unknown parameter: {unknown[0]}")

    result = handler(**params) if params else handler()
    if inspect.isawaitable(result):
        return await result
    return result


def register_op(
    app: FastAPI,
    name: str,
    handler: Callable[..., Any],
    *,
    method: str = "GET",
) -> None:
    """Expose one named operation under ``/api/`` and wrap it in an envelope."""
    path = f"{API_PREFIX}{name}"
    accepted = _accepted_parameters(handler)

    async def endpoint(request: Request) -> dict[str, Any]:
        try:
            result = await _call_handler(handler, request, method, accepted)
        except Exception as exc:  # noqa: BLE001 - public API boundary sanitizes all exceptions
            return envelope(error=_safe_error(exc))
        if isinstance(result, OpError):
            return envelope(error=result)
        return envelope(result)

    app.add_api_route(path, endpoint, methods=[method.upper()])


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=envelope(error=OpError("forbidden", message)),
        headers=dict(SECURITY_HEADERS),
    )


def _is_loopback_hostname(hostname: str | None) -> bool:
    return (hostname or "").strip().strip("[]").lower() in LOOPBACK_HOSTNAMES


def register_local_only_guard(app: FastAPI) -> None:
    """Reject requests that did not originate from this machine's own browser.

    ``Host`` is checked on every request: a rebound DNS name resolving to
    127.0.0.1 arrives with its own hostname here, and rejecting it keeps read
    operations private too. ``Origin`` is checked whenever the browser sends one
    — it is absent for same-origin navigations and for non-browser clients such
    as curl, and a page cannot forge it — so any cross-origin value is refused.
    """

    @app.middleware("http")
    async def guard_local_only(request: Request, call_next: Callable[..., Any]) -> Any:
        # Parsed as an authority so that "127.0.0.1:8710" and "[::1]:8710" both
        # reduce to a bare hostname.
        host_header = request.headers.get("host", "")
        if not _is_loopback_hostname(urlsplit(f"//{host_header}").hostname):
            return _forbidden("requests must address this cockpit as a loopback host")

        origin = request.headers.get("origin")
        if origin is not None and not _is_loopback_hostname(urlsplit(origin).hostname):
            return _forbidden("cross-origin requests are not accepted")

        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def _app_status(killswitch: KillSwitch) -> dict[str, Any]:
    engaged = killswitch.engaged()
    return {
        "mode": current_mode().value,
        "real_orders_enabled": real_orders_enabled(),
        "killswitch_engaged": engaged,
        "killswitch_reason": killswitch.reason() if engaged else None,
        "schema_version": SCHEMA_VERSION,
    }


def _register_service_operations(app: FastAPI, registry: ServiceRegistry) -> None:
    for op in iter_seam_operations():
        if op.service_attr == "facade":
            continue
        service = getattr(registry, op.service_attr)
        register_op(app, op.name, getattr(service, op.name), method=op.method)


def create_app(services: ServiceRegistry | None = None) -> FastAPI:
    registry = services or ServiceRegistry()
    app = FastAPI(
        title="arbx local cockpit",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = registry
    register_local_only_guard(app)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/paper", status_code=307)

    for route_path, (template_name, page, title) in PAGE_ROUTES.items():

        async def page_route(
            request: Request,
            template_name: str = template_name,
            page: str = page,
            title: str = title,
        ) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                template_name,
                {
                    "page": page,
                    "title": title,
                    "nav_items": (
                        ("live", "Access & Safety", "/live"),
                        ("paper", "Paper", "/paper"),
                        ("pairs", "Pairs", "/pairs"),
                        ("data", "Data", "/data"),
                        ("docs", "Documents", "/docs-viewer"),
                    ),
                    "status_poll_ms": 5000,
                    "schema_version": SCHEMA_VERSION,
                    "paper_defaults": registry.paper_defaults,
                },
            )

        app.add_api_route(route_path, page_route, methods=["GET"], include_in_schema=False)

    register_op(app, "get_app_status", lambda: _app_status(registry.killswitch))
    _register_service_operations(app, registry)
    return app
