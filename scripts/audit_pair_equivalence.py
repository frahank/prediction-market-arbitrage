#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# CLI: snapshot a pair's rules, run the deterministic prescreen, and emit the
# rules-diff audit prompt for a human or an LLM reviewer.
#
# This is the step that decides whether two markets actually settle on the same
# condition. It is the highest-consequence judgement in the project and the one
# no automation here is allowed to make on its own: the prescreen only ever
# flags, never approves, and the verdict this produces still has to be recorded
# by a person through scripts/pair_decide.py.
#
#   1. --emit-prompt   fetch both venues' current rules text, hash it, run the
#                      deterministic checks, and print the audit prompt.
#   2. (you review, or hand the prompt to a model)
#   3. --verdict-in    validate the response and print the pair_decide.py
#                      command that records it.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arbx.pairs.equivalence import (  # noqa: E402
    build_audit_prompt,
    parse_audit_verdict,
    prescreen,
)
from arbx.pairs.registry import PairSpec, load_pairs  # noqa: E402
from arbx.pairs.rules_snapshot import fetch_rules, save  # noqa: E402


def _find_pair(registry: Path, market: str) -> PairSpec | None:
    for pair in load_pairs(registry, verify_sha256=False):
        if market in (pair.kalshi_market_id, pair.pair_key):
            return pair
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot a pair's rules, prescreen it, and emit the audit prompt.",
        epilog=(
            "The prescreen never approves a pair. It surfaces deterministic "
            "risks so a human reads the right part of the rules text."
        ),
    )
    parser.add_argument("market", help="pair_key or kalshi_market_id")
    parser.add_argument(
        "--pairs",
        type=Path,
        default=ROOT / "configs" / "pairs.approved.yaml",
        help="registry to read the pair from (default: the approved registry)",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=ROOT / "evidence",
        help="where the rules snapshot is written for the audit trail",
    )
    parser.add_argument(
        "--prompt-out",
        type=Path,
        help="write the audit prompt here instead of stdout",
    )
    parser.add_argument(
        "--verdict-in",
        type=Path,
        help="validate a reviewer's response instead of emitting a prompt",
    )
    args = parser.parse_args(argv)

    pair = _find_pair(args.pairs, args.market)
    if pair is None:
        print(f"pair {args.market!r} not found in {args.pairs}", file=sys.stderr)
        return 1

    if args.verdict_in is not None:
        try:
            verdict = parse_audit_verdict(args.verdict_in.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # An unparseable audit must never be read as a pass.
            print(f"verdict rejected: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(verdict, indent=2, sort_keys=True))
        print("\nNothing has been recorded. To record this decision yourself:\n")
        print(
            f"  ./.venv/bin/python scripts/pair_decide.py --pair {pair.pair_key} \\\n"
            f"      --decision <approve|reject|needs_more_data|archive> \\\n"
            f'      --rationale "<why, in your own words>" --auditor "<your name>"'
        )
        return 0

    print(f"fetching current rules for {pair.pair_key} ...", file=sys.stderr)
    snapshot = fetch_rules(pair)
    evidence = save(snapshot, args.evidence_root / pair.kalshi_market_id)
    result = prescreen(pair, snapshot)

    print(f"rules snapshot: {evidence}", file=sys.stderr)
    print(f"  sha256:    {snapshot.sha256}", file=sys.stderr)
    print(f"  warnings:  {list(snapshot.warnings) or 'none'}", file=sys.stderr)
    print(f"  structure: {result.structure}", file=sys.stderr)
    print(f"  prescreen: {result.score}", file=sys.stderr)
    for flag in result.flags:
        print(f"    - {flag}", file=sys.stderr)
    if not result.flags:
        print(
            "    no deterministic risk fired - this is NOT an approval, "
            "it only means the cheap checks found nothing",
            file=sys.stderr,
        )

    prompt = build_audit_prompt(pair, snapshot)
    if args.prompt_out is not None:
        args.prompt_out.write_text(prompt, encoding="utf-8")
        print(f"audit prompt written: {args.prompt_out}", file=sys.stderr)
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
