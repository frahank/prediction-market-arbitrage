#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
"""Verify that a built wheel contains the files required by the local UI."""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

REQUIRED_WHEEL_FILES = {
    "arbx/ui/templates/base.html",
    "arbx/ui/templates/paper.html",
    "arbx/ui/static/app.css",
    "arbx/ui/static/app.js",
}


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    missing = sorted(REQUIRED_WHEEL_FILES - names)
    if missing:
        raise RuntimeError(
            f"wheel {path.name} is missing required UI assets: {', '.join(missing)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args(argv)
    wheels = sorted(args.dist_dir.glob("arbx-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"expected exactly one arbx wheel in {args.dist_dir}, found {len(wheels)}"
        )
    verify_wheel(wheels[0])
    print(f"distribution check passed: {wheels[0].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
