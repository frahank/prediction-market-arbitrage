# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
"""Local-only UI composition root used by the script and console entry point."""
from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from arbx.exec.killswitch import KillSwitch, killswitch_path_from_config
from arbx.services.analysis import AnalysisServiceImpl
from arbx.services.datastore import DataServiceImpl, SoakStoreImpl
from arbx.services.docs import DocStoreImpl, NotesStoreImpl
from arbx.services.live import LiveControllerImpl
from arbx.services.pairs import PairRegistryServiceImpl
from arbx.services.scanner import ScannerControllerImpl
from arbx.services.testsuite import TestSuiteRunnerImpl
from arbx.ui.app import ServiceRegistry, create_app


def discover_repo_root(start: Path | None = None) -> Path:
    """Find the checkout containing the runtime configs.

    ``ARBX_REPO_ROOT`` is an explicit override for wrappers and tests. Otherwise
    the current directory and editable-install source tree are checked. The
    public launcher deliberately refuses to guess when no checkout is present;
    this application is distributed as a clone-and-run research repository.
    """
    candidates: list[Path] = []
    override = os.environ.get("ARBX_REPO_ROOT")
    if override:
        candidates.append(Path(override).expanduser())
    if start is not None:
        candidates.append(start)
    candidates.extend((Path.cwd(), Path(__file__).resolve().parents[2]))

    checked: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in checked:
            continue
        checked.add(resolved)
        if (resolved / "configs" / "ui.yaml").is_file() and (
            resolved / "configs" / "runtime.yaml"
        ).is_file():
            return resolved
    raise RuntimeError(
        "arbx must be launched from its cloned repository root "
        "(or with ARBX_REPO_ROOT set to that directory)"
    )


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@functools.cache
def repo_root() -> Path:
    """The checkout this process is serving, discovered once and cached.

    Deliberately a function rather than a module constant: importing this
    module must not require a checkout, and must not raise. Callers that need
    the root ask for it, and only then can discovery fail.
    """
    return discover_repo_root()


def default_ui_yaml() -> Path:
    return repo_root() / "configs" / "ui.yaml"


def load_ui_config(path: Path | None = None) -> dict[str, Any]:
    data = yaml.safe_load((path or default_ui_yaml()).read_text())
    return data if isinstance(data, dict) else {}


def enforce_localhost(host: str) -> str:
    normalized = str(host).strip().lower()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError("UI host must be localhost or a loopback address")
    return normalized


def load_depth_haircut(path: Path | None = None) -> float:
    """`executable.depth_haircut` from the modeling config (0.5 fallback)."""
    try:
        path = path or repo_root() / "configs" / "modeling.yaml"
        modeling = yaml.safe_load(path.read_text())
        return float(modeling["executable"]["depth_haircut"])
    except (OSError, KeyError, TypeError, ValueError):
        return 0.5


