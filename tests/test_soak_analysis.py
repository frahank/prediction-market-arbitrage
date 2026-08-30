from __future__ import annotations

import json
from pathlib import Path

from scripts.run_soak_analysis import main


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_run_soak_analysis_writes_dated_summary(tmp_path: Path):
    data_dir = tmp_path / "data_strategy_30_modeling_20260701"
    ts = "2026-07-01T00:00:00+00:00"
    rows = []
    # quiet cycles + a 3-snapshot transient dislocation
    rows += [{"pair_key": "KXWC|0x", "direction": "kalshi_no_poly_yes",
              "capture_ts_utc": f"2026-07-01T00:1{i}:00+00:00", "capture_skew_ms": 50.0,
              "books_fresh": True, "fee_adj_edge": 0.0, "depth_adj_edge": 0.0,
              "max_profitable_size": 0.0, "public_probe": False, "run_id": "r"} for i in range(9)]
    rows += [{"pair_key": "KXWC|0x", "direction": "kalshi_no_poly_yes",
              "capture_ts_utc": f"2026-07-01T00:20:{30*i:02d}+00:00", "capture_skew_ms": 50.0,
              "books_fresh": True, "fee_adj_edge": 0.05, "depth_adj_edge": 0.05,
              "max_profitable_size": 5000.0, "public_probe": False, "run_id": "r"} for i in range(3)]
    # a probe row on the transient candidate that survived to 1000ms
    rows.append({"pair_key": "KXWC|0x", "direction": "kalshi_no_poly_yes",
                 "capture_ts_utc": ts, "public_probe": True, "benchmark_ms": 1000,
                 "survived_through_ms": 1000, "survival_tier": "survived_1000ms"})
    _write_jsonl(data_dir / "raw" / "edge" / "2026-07-01.jsonl", rows)
    _write_jsonl(data_dir / "raw" / "latency" / "2026-07-01.jsonl",
                 [{"kind": "recorder_heartbeat", "capture_ts_utc": ts,
                   "run_id": "rec_20260701T053201Z_abc"}])

    rc = main(["--data-dir", str(data_dir), "--out-dir", str(tmp_path / "reports"),
               "--skip-heatmaps", "--no-compare"])
    assert rc == 0
    summary = tmp_path / "reports" / "soak_analysis_2026-07-01.md"
    assert summary.exists()
    text = summary.read_text(encoding="utf-8")
    assert "Soak analysis — 2026-07-01" in text
    assert "Go / no-go read" in text
    assert "KXWC" in text
    assert "Survival deep-dive" in text
    # evidence pack was exported by the battery
    assert (tmp_path / "reports" / "rec_20260701T053201Z_abc" / "profitability_summary.md").exists()
