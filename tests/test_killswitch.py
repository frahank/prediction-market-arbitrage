# KillSwitch contract and global wiring.
"""The kill switch is the operator's one hard off-switch.

These tests pin the kill-switch contract verbatim: sentinel-or-env engagement,
atomic sentinel write followed by ``cancel_all()``, the no-``clear()``
invariant, scanner start refusal, and the app-status surface. All tests use
tmp sentinel paths only — never the real ``~/.arbx/KILL``.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

import arbx.exec.killswitch as killswitch_module
from arbx.exec.killswitch import KILL_ENV_VAR, KillSwitch, KillSwitchEngaged
from arbx.pairs.registry import write_registry_integrity
from arbx.services.datastore import SoakStoreImpl
from arbx.services.scanner import ScannerControllerImpl
from arbx.ui.app import ServiceRegistry, create_app
from arbx.ui.envelope import OpError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "arbx"

# Any name suggesting an un-engage path. "unlink"/"remove"/"delete" catch
# helpers that would delete the sentinel programmatically.
FORBIDDEN_NAME_FRAGMENTS = (
    "clear",
    "reset",
    "disengage",
    "unengage",
    "un_engage",
    "release",
    "remove",
    "delete",
    "unlink",
)


def _sentinel(tmp_path: Path) -> Path:
    return tmp_path / "arbx-test" / "KILL"


def test_sentinel_or_env_engages(tmp_path, monkeypatch):
    monkeypatch.delenv(KILL_ENV_VAR, raising=False)
    sentinel = _sentinel(tmp_path)
    switch = KillSwitch(sentinel)

    assert switch.engaged() is False
    switch.check_or_raise()  # must not raise

    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("manual\n")
    assert switch.engaged() is True
    try:
        switch.check_or_raise()
    except KillSwitchEngaged:
        pass
    else:
        raise AssertionError("check_or_raise must raise while the sentinel exists")

    sentinel.unlink()  # the operator's manual rm — the only way to clear
    assert switch.engaged() is False

    monkeypatch.setenv(KILL_ENV_VAR, "1")
    assert switch.engaged() is True
    monkeypatch.setenv(KILL_ENV_VAR, "0")
    assert switch.engaged() is False


def test_engage_writes_atomically_and_calls_cancel_all(tmp_path, monkeypatch):
    monkeypatch.delenv(KILL_ENV_VAR, raising=False)
    sentinel = _sentinel(tmp_path)

    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: Any, dst: Any) -> None:
        replace_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(killswitch_module.os, "replace", spy_replace)

    seen: list[dict[str, Any]] = []

    async def cancel_all() -> None:
        # The sentinel must already be durably on disk before cancel_all runs,
        # and it must carry the reason (no partially-written file).
        assert sentinel.exists()
        seen.append({"reason_at_cancel": KillSwitch(sentinel).reason()})

    switch = KillSwitch(sentinel, cancel_all=cancel_all)
    asyncio.run(switch.engage("unit-test reason"))

    assert len(seen) == 1
    assert seen[0]["reason_at_cancel"] == "unit-test reason"
    assert switch.engaged() is True
    assert switch.reason() == "unit-test reason"
    # Atomic pattern: exactly one os.replace onto the sentinel, no temp litter.
    assert [dst for _src, dst in replace_calls] == [str(sentinel)]
    assert [entry.name for entry in sentinel.parent.iterdir()] == ["KILL"]

    # engage() without a cancel hook still writes the sentinel.
    other = _sentinel(tmp_path).with_name("KILL2")
    asyncio.run(KillSwitch(other).engage("no hook"))
    assert other.exists()


def test_no_clear_method_exists():
    offenders = [
        name
        for name in dir(KillSwitch)
        for fragment in FORBIDDEN_NAME_FRAGMENTS
        if fragment in name.lower() and not (name.startswith("__") and name.endswith("__"))
    ]
    assert not offenders, f"KillSwitch exposes un-engage-shaped attributes: {offenders}"

    module_offenders = [
        name
        for name in vars(killswitch_module)
        for fragment in FORBIDDEN_NAME_FRAGMENTS
        if fragment in name.lower() and not name.startswith("__")
    ]
    assert not module_offenders, (
        f"arbx.exec.killswitch exposes un-engage-shaped names: {module_offenders}"
    )

    # No code anywhere under src/arbx may clear a kill switch or delete its
    # sentinel: scan every module for `<killswitch-ish>.clear(...)`-shaped
    # calls and for sentinel deletion.
    clear_pattern = re.compile(
        r"(killswitch|kill_switch)\s*\.\s*(clear|reset|disengage|unengage|release)\s*\(",
        re.IGNORECASE,
    )
    sentinel_delete_pattern = re.compile(r"sentinel_path\s*\.\s*(unlink|rmdir)\s*\(")
    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for pattern in (clear_pattern, sentinel_delete_pattern):
            match = pattern.search(text)
            if match:
                violations.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)!r}")
    assert not violations, "code paths that clear the kill switch exist:\n" + "\n".join(violations)


def _write_scanner_fixture(tmp_path: Path) -> tuple[Path, Path]:
    registry = tmp_path / "pairs.approved.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2.1,
                "pairs": [
                    {
                        "pair_key": "KXONE|0x1",
                        "kalshi_market_id": "KXONE",
                        "orientation": "same",
                        "status": "approved_for_paper",
                        "include_in_strategy_metrics": True,
                        "polymarket_identifiers": {
                            "condition_id": "0x1",
                            "yes_token_id": "yes-1",
                            "no_token_id": "no-1",
                        },
                        "taxonomy": {
                            "resolution_structure": "objective_single_event",
                            "grouping_alignment": "n/a",
                            "persistence_cause": "none",
                        },
                        "equivalence": {"status": "verified_equivalent"},
                        "decision_log": [
                            {
                                "at": "2026-07-05T00:00:00+00:00",
                                "decision": "approve",
                                "rationale": "fixture",
                                "auditor": "test",
                            }
                        ],
                        "display_name": "KXONE",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    write_registry_integrity(registry)
    config = tmp_path / "scanner.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "default_batch_size": 20,
                "default_tick_s": 1.0,
                "max_calls_per_s_per_venue": 20,
                "min_arb_edge": 0.0,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return registry, config


def test_scanner_start_refused_when_engaged(tmp_path, monkeypatch):
    monkeypatch.delenv(KILL_ENV_VAR, raising=False)
    sentinel = _sentinel(tmp_path)
    switch = KillSwitch(sentinel)
    asyncio.run(switch.engage("scanner refusal test"))

    def forbidden_popen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("scanner subprocess must not start while the kill switch is engaged")

    registry, config = _write_scanner_fixture(tmp_path)
    soaks_root = tmp_path / "data" / "soaks"
    controller = ScannerControllerImpl(
        repo_root=REPO_ROOT,
        pair_registry_path=registry,
        soak_store=SoakStoreImpl(soaks_root, []),
        config_path=config,
        popen_factory=forbidden_popen,  # type: ignore[arg-type]
        killswitch=switch,
    )

    result = controller.start_scanner(pair_keys=["KXONE|0x1"], batch_size=1, tick_s=1.0)

    assert isinstance(result, OpError)
    assert result.code == "killswitch_engaged"
    # Refusal happens before any side effect: no soak dir, no state change.
    assert not soaks_root.exists() or not any(soaks_root.iterdir())
    assert controller.get_scanner_status()["running"] is False

    # env-only engagement (no sentinel) must refuse too.
    sentinel.unlink()
    monkeypatch.setenv(KILL_ENV_VAR, "1")
    result = controller.start_scanner(pair_keys=["KXONE|0x1"], batch_size=1, tick_s=1.0)
    assert isinstance(result, OpError)
    assert result.code == "killswitch_engaged"


def test_engaged_state_in_app_status(tmp_path, monkeypatch):
    monkeypatch.delenv(KILL_ENV_VAR, raising=False)
    sentinel = _sentinel(tmp_path)
    switch = KillSwitch(sentinel)
    client = TestClient(create_app(ServiceRegistry(killswitch=switch)))

    before = client.get("/api/get_app_status").json()
    assert before["ok"] is True
    assert before["data"]["killswitch_engaged"] is False
    assert before["data"]["killswitch_reason"] is None

    asyncio.run(switch.engage("status surface test"))

    after = client.get("/api/get_app_status").json()
    assert after["ok"] is True
    assert after["data"]["killswitch_engaged"] is True
    assert after["data"]["killswitch_reason"] == "status surface test"
    assert after["data"]["mode"] == "paper"
