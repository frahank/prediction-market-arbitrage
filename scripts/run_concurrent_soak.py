#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Operator harness: drive a long true-concurrent
# recorded soak through the tested ScannerController service, with graceful
# SIGINT/SIGTERM finalization.
#
# It starts
# an all-approved-pairs recorded scan (record on, edges-only off,
# confirm_survival_ms=200) via arbx.services.scanner.ScannerControllerImpl —
# the same service the Paper Dashboard uses — and on a stop signal calls
# stop_scanner() so the manifest's ended_at and scan_summary.json are written.
# Public GETs only, paper-only. Kill with `kill -INT <pid>` (or SIGTERM).
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import yaml  # noqa: E402

from arbx.pairs.registry import load_pairs  # noqa: E402
from arbx.services.datastore import SoakStoreImpl  # noqa: E402
from arbx.services.scanner import ScannerControllerImpl  # noqa: E402
from arbx.ui.envelope import OpError  # noqa: E402


def _legacy_roots() -> list[Path]:
    ui_yaml = ROOT / "configs" / "ui.yaml"
    try:
        cfg = yaml.safe_load(ui_yaml.read_text()) or {}
    except OSError:
        return []
    roots = []
    for item in cfg.get("legacy_soak_roots", []) or []:
        p = Path(item)
        roots.append(p if p.is_absolute() else ROOT / p)
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Concurrent soak driver")
    parser.add_argument("--confirm-survival-ms", type=int, default=200)
    parser.add_argument("--heartbeat-s", type=float, default=60.0)
    args = parser.parse_args(argv)

    approved = ROOT / "configs" / "pairs.approved.yaml"
    pair_keys = [spec.pair_key for spec in load_pairs(approved)]
    soak_store = SoakStoreImpl(ROOT / "data" / "soaks", _legacy_roots())
    controller = ScannerControllerImpl(
        repo_root=ROOT,
        pair_registry_path=approved,
        soak_store=soak_store,
        config_path=ROOT / "configs" / "scanner.yaml",
    )

    started = controller.start_scanner(
        pair_keys=pair_keys,
        record=True,
        edges_only=False,
        confirm_survival_ms=args.confirm_survival_ms,
    )
    if isinstance(started, OpError):
        print(f"[soak] start refused: {started.code}: {started.message}", flush=True)
        return 1

    print(
        f"[soak] STARTED soak_id={started['soak_id']} "
        f"pairs={started['pair_count']} cadence={started['effective_cadence_s']}s "
        f"path={started['soak_path']}",
        flush=True,
    )
    print(f"[soak] driver pid={os.getpid()} — kill with: kill -INT {os.getpid()}",
          flush=True)

    stop = {"flag": False}

    def _handle(signum, _frame):
        print(f"[soak] signal {signum} received — finalizing…", flush=True)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    last_beat = 0.0
    try:
        while not stop["flag"]:
            now = time.monotonic()
            if now - last_beat >= args.heartbeat_s:
                st = controller.get_scanner_status()
                print(
                    f"[soak] beat state={st['state']} ticks={st['ticks']} "
                    f"snapshots={st['snapshots']} arbs={st['arbs_detected']} "
                    f"qualifying={st['qualifying']} errors={st['fetch_errors']} "
                    f"edges_written={st['edges_written']}",
                    flush=True,
                )
                last_beat = now
                if not st["running"]:
                    print("[soak] scanner exited on its own — stopping driver", flush=True)
                    break
            time.sleep(0.5)
    finally:
        result = controller.stop_scanner()
        if isinstance(result, OpError):
            print(f"[soak] stop note: {result.code}: {result.message}", flush=True)
        else:
            summary = result.get("summary") or {}
            print(
                f"[soak] STOPPED run_id={result.get('run_id')} "
                f"ticks={summary.get('ticks')} snapshots={summary.get('snapshots')} "
                f"arbs={summary.get('arbs_detected')} qualifying={summary.get('qualifying')}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
