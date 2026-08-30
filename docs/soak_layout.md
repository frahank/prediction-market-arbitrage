# Soak Layout Contract

`ScannerController` writes scan soaks in this shape, and
`arbx.services.datastore` reads them for the UI.

```text
data/soaks/scan_<YYYYMMDD-HHMMSS>[_EDGES]/
  manifest.json
  raw/book/venue=*/<date>.jsonl
  scan/opportunities/<date>.jsonl
  EDGES_<YYYYMMDD-HHMMSS>.jsonl
  scan_summary.json
```

`manifest.json` is required for new contract directories:

```json
{
  "soak_id": "scan_20260705-141530",
  "label": "operator label",
  "started_at": "2026-07-05T14:15:30+00:00",
  "ended_at": null,
  "pair_keys": ["PAIR_A", "PAIR_B"],
  "edges_only": false,
  "record_books": true,
  "schema_version": 1
}
```

Files:

- `raw/book/venue=*/<date>.jsonl` is present only when `record_books=true` and
  keeps the recorder book-row layout unchanged.
- `scan/opportunities/<date>.jsonl` is present whenever scanner recording is
  on and stores full edge rows plus scanner fields.
- `EDGES_<YYYYMMDD-HHMMSS>.jsonl` stores one `StandardizedEdgeRow.to_dict()`
  per line, written at capture time by `arbx.scanner.edges_writer.EdgesWriter`
  Edge-only runs persist every detected row; full-record runs also
  write this file but persist only rows passing the full `qualifies()` gate,
  so the UI edges view is uniform across run shapes.

  Pinned semantics (see the writer's module docstring):
  - `round_trip_latency_ms = max(leg fetch_elapsed_ms)` — the slower leg's
    wire time of the pair's two concurrent venue fetches. This is the number
    a real reaction pays (the ~110ms REST floor); the recv-to-recv capture
    skew (~5ms p50) is stamped separately in `capture_skew_ms` and is NOT the
    round trip. Missing leg timings fall back to `|capture_skew_ms|`.
  - `est_profit = depth_adj_edge × executable_size` with
    `executable_size = depth_haircut × max_profitable_size`
    (`configs/modeling.yaml`), never the visible size.
  - `est_fees` comes from the real FeeEngine stamp; flat-heuristic rows are
    labeled `fee_model_version: "flat_heuristic"`.
  - `contract_equivalent`/`include_in_strategy_metrics` flow from the pair
    registry; `simulation_scope` is `"public_displayed_books"`.
- `scan_summary.json` records scanner exit counters and is used for operator
  diagnostics, not as the canonical manifest for new directories.

Rows must be append-only JSONL. Readers use byte-offset cursors for row
streaming; no multi-day file may be loaded whole.

## Migration Note

Older pre-contract capture directories may not have `manifest.json`. `SoakStoreImpl`
surfaces them by synthesizing manifest fields in memory from `scan_summary.json`,
directory contents, and file mtimes. The synthesized `label` is the directory
name, and `schema_version` is `1` at the UI seam so old datasets can be listed
beside new soaks.

Legacy roots are never modified. Anything discovered through a configured
legacy root, and anything whose DQ report shows crossed single-venue books, is
stamped `legacy_book_fix_applied=true` in `SoakFileMeta`; row readers must
route book-derived values through `arbx.data.legacy`.
