"""Bounded live-public health check for the checked-in pair registry."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from arbx.capture.rest_concurrent import ConcurrentRestSource
from arbx.capture.types import PairedSnapshot
from arbx.pairs.registry import PairSpec, load_pairs

PairFetcher = Callable[[PairSpec], Awaitable[PairedSnapshot | None]]


async def evaluate_pairs(
    pairs: list[PairSpec],
    fetch_pair: PairFetcher,
    *,
    max_concurrency: int = 4,
    pair_timeout_s: float | None = None,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def evaluate(pair: PairSpec) -> dict[str, Any]:
        try:
            async with semaphore:
                if pair_timeout_s is None:
                    snapshot = await fetch_pair(pair)
                else:
                    snapshot = await asyncio.wait_for(
                        fetch_pair(pair), timeout=max(0.1, pair_timeout_s)
                    )
        except TimeoutError:
            snapshot = None
            status = "timeout"
        except Exception as exc:  # health reporting must isolate each pair
            snapshot = None
            status = f"error:{type(exc).__name__}"
        else:
            status = "healthy" if snapshot is not None else "unavailable"
        return {
            "pair_key": pair.pair_key,
            "kalshi_market_id": pair.kalshi_market_id,
            "display_name": pair.display_name,
            "healthy": snapshot is not None,
            "status": status,
            "skew_ms": None if snapshot is None else round(snapshot.skew_ms, 3),
        }

    return await asyncio.gather(*(evaluate(pair) for pair in pairs))


async def check_registry(
    pairs_path: Path,
    *,
    timeout_s: float = 10.0,
    max_concurrency: int = 4,
) -> list[dict[str, Any]]:
    pairs = load_pairs(pairs_path)
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        source = ConcurrentRestSource(pairs, client=client)

        async def fetch(pair: PairSpec) -> PairedSnapshot | None:
            return await source.fetch_pair(client, pair)

        return await evaluate_pairs(
            pairs,
            fetch,
            max_concurrency=max_concurrency,
            pair_timeout_s=timeout_s,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check every active pair against current public venue books"
    )
    parser.add_argument(
        "--pairs",
        type=Path,
        default=Path.cwd() / "configs" / "pairs.approved.yaml",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--json", type=Path, default=None, dest="json_path")
    parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="report unavailable pairs without returning a failing exit status",
    )
    args = parser.parse_args(argv)

    results = asyncio.run(
        check_registry(
            args.pairs,
            timeout_s=max(0.1, args.timeout),
            max_concurrency=max(1, args.max_concurrency),
        )
    )
    healthy = [row for row in results if row["healthy"]]
    unavailable = [row for row in results if not row["healthy"]]
    report = {
        "schema_version": 1,
        "pairs": len(results),
        "healthy": len(healthy),
        "unavailable": len(unavailable),
        "results": results,
    }
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"pair health: {len(healthy)}/{len(results)} healthy")
    for row in unavailable:
        print(
            f"unavailable ({row['status']}): "
            f"{row['kalshi_market_id']} — {row['display_name']}"
        )
    return 0 if not unavailable or args.allow_unavailable else 1


if __name__ == "__main__":
    raise SystemExit(main())
