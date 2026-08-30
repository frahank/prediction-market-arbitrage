# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Module 4 soak metadata store and data service.
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arbx.data.legacy import unswap_legacy_book_row
from arbx.data.quality import DataQualityReport, analyze
from arbx.ui.envelope import SCHEMA_VERSION, OpError
from arbx.ui.schemas import SoakFileMeta, StandardizedDataRow, StandardizedEdgeRow


def _safe_read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _line_count(path: Path) -> int:
    count = 0
    try:
        with path.open("rb") as fh:
            for line in fh:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _utc_from_mtime(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def _bool_param(value: bool | str | None) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean parameter must be true or false")


def _int_param(value: int | str | None, default: int, *, min_value: int, max_value: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("integer parameter is invalid") from exc
    return max(min_value, min(parsed, max_value))


class _LineCountCache:
    def __init__(self, cache_dir: Path | None) -> None:
        self.cache_dir = cache_dir

    def count(self, path: Path) -> int:
        if self.cache_dir is None:
            return _line_count(path)
        try:
            stat = path.stat()
        except OSError:
            return 0
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
        cache_file = self.cache_dir / "line_counts" / f"{path.name}.{digest}.json"
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns:
                value = cached.get("lines")
                return value if isinstance(value, int) else 0
        except (OSError, json.JSONDecodeError):
            pass
        lines = _line_count(path)
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "lines": lines}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return lines


class SoakStoreImpl:
    """Enumerate contract and legacy soak directories as `SoakFileMeta`."""

    def __init__(self, soaks_root: Path, legacy_roots: list[Path], cache_dir: Path | None = None) -> None:
        self.soaks_root = soaks_root.resolve()
        self.legacy_roots = tuple(root.resolve() for root in legacy_roots)
        self.cache_dir = cache_dir if cache_dir is not None else self.soaks_root / ".cache"
        self._line_counts = _LineCountCache(self.cache_dir)

    def list_soaks(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        edges_only: bool | None = None,
    ) -> tuple[list[SoakFileMeta], str | None]:
        found = self._all_meta()
        if edges_only is not None:
            found = [meta for meta in found if meta.edges_only is edges_only]
        start = 0
        if cursor:
            ids = [meta.soak_id for meta in found]
            if cursor in ids:
                start = ids.index(cursor) + 1
        page = found[start:start + limit]
        next_cursor = page[-1].soak_id if start + limit < len(found) and page else None
        return page, next_cursor

    def get_soak(self, soak_id: str) -> dict[str, Any] | OpError:
        for meta in self._all_meta():
            if meta.soak_id == soak_id:
                path = self.resolve(soak_id)
                dq = self._dq_report(path)
                return {"meta": meta.to_dict(), "dq": dq.to_dict() if dq is not None else None}
        return OpError("not_found", "soak was not found")

    def resolve(self, soak_id: str) -> Path:
        for path, _legacy in self._candidate_dirs():
            if path.name == soak_id:
                return path
        raise KeyError(soak_id)

    def resolve_for_rows(self, soak_id: str) -> tuple[Path, bool]:
        """Soak path plus whether the legacy book-semantics corrector must run.

        The fix is required for anything under a configured legacy root and for
        any directory whose data-quality
        report shows crossed single-venue books — a venue's own book can never
        genuinely cross, so crossing identifies pre-fix labels.
        """
        for path, legacy in self._candidate_dirs():
            if path.name == soak_id:
                needs_fix = legacy or self._dq_crossed_books(self._dq_report(path))
                return path, needs_fix
        raise KeyError(soak_id)

    def _all_meta(self) -> list[SoakFileMeta]:
        metas = [self._meta_for(path, legacy_root=legacy) for path, legacy in self._candidate_dirs()]
        metas = [meta for meta in metas if meta is not None]
        return sorted(metas, key=lambda meta: (meta.started_at, meta.soak_id), reverse=True)

    def _candidate_dirs(self) -> list[tuple[Path, bool]]:
        dirs: dict[Path, bool] = {}
        if self.soaks_root.exists():
            for path in self.soaks_root.iterdir():
                if path.is_dir() and path.name != ".cache":
                    dirs[path.resolve()] = False
        for root in self.legacy_roots:
            if root.is_dir() and root.name.startswith("data_") and (root / "raw").exists():
                dirs[root] = True
            elif root.is_dir():
                for path in root.glob("data_*"):
                    if path.is_dir() and (path / "raw").exists():
                        dirs[path.resolve()] = True
        return sorted(dirs.items(), key=lambda item: item[0].name)

    def _meta_for(self, path: Path, *, legacy_root: bool) -> SoakFileMeta | None:
        manifest = _safe_read_json(path / "manifest.json")
        summary = _safe_read_json(path / "scan_summary.json")
        has_data = (path / "raw").exists() or (path / "scan").exists() or any(path.glob("EDGES_*.jsonl"))
        if not manifest and not has_data:
            return None

        row_counts = self._row_counts(path)
        dq = self._dq_report(path)
        crossed = self._dq_crossed_books(dq)
        pair_keys = self._pair_keys(manifest, summary, path)
        started_at = str(manifest.get("started_at") or summary.get("started_at") or self._first_data_mtime(path))
        record_books = bool(manifest.get("record_books", row_counts["book"] > 0))
        edges_only = bool(manifest.get("edges_only", bool(list(path.glob("EDGES_*.jsonl"))) and row_counts["book"] == 0))

        return SoakFileMeta(
            soak_id=str(manifest.get("soak_id") or path.name),
            label=str(manifest.get("label") or path.name),
            path=path.as_posix(),
            started_at=started_at,
            ended_at=manifest.get("ended_at"),
            pair_keys=tuple(pair_keys),
            pair_count=len(pair_keys),
            edges_only=edges_only,
            record_books=record_books,
            row_counts=row_counts,
            dq_status=self._dq_status(dq),
            legacy_book_fix_applied=legacy_root or crossed,
            size_bytes=self._size_bytes(path),
            schema_version=int(manifest.get("schema_version") or SCHEMA_VERSION),
        )

    def _row_counts(self, path: Path) -> dict[str, int]:
        book = sum(self._line_counts.count(src) for src in (path / "raw" / "book").rglob("*.jsonl")) if (path / "raw" / "book").exists() else 0
        opportunities = sum(self._line_counts.count(src) for src in (path / "scan" / "opportunities").rglob("*.jsonl")) if (path / "scan" / "opportunities").exists() else 0
        edges_files = list(path.glob("EDGES_*.jsonl"))
        raw_edge_dir = path / "raw" / "edge"
        edge_sources = edges_files + (list(raw_edge_dir.rglob("*.jsonl")) if raw_edge_dir.exists() else [])
        edges = sum(self._line_counts.count(src) for src in edge_sources)
        return {"book": book, "opportunities": opportunities, "edges": edges}

    def _dq_report(self, path: Path) -> DataQualityReport | None:
        if not (path / "raw" / "book").exists():
            return None
        return analyze(path, cache_dir=self.cache_dir / "dq")

    @staticmethod
    def _dq_status(dq: DataQualityReport | None) -> str:
        if dq is None:
            return "unknown"
        return "pass" if dq.passed() else "fail"

    @staticmethod
    def _dq_crossed_books(dq: DataQualityReport | None) -> bool:
        if dq is None:
            return False
        threshold = dq.threshold_results().get("crossed_books")
        return threshold is not None and not threshold[0]

    @staticmethod
    def _pair_keys(manifest: dict[str, Any], summary: dict[str, Any], path: Path) -> list[str]:
        manifest_pairs = manifest.get("pair_keys")
        if isinstance(manifest_pairs, list):
            return sorted(str(pair) for pair in manifest_pairs)
        by_pair = summary.get("opportunities_by_pair")
        if isinstance(by_pair, dict):
            return sorted(str(pair) for pair in by_pair)
        pairs: set[str] = set()
        for base in (path / "scan" / "opportunities", path / "raw" / "edge"):
            for src in sorted(base.rglob("*.jsonl")) if base.exists() else []:
                pairs.update(_sample_pair_keys(src, limit=200))
        return sorted(pairs)

    @staticmethod
    def _first_data_mtime(path: Path) -> str:
        files = [src for src in path.rglob("*") if src.is_file()]
        if not files:
            return _utc_from_mtime(path)
        return _utc_from_mtime(min(files, key=lambda src: src.stat().st_mtime))

    @staticmethod
    def _size_bytes(path: Path) -> int:
        total = 0
        for src in path.rglob("*"):
            if src.is_file():
                try:
                    total += src.stat().st_size
                except OSError:
                    pass
        return total


def _sample_pair_keys(path: Path, *, limit: int) -> set[str]:
    pairs: set[str] = set()
    try:
        with path.open(encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if idx >= limit:
                    break
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pair = row.get("pair_key")
                if pair:
                    pairs.add(str(pair))
    except OSError:
        return pairs
    return pairs


# ---------------------------------------------------------------------------
# M4-T2: bounded standardized row reads (edges + books)
# ---------------------------------------------------------------------------

# Mirror of configs/modeling.yaml executable.depth_haircut (the composition
# root passes the config value; this is the fallback when it is unreadable).
# Displayed top-5 depth is an upper bound, not a fill, so executable size is
# haircut before any profit estimate.
DEFAULT_DEPTH_HAIRCUT = 0.5

_FRESHNESS_WORST_FIRST = ("missing_book", "missing_venue_timestamp", "stale")


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _float_or(value: Any, default: float = 0.0) -> float:
    parsed = _float_or_none(value)
    return default if parsed is None else parsed


def _str_or_none(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _parse_row_cursor(cursor: str | None) -> tuple[str | None, int]:
    """Split an opaque row cursor into (file relpath, byte offset)."""
    if not cursor:
        return None, 0
    rel, sep, offset = cursor.rpartition(":")
    if not sep or not rel or not offset.isdigit():
        raise ValueError("cursor is invalid")
    return rel, int(offset)


def _iter_rows(base: Path, files: list[Path], cursor: str | None):
    """Yield ``(relpath, byte_offset, row)`` from JSONL files, incrementally.

    Files iterate in sorted-relpath order; the cursor names the file and byte
    offset of the next row to emit, so reads resume without loading a
    multi-day file whole. Blank and malformed lines are skipped (their bytes
    still advance the offset).
    """
    start_rel, start_offset = _parse_row_cursor(cursor)
    for file in sorted(files, key=lambda f: f.relative_to(base).as_posix()):
        rel = file.relative_to(base).as_posix()
        if start_rel is not None and rel < start_rel:
            continue
        offset = start_offset if rel == start_rel else 0
        try:
            with file.open("rb") as fh:
                if offset:
                    fh.seek(offset)
                pos = offset
                for line in fh:
                    start = pos
                    pos += len(line)
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        row = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield rel, start, row
        except OSError:
            continue


def _edge_source_files(path: Path) -> tuple[list[Path], str]:
    """Edge-row sources in contract priority: EDGES files, else scanner
    opportunities, else recorder-derived raw edge rows."""
    edges_files = sorted(path.glob("EDGES_*.jsonl"))
    if edges_files:
        return edges_files, "standardized"
    opportunities_dir = path / "scan" / "opportunities"
    if opportunities_dir.exists():
        files = sorted(opportunities_dir.rglob("*.jsonl"))
        if files:
            return files, "opportunity"
    raw_edge_dir = path / "raw" / "edge"
    if raw_edge_dir.exists():
        return sorted(raw_edge_dir.rglob("*.jsonl")), "raw_edge"
    return [], "none"


def _pair_freshness(row: dict[str, Any]) -> str:
    """Worst of the two venue freshness labels (see F-T2 mapping docstring)."""
    kalshi = row.get("kalshi_freshness_status")
    polymarket = row.get("polymarket_freshness_status")
    statuses = {kalshi, polymarket}
    for status in _FRESHNESS_WORST_FIRST:
        if status in statuses:
            return status
    if kalshi == "fresh" and polymarket == "fresh":
        return "fresh"
    return "unknown"


def _map_edge_row(
    row: dict[str, Any],
    *,
    edge_id: str,
    depth_haircut: float,
    legacy: bool,
) -> StandardizedEdgeRow:
    """Map a scanner-opportunity / raw-edge row into ``StandardizedEdgeRow``.

    Mapping follows the F-T2 docstring on the schema. Conservative defaults
    for missing fields:

    - ``arb_detected`` (pre-scanner rows) is *derived* as the permissive
      after-fee cross ``fee_adj_edge > 0`` — that is its definition.
    - ``qualifies`` missing → ``False``: an unevaluated row never counts as
      passing the full gate.
    - ``executable_size = max_profitable_size × depth_haircut``
      (configs/modeling.yaml): displayed depth is an upper bound, not a fill.
    - ``est_profit = depth_adj_edge × executable_size``; no depth walk → 0.0.
    - ``est_fees`` prefers the real-engine ``fee_usd_at_target / target_size``;
      rows without it fall back to the per-unit fee actually subtracted
      (``raw_edge − fee_adj_edge``) and are labeled ``fee_model_version:
      "flat_heuristic"`` so the flat path is never mistaken for real fees.
    - legacy dirs: edges derived from pre-fix swapped books are swap
      artifacts; ``include_in_strategy_metrics`` is forced ``False`` so they
      can never re-enter strategy metrics.
    """
    pair_key = str(row.get("pair_key") or "")
    raw_edge = _float_or(row.get("raw_edge"))
    fee_adj_edge = _float_or(row.get("fee_adj_edge"))
    depth_adj_edge = _float_or_none(row.get("depth_adj_edge"))
    executable_size = _float_or(row.get("max_profitable_size")) * depth_haircut

    fee_usd = _float_or_none(row.get("fee_usd_at_target"))
    target_size = _float_or_none(row.get("target_size"))
    if fee_usd is not None and target_size is not None and target_size > 0:
        est_fees = fee_usd / target_size
    else:
        est_fees = max(raw_edge - fee_adj_edge, 0.0)

    arb_detected = row.get("arb_detected")
    if not isinstance(arb_detected, bool):
        arb_detected = fee_adj_edge > 0
    capture_skew_ms = _float_or(row.get("capture_skew_ms"))

    return StandardizedEdgeRow(
        edge_id=edge_id,
        pair_key=pair_key,
        # Registry display names land in M3 (registry v2.1); until a row
        # carries one, the pair key is the honest display fallback.
        display_name=str(row.get("display_name") or pair_key),
        direction=str(row.get("direction") or ""),
        scanned_at=str(row.get("scanned_at") or row.get("capture_ts_utc") or ""),
        arb_detected=bool(arb_detected),
        qualifies=bool(row.get("qualifies", False)),
        round_trip_latency_ms=abs(capture_skew_ms),
        est_fees=est_fees,
        est_profit=(depth_adj_edge * executable_size) if depth_adj_edge is not None else 0.0,
        raw_edge=raw_edge,
        fee_adj_edge=fee_adj_edge,
        depth_adj_edge=_float_or(depth_adj_edge),
        visible_size=_float_or(row.get("depth_fillable_size")),
        executable_size=executable_size,
        vwap_kalshi=_float_or_none(row.get("kalshi_vwap")),
        vwap_polymarket=_float_or_none(row.get("polymarket_vwap")),
        slippage=_float_or_none(row.get("slippage")),
        capture_skew_ms=capture_skew_ms,
        freshness_status=_pair_freshness(row),
        survival_tier=_str_or_none(row.get("survival_tier")),
        fee_model_version=str(row.get("fee_model_version") or "flat_heuristic"),
        simulation_scope=str(row.get("simulation_scope") or "paper_scan"),
        contract_equivalent=str(row.get("contract_equivalent") or "unreviewed"),
        include_in_strategy_metrics=(
            bool(row.get("include_in_strategy_metrics", False)) and not legacy
        ),
    )


# Conservative defaults for EDGES_*.jsonl passthrough rows missing a field
# (the file already holds StandardizedEdgeRow.to_dict() lines per the soak
# layout contract; defaults only paper over partial rows, never recompute).
_STANDARDIZED_EDGE_DEFAULTS: dict[str, Any] = {
    "pair_key": "", "display_name": "", "direction": "", "scanned_at": "",
    "arb_detected": False, "qualifies": False, "round_trip_latency_ms": 0.0,
    "est_fees": 0.0, "est_profit": 0.0, "raw_edge": 0.0, "fee_adj_edge": 0.0,
    "depth_adj_edge": 0.0, "visible_size": 0.0, "executable_size": 0.0,
    "vwap_kalshi": None, "vwap_polymarket": None, "slippage": None,
    "capture_skew_ms": 0.0, "freshness_status": "unknown", "survival_tier": None,
    "fee_model_version": "unknown", "simulation_scope": "paper_scan",
    "contract_equivalent": "unreviewed", "include_in_strategy_metrics": False,
}


def _edge_row_from_standardized(row: dict[str, Any], *, edge_id: str) -> StandardizedEdgeRow:
    kwargs = {
        name: row.get(name, default)
        for name, default in _STANDARDIZED_EDGE_DEFAULTS.items()
    }
    kwargs["edge_id"] = str(row.get("edge_id") or edge_id)
    if not kwargs["display_name"]:
        kwargs["display_name"] = str(kwargs["pair_key"])
    return StandardizedEdgeRow(**kwargs)


def _map_book_row(row: dict[str, Any], *, legacy_fix: bool) -> StandardizedDataRow:
    """Map a recorder §2.1 book row into ``StandardizedDataRow``.

    When ``legacy_fix`` is set, EVERY row routes through the book-semantics
    corrector before mapping (idempotent — already-correct rows pass through
    unchanged). Skipping it on legacy data silently inflates every edge by
    both venues' spreads.

    Conservative defaults: a book row carries no fee or edge estimate, so
    ``est_fees``/``est_profit`` stay ``None`` (never synthesized here), and a
    bare book row is never a strategy input (``include_in_strategy_metrics``
    ``False``, deny-by-default). Book rows are per venue-market, not per
    pair; ``pair_key`` falls back to ``venue:market_id`` — the pair join is
    Module 2's job.
    """
    if legacy_fix:
        row = unswap_legacy_book_row(row)

    freshness = str(row.get("freshness_status") or "unknown")
    dq_flags: list[str] = []
    best_bid = _float_or_none(row.get("best_bid"))
    best_ask = _float_or_none(row.get("best_ask"))
    if best_bid is not None and best_ask is not None and best_bid > best_ask:
        dq_flags.append("crossed_book")
    if freshness != "fresh":
        dq_flags.append(freshness)
    if row.get("legacy_book_fix"):
        dq_flags.append("legacy_book_fix")

    market_key = f"{row.get('venue') or 'unknown'}:{row.get('market_id') or 'unknown'}"
    return StandardizedDataRow(
        pair_key=str(row.get("pair_key") or market_key),
        display_name=str(row.get("display_name") or market_key),
        captured_at=str(row.get("capture_ts_utc") or ""),
        round_trip_duration_ms=_float_or(row.get("fetch_elapsed_ms")),
        est_fees=None,
        est_profit=None,
        freshness_status=freshness,
        staleness_seconds=_float_or_none(row.get("staleness_seconds")),
        dq_flags=tuple(dq_flags),
        simulation_scope=str(row.get("simulation_scope") or "public_observation"),
        include_in_strategy_metrics=False,
    )


class DataServiceImpl:
    """UI-facing Module 4 data service."""

    def __init__(self, store: SoakStoreImpl, *, depth_haircut: float | None = None) -> None:
        self.store = store
        self.depth_haircut = (
            DEFAULT_DEPTH_HAIRCUT if depth_haircut is None else float(depth_haircut)
        )

    def list_soaks(
        self,
        cursor: str | None = None,
        limit: int = 50,
        edges_only: bool | str | None = None,
    ) -> dict[str, Any]:
        metas, next_cursor = self.store.list_soaks(
            cursor=cursor,
            limit=_int_param(limit, 50, min_value=1, max_value=200),
            edges_only=_bool_param(edges_only),
        )
        return {"items": [meta.to_dict() for meta in metas], "next_cursor": next_cursor}

    def get_soak(self, soak_id: str) -> dict[str, Any] | OpError:
        return self.store.get_soak(soak_id)

    def list_soak_rows(
        self,
        soak_id: str,
        kind: str = "edges",
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any] | OpError:
        """Bounded standardized row reads: ``kind="edges"`` | ``kind="books"``.

        Byte-offset cursors; limit clamps to [1, 500]; legacy dirs route every
        book-derived value through the corrector before mapping.
        """
        bounded = _int_param(limit, 100, min_value=1, max_value=500)
        try:
            path, legacy_fix = self.store.resolve_for_rows(soak_id)
        except KeyError:
            return OpError("not_found", "soak was not found")

        if kind == "edges":
            files, source = _edge_source_files(path)

            def map_row(rel: str, offset: int, row: dict[str, Any]) -> dict[str, Any]:
                edge_id = f"{soak_id}:{rel}:{offset}"
                if source == "standardized":
                    return _edge_row_from_standardized(row, edge_id=edge_id).to_dict()
                return _map_edge_row(
                    row,
                    edge_id=edge_id,
                    depth_haircut=self.depth_haircut,
                    legacy=legacy_fix,
                ).to_dict()
        elif kind == "books":
            book_dir = path / "raw" / "book"
            files = list(book_dir.rglob("*.jsonl")) if book_dir.exists() else []

            def map_row(rel: str, offset: int, row: dict[str, Any]) -> dict[str, Any]:
                return _map_book_row(row, legacy_fix=legacy_fix).to_dict()
        else:
            return OpError("invalid_request", 'kind must be "edges" or "books"')

        items: list[dict[str, Any]] = []
        next_cursor: str | None = None
        for rel, offset, row in _iter_rows(path, files, cursor):
            if len(items) >= bounded:
                next_cursor = f"{rel}:{offset}"
                break
            items.append(map_row(rel, offset, row))
        return {"items": items, "next_cursor": next_cursor, "kind": kind}
