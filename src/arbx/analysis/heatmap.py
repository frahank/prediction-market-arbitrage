# Scope: BOT_RUNTIME — Heatmap builders over recorder-derived tables (Phase 5).
"""
Heatmap tooling for the recorder dataset.

Three views, each derivable from data the recorder already collects:

  - ``latency_heatmap``: median data-fetch latency by venue × hour (Tier-0, the
    public half of the executability question — needs no credentials).
  - ``edge_heatmap``: positive-edge rate by pair × hour from edge_observations.
  - ``survival_heatmap``: edge-survival rate by pair × probe-ladder bucket. This
    is the cross-venue latency heatmap from DATA_PLATFORM_PLAN.md §5; it renders
    only once ``benchmark_ms``/``survived`` are populated (by the opportunity
    runner's latency ladder or Phase 3 streaming), so it is empty on plain
    snapshot data and that is reported honestly rather than faked.

Pure stdlib: builds grids and renders to text or self-contained HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _hour_of(ts: Any) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).hour


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass
class Heatmap:
    title: str
    row_label: str
    col_label: str
    rows: list[str] = field(default_factory=list)
    cols: list[str] = field(default_factory=list)
    cells: dict[tuple[str, str], float] = field(default_factory=dict)
    value_fmt: str = "{:.0f}"
    empty: bool = True

    def render_text(self) -> str:
        if self.empty or not self.rows:
            return f"{self.title}\n  (no data)"
        lines = [self.title, f"  {self.row_label} \\ {self.col_label}"]
        header = "  " + " ".join(f"{c:>6s}" for c in self.cols)
        lines.append(header)
        for r in self.rows:
            cells = " ".join(
                (self.value_fmt.format(self.cells[(r, c)]) if (r, c) in self.cells else "·").rjust(6)
                for c in self.cols
            )
            lines.append(f"  {r[:24]:24s} {cells}")
        return "\n".join(lines)

    def render_html(self) -> str:
        if self.empty or not self.rows:
            return f"<section><h3>{self.title}</h3><p>(no data)</p></section>"
        vals = list(self.cells.values())
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
        span = (hi - lo) or 1.0

        def color(v: float) -> str:
            t = (v - lo) / span  # 0..1
            # blue (low) -> red (high)
            r, g, b = int(60 + 195 * t), int(80 * (1 - t)), int(200 * (1 - t))
            return f"rgb({r},{g},{b})"

        head = "".join(f"<th>{c}</th>" for c in self.cols)
        body = []
        for rkey in self.rows:
            tds = []
            for c in self.cols:
                if (rkey, c) in self.cells:
                    v = self.cells[(rkey, c)]
                    tds.append(f'<td style="background:{color(v)};color:#fff">{self.value_fmt.format(v)}</td>')
                else:
                    tds.append('<td style="background:#eee">·</td>')
            body.append(f"<tr><th>{rkey}</th>{''.join(tds)}</tr>")
        return (
            f"<section><h3>{self.title}</h3>"
            f'<table style="border-collapse:collapse;text-align:center;font:12px monospace">'
            f"<tr><th>{self.row_label}\\{self.col_label}</th>{head}</tr>"
            f"{''.join(body)}</table></section>"
        )


def latency_heatmap(book_rows: list[dict[str, Any]]) -> Heatmap:
    """Median fetch latency (ms) by venue × UTC hour."""
    buckets: dict[tuple[str, str], list[float]] = {}
    hours: set[int] = set()
    venues: set[str] = set()
    for row in book_rows:
        venue = row.get("venue")
        ms = row.get("fetch_elapsed_ms")
        hour = _hour_of(row.get("capture_ts_utc"))
        if venue is None or not isinstance(ms, (int, float)) or hour is None:
            continue
        venues.add(venue)
        hours.add(hour)
        buckets.setdefault((venue, f"{hour:02d}"), []).append(float(ms))
    hm = Heatmap(title="Data-fetch latency (median ms) by venue × hour (UTC)",
                 row_label="venue", col_label="hour", value_fmt="{:.0f}")
    hm.rows = sorted(venues)
    hm.cols = [f"{h:02d}" for h in sorted(hours)]
    for key, vals in buckets.items():
        m = _median(vals)
        if m is not None:
            hm.cells[key] = m
    hm.empty = not hm.cells
    return hm


def edge_heatmap(edge_rows: list[dict[str, Any]], *, strategy_only: bool = False) -> Heatmap:
    """Positive fee-adjusted-edge rate (%) by pair × UTC hour."""
    counts: dict[tuple[str, str], list[int]] = {}  # (pair,hour) -> [positive, total]
    pairs: set[str] = set()
    hours: set[int] = set()
    for row in edge_rows:
        if strategy_only and not row.get("include_in_strategy_metrics", False):
            continue
        pair = row.get("pair_key")
        edge = row.get("fee_adj_edge")
        hour = _hour_of(row.get("capture_ts_utc"))
        if pair is None or not isinstance(edge, (int, float)) or hour is None:
            continue
        pairs.add(pair)
        hours.add(hour)
        slot = counts.setdefault((pair, f"{hour:02d}"), [0, 0])
        slot[1] += 1
        if edge > 0:
            slot[0] += 1
    hm = Heatmap(title="Positive fee-adj-edge rate (%) by pair × hour (UTC)",
                 row_label="pair", col_label="hour", value_fmt="{:.0f}")
    hm.rows = sorted(pairs)
    hm.cols = [f"{h:02d}" for h in sorted(hours)]
    for key, (pos, total) in counts.items():
        if total:
            hm.cells[key] = 100.0 * pos / total
    hm.empty = not hm.cells
    return hm


def survival_heatmap(edge_rows: list[dict[str, Any]]) -> Heatmap:
    """Edge-survival rate (%) by pair × probe-ladder bucket (benchmark_ms)."""
    counts: dict[tuple[str, str], list[int]] = {}
    pairs: set[str] = set()
    buckets: set[int] = set()
    for row in edge_rows:
        bm = row.get("benchmark_ms")
        survived = row.get("survived")
        pair = row.get("pair_key")
        if bm is None or survived is None or pair is None:
            continue
        pairs.add(pair)
        buckets.add(int(bm))
        slot = counts.setdefault((pair, str(int(bm))), [0, 0])
        slot[1] += 1
        if survived:
            slot[0] += 1
    hm = Heatmap(title="Edge-survival rate (%) by pair × latency bucket (ms)",
                 row_label="pair", col_label="ms", value_fmt="{:.0f}")
    hm.rows = sorted(pairs)
    hm.cols = [str(b) for b in sorted(buckets)]
    for key, (surv, total) in counts.items():
        if total:
            hm.cells[key] = 100.0 * surv / total
    hm.empty = not hm.cells
    return hm


def render_html_page(heatmaps: list[Heatmap], *, title: str = "Recorder heatmaps") -> str:
    sections = "".join(h.render_html() for h in heatmaps)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>body{{font-family:system-ui;margin:2rem}}td,th{{padding:3px 6px;border:1px solid #ccc}}"
        f"section{{margin-bottom:2rem}}</style></head><body><h1>{title}</h1>{sections}</body></html>"
    )