def load_scanner_defaults(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError:
        data = {}
    config = data if isinstance(data, dict) else {}
    try:
        batch_size = int(config.get("default_batch_size", 20))
    except (TypeError, ValueError):
        batch_size = 20
    try:
        tick_s = float(config.get("default_tick_s", 1.0))
    except (TypeError, ValueError):
        tick_s = 1.0
    return {
        "batch_size": max(1, batch_size),
        "tick_s": max(0.001, tick_s),
    }


def _repo_path(value: Any, *, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a repository-relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{key} must be a repository-relative path")
    return repo_root() / path


def build_services(config: dict[str, Any] | None = None) -> ServiceRegistry:
    ui_config = config if config is not None else load_ui_config()
    docs_roots = ui_config.get("docs_roots", ["docs", "README.md"])
    if not isinstance(docs_roots, list) or not all(isinstance(item, str) for item in docs_roots):
        raise ValueError("docs_roots must be a list of repository-relative paths")
    notes_dir_config = ui_config.get("notes_dir", "docs/notes")
    if not isinstance(notes_dir_config, str) or not notes_dir_config:
        raise ValueError("notes_dir must be a repository-relative path")
    notes_dir_path = Path(notes_dir_config)
    if notes_dir_path.is_absolute() or ".." in notes_dir_path.parts:
        raise ValueError("notes_dir must be a repository-relative path")
    notes_dir = repo_root() / notes_dir_path
    soaks_root_config = ui_config.get("soaks_root", "data/soaks")
    if not isinstance(soaks_root_config, str) or not soaks_root_config:
        raise ValueError("soaks_root must be a repository-relative path")
    soaks_root_path = Path(soaks_root_config)
    if soaks_root_path.is_absolute() or ".." in soaks_root_path.parts:
        raise ValueError("soaks_root must be a repository-relative path")
    legacy_roots_config = ui_config.get("legacy_soak_roots", [])
    if not isinstance(legacy_roots_config, list) or not all(isinstance(item, str) for item in legacy_roots_config):
        raise ValueError("legacy_soak_roots must be a list of paths")
    legacy_roots = [Path(item) if Path(item).is_absolute() else repo_root() / item for item in legacy_roots_config]
    soak_store = SoakStoreImpl(repo_root() / soaks_root_path, legacy_roots)
    approved_path = _repo_path(
        ui_config.get("pairs_approved_path", "configs/pairs.approved.yaml"),
        key="pairs_approved_path",
    )
    candidates_path = _repo_path(
        ui_config.get("pairs_candidates_path", "configs/pairs.candidates.yaml"),
        key="pairs_candidates_path",
    )
    imported_paths_config = ui_config.get("pairs_imported_paths", [])
    if not isinstance(imported_paths_config, list) or not all(
        isinstance(item, str) for item in imported_paths_config
    ):
        raise ValueError("pairs_imported_paths must be a list of repository-relative paths")
    imported_paths = [
        _repo_path(item, key="pairs_imported_paths") for item in imported_paths_config
    ]
    archived_path = _repo_path(
        ui_config.get("pairs_archived_path", "configs/pairs.archived.yaml"),
        key="pairs_archived_path",
    )
    evidence_root = _repo_path(
        ui_config.get("evidence_root", "evidence"),
        key="evidence_root",
    )
    scanner_config_path = _repo_path(
        ui_config.get("scanner_config_path", "configs/scanner.yaml"),
        key="scanner_config_path",
    )
    # One kill switch, whole system: engage() stops any running scanner, the
    # scanner refuses to start while engaged, and get_app_status surfaces it.
    # The switch is constructed first because the controller needs it, then the
    # cancel hook is attached once the controller exists - so nothing here
    # depends on a name that is not yet bound.
    killswitch = KillSwitch(
        killswitch_path_from_config(repo_root() / "configs" / "runtime.yaml"),
    )
    scanner_controller = ScannerControllerImpl(
        repo_root=repo_root(),
        pair_registry_path=approved_path,
        soak_store=soak_store,
        config_path=scanner_config_path,
        killswitch=killswitch,
    )

    async def _stop_scanner_on_kill() -> None:
        scanner_controller.stop_scanner()  # "no active scanner" OpError is fine

    killswitch.cancel_all = _stop_scanner_on_kill
    return ServiceRegistry(
        doc_store=DocStoreImpl(repo_root(), docs_roots),
        notes_store=NotesStoreImpl(notes_dir),
        data_service=DataServiceImpl(soak_store, depth_haircut=load_depth_haircut()),
        pair_registry_service=PairRegistryServiceImpl(
            approved_path,
            candidates_path,
            archived_path,
            evidence_root,
            extra_candidates_paths=imported_paths,
        ),
        scanner_controller=scanner_controller,
        analysis_service=AnalysisServiceImpl(
            soak_store,
            repo_root() / "reports" / "analysis_jobs",
            registry_path=approved_path,
            modeling_path=repo_root() / "configs" / "modeling.yaml",
        ),
        test_suite_runner=TestSuiteRunnerImpl(
            repo_root(),
            repo_root() / "reports" / "test_runs",
            scanner_status=scanner_controller.get_scanner_status,
        ),
        live_controller=LiveControllerImpl(killswitch),
        killswitch=killswitch,
        paper_defaults=load_scanner_defaults(scanner_config_path),
    )


def create_default_app():
    """Build the cockpit app from the repository this process is running in.

    Importing this module deliberately builds nothing: the service graph, the
    kill switch, and the soak store are all constructed here, on demand. Use
    with an ASGI server's factory mode, e.g.
    ``uvicorn arbx.launcher:create_default_app --factory``.
    """
    return create_app(build_services())


def main() -> None:
    config = load_ui_config()
    host = enforce_localhost(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 8710))
    uvicorn.run(create_app(build_services(config)), host=host, port=port, reload=False)

