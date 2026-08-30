# Scope: BOT_RUNTIME — Restart-safe fair rotation over scanner pair keys.
"""Deterministic rolling-cursor batching for the live scanner.

The scheduler is intentionally small and stateful: one call returns the next
contiguous batch, wrapping inside the batch when needed, then persists the
post-batch cursor. That pins the operator-facing cadence math independently of
the scanner's fetch/capture machinery.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RotationPlan:
    batch: tuple[str, ...]
    cursor: int


def effective_cadence_s(pair_count: int, batch_size: int, tick_s: float) -> float:
    """Return the real per-pair refresh interval for a rolling scan."""
    if pair_count <= 0:
        return 0.0
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if tick_s < 0:
        raise ValueError("tick_s must be >= 0")
    return ceil(pair_count / batch_size) * tick_s


class RotationScheduler:
    """Restart-safe round-robin batch scheduler.

    ``state_path`` points at the scanner's ``scan_state.json``. Only the cursor
    is authoritative; if the universe size changes, it is clamped modulo the
    current pair count so a stale state file cannot skip the run.
    """

    def __init__(
        self,
        pairs: list[str],
        *,
        batch_size: int,
        state_path: Path | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._pairs = tuple(pairs)
        self._batch_size = batch_size
        self._state_path = Path(state_path) if state_path is not None else None
        self._cursor = self._load_cursor()

    @property
    def cursor(self) -> int:
        return self._cursor

    def next_batch(self) -> RotationPlan:
        if not self._pairs:
            self._cursor = 0
            self._persist()
            return RotationPlan((), 0)

        n = len(self._pairs)
        start = self._cursor % n
        count = min(self._batch_size, n)
        batch = tuple(self._pairs[(start + i) % n] for i in range(count))
        self._cursor = (start + count) % n
        self._persist()
        return RotationPlan(batch, self._cursor)

    def _load_cursor(self) -> int:
        if not self._pairs or self._state_path is None or not self._state_path.exists():
            return 0
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            cursor = int(payload.get("cursor", 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return 0
        return cursor % len(self._pairs)

    def _persist(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
        payload = {
            "cursor": self._cursor,
            "pair_count": len(self._pairs),
            "batch_size": self._batch_size,
        }
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._state_path)
