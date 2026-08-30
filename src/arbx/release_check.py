"""Fast, offline validation used by ``./run --check`` and CI."""
from __future__ import annotations

from pathlib import Path

import yaml

from arbx.launcher import REPO_ROOT, build_services, load_ui_config
from arbx.pairs.registry import (
    SCANNABLE_STATUSES,
    load_pairs,
    verify_registry_integrity,
)
from arbx.ui.app import STATIC_DIR, TEMPLATES_DIR, create_app


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def check_release(repo_root: Path = REPO_ROOT) -> dict[str, int | str]:
    runtime_path = repo_root / "configs" / "runtime.yaml"
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
    _require(runtime.get("mode") == "paper", "runtime mode must remain paper")
    _require(runtime.get("real_orders") == 0, "real_orders must remain zero")
    _require(
        runtime.get("enable_real_orders") is False,
        "enable_real_orders must remain false",
    )

    approved_path = repo_root / "configs" / "pairs.approved.yaml"
    archived_path = repo_root / "configs" / "pairs.archived.yaml"
    for path in (approved_path, archived_path):
        integrity = verify_registry_integrity(path)
        _require(integrity.audited, f"registry integrity failed: {path.name}")

    pairs = load_pairs(approved_path)
    _require(bool(pairs), "approved registry contains no pairs")
    scannable = [pair for pair in pairs if pair.status in SCANNABLE_STATUSES]
    _require(bool(scannable), "approved registry contains no scannable paper pairs")
    _require(TEMPLATES_DIR.is_dir(), "packaged UI templates are missing")
    _require(STATIC_DIR.is_dir(), "packaged UI static assets are missing")

    app = create_app(build_services(load_ui_config()))
    routes = {getattr(route, "path", "") for route in app.routes}
    for route in ("/paper", "/pairs", "/data", "/docs-viewer", "/live"):
        _require(route in routes, f"UI route missing: {route}")

    # Three counts, because they answer three different questions. The old
    # single ``eligible_pairs`` reported the raw registry length under a name
    # that implied a filter, which let a registry nothing could scan still
    # report a healthy number.
    return {
        "mode": "paper",
        "registry_pairs": len(pairs),
        "scannable_pairs": len(scannable),
        "strategy_eligible_pairs": sum(1 for pair in pairs if pair.include_in_strategy_metrics),
        "ui_routes": 5,
    }


def main() -> int:
    result = check_release()
    print(
        "release check passed: "
        f"mode={result['mode']} registry={result['registry_pairs']} "
        f"scannable={result['scannable_pairs']} "
        f"strategy_eligible={result['strategy_eligible_pairs']} "
        f"ui_routes={result['ui_routes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
