# Scanner rotation scheduler contract.
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from arbx.scanner.rotation import RotationScheduler, effective_cadence_s


def _pairs(n: int) -> list[str]:
    return [f"p{i}" for i in range(1, n + 1)]


def test_rolling_cursor_matches_operator_example():
    scheduler = RotationScheduler(_pairs(21), batch_size=20)

    first = scheduler.next_batch()
    second = scheduler.next_batch()

    assert first.batch == tuple(_pairs(20))
    assert first.cursor == 20
    assert second.batch == tuple(["p21", *_pairs(19)])
    assert second.cursor == 19


def test_fairness_over_even_refresh_window():
    scheduler = RotationScheduler(_pairs(300), batch_size=20)
    seen: list[str] = []

    for _ in range(15):
        seen.extend(scheduler.next_batch().batch)

    assert Counter(seen) == {pair: 1 for pair in _pairs(300)}
    assert scheduler.cursor == 0


def test_deterministic_given_cursor(tmp_path: Path):
    state_path = tmp_path / "scan_state.json"
    state_path.write_text(json.dumps({"cursor": 3}), encoding="utf-8")

    scheduler = RotationScheduler(_pairs(10), batch_size=4, state_path=state_path)
    plan = scheduler.next_batch()

    assert plan.batch == ("p4", "p5", "p6", "p7")
    assert plan.cursor == 7


def test_restart_resumes_from_persisted_cursor(tmp_path: Path):
    state_path = tmp_path / "scan_state.json"
    first = RotationScheduler(_pairs(8), batch_size=3, state_path=state_path)

    assert first.next_batch().batch == ("p1", "p2", "p3")

    restarted = RotationScheduler(_pairs(8), batch_size=3, state_path=state_path)
    plan = restarted.next_batch()

    assert plan.batch == ("p4", "p5", "p6")
    assert json.loads(state_path.read_text(encoding="utf-8"))["cursor"] == 6


def test_cadence_math():
    assert effective_cadence_s(300, 20, 1.0) == 15.0
    assert effective_cadence_s(21, 20, 1.0) == 2.0
    assert effective_cadence_s(0, 20, 1.0) == 0.0
