# Continuous paper scanner (`arbx.scanner`)

A continuous, round-robin **detection + capture** engine. It cycles the whole
pair universe in fixed-size batches, fires both venues concurrently for each
pair, prices the cross-edge with the real fee engine, and records every
qualifying (and near-miss) opportunity while it goes.

Public GETs only, paper-only, no order-mutation code — same safety envelope as
the recorder and the capture source (`docs/SAFETY.md`).

## The cadence

The operator picks two knobs:

- `batch_size` — pairs scanned per tick (default **20**).
- `tick_s` — seconds between ticks (default **1.0**).

Each tick the scanner takes the next `batch_size` pairs off a rolling cursor,
fetches them, then advances the cursor. If the cursor reaches the end inside a
batch, it wraps immediately and fills the rest of the batch from the front. A
**refresh window** — enough ticks for every pair to appear at least once —
therefore takes:

```
refresh_window ≈ ceil(len(universe) / batch_size) × tick_s
```

So 300 pairs at 20/tick, 1 tick/s → **15 ticks ≈ 15 s per refresh window**,
exactly the target. With 21 pairs at 20/tick, tick 1 scans pairs 1-20 and tick
2 scans pair 21 plus pairs 1-19, matching the operator rotation example. Change
either knob and the refresh interval scales predictably; the scanner logs the
implied cadence at startup.

Bounded concurrency is the point: a batch of 20 pairs fires **≤ 40 in-flight
GETs** (two venues each), which sits inside the default `httpx` connection pool
(100) and keeps per-request latency flat — unlike bursting all 300 pairs at
once. Per-pair fetches stay concurrent and RTT-compensated, so the **skew at the
scan instant remains single-digit-ms** and the edge you detect is a real
simultaneous cross, not a cross-time artifact.

## What it detects

For each pair it derives both directions' edge rows via
`arbx.analysis.edges.edge_rows_for_capture` with the real `FeeEngine`, then
tags each row at two levels:

- **`arb_detected`** — `fee_adj_edge > min_arb_edge` (default `> 0`): a positive
  after-real-fee edge on the displayed book. The cheap, permissive signal.
- **`qualifies`** — the full `arbx.analysis.episodes.qualifies` gate: fresh both
  books, `|skew| < 250 ms`, fee- **and** depth-adjusted edge ≥ 1¢, and ≥ 1
  contract fillable at the target size. The real opportunity bar.

Both flags are stamped on every logged row, so near-misses are captured too —
you see the distribution, not just the winners.

> **Displayed-book estimate, never a fill or profit claim.** An "opportunity"
> here is a cross on public order books after modeled fees. Whether it is
> *equivalent* (a true hedge vs. basis) is a separate registry question
> (`arbx.pairs.equivalence`); whether it would *fill* is P6 (replay) / P12
> (tiny live). Scanning an un-audited pair can surface a basis cross that is not
> arbitrage — prescreen the universe first.

## What it captures

- **Book rows** (default on): written through `ObservationSink` into the exact
  recorder layout `data_<run>/raw/book/venue=*/<date>.jsonl`, so the existing DQ
  report, edge derivation, compaction, and profitability tooling read scanner
  output with zero changes. `--no-record-books` skips this and logs only
  opportunities (tiny footprint) when you just want the detection stream.
- **Opportunity log**: every `arb_detected`/`qualifies` row →
  `data_<run>/scan/opportunities/<date>.jsonl`, each record being the full edge
  row plus `scanned_at`, `tick_index`, `arb_detected`, `qualifies`, and the two
  per-leg `*_fetch_elapsed_ms` wire times.
- **Standardized EDGES file** (always on): `EDGES_<ts>.jsonl` at the
  data-dir root, one `StandardizedEdgeRow` per line via
  `arbx.scanner.edges_writer.EdgesWriter`. `--edges-only` persists every
  detected row (and implies `--no-record-books`); normal runs persist
  qualifying rows only. Pinned semantics (latency = slower leg's wire time,
  executable-not-visible `est_profit`) live in the writer docstring and
  `docs/soak_layout.md`. The UI service (`ScannerControllerImpl`) passes
  `--edges-only` for edges-only runs.
- **`scan_summary.json`**: ticks, pairs scanned, snapshots, fetch errors/skips,
  arbs detected, qualifying count, per-pair opportunity counts, and skew
  percentiles.
- **`scan_state.json`**: the post-batch rotation cursor. New contract-style
  scanner runs persist it beside the scan output so a restarted scan resumes
  the next batch instead of starting at pair 1 again.

Data volume is governed by book recording. At ~2.5 KB/row and 2 rows per pair
per scan, 300 pairs on a 15 s refresh window write ~2.9 MB/min ≈ **4.1 GB/day**;
the opportunity log alone is negligible.

## Optional: single-rung survival confirmation (`--confirm-survival-ms`)

When set, every *detected* opportunity triggers **one** delayed refetch of that
same pair; the scanner recomputes the same-direction edge and labels whether it
survived. This is a single-rung version of the active survival probe
(`arbx.analysis.survival`) — cheap, because only pairs that crossed are
probed, and all of a tick's probes share one sleep and fire concurrently.

Each opportunity row gains:

- `survived_probe` — bool: the edge (same direction) still crosses after fees
  (uses the probe's `_survival_edge`: depth-adjusted if liquidity-complete,
  else fee-adjusted, `> 0`).
- `survived_probe_edge` — the delayed edge value.
- `survived_probe_qualifies` — whether the delayed row still passes the full gate.
- `survived_probe_delay_ms` — the configured sleep.
- `survived_probe_elapsed_ms` — the **true** measured recv-to-recv interval.

**Read the elapsed, not the delay.** `--confirm-survival-ms 200` adds a 200 ms
sleep, but the refetch itself costs one round-trip (~300–400 ms from this host,
Polymarket-bound), so the real survival interval is *sleep + refetch RTT*,
typically ~500–580 ms — captured in `survived_probe_elapsed_ms`. The refetch
RTT is a hard floor: a single public refetch **cannot** measure survival
tighter than one round-trip. To confirm survival below the RTT floor you need
streaming push from Kalshi WebSocket or a lower-latency probe, not this method. What
this *does* give you, cheaply and honestly, is a real "did the edge persist
across a few hundred ms, or was it a one-scan blip" signal — enough to separate
sticky crosses from noise.

```bash
python scripts/run_scanner.py --data-dir data_scan --duration 900 \
    --poly-bps 1000 --confirm-survival-ms 200
```

## Honest limitation: sampling resolution ≠ survival

Each pair is sampled **at least once per refresh window** (~15 s). So the scanner answers
*"is there an arb right now, and how big?"* — it does **not** measure whether an
edge survives 100 ms. Survival at sub-window timescales still needs streaming
push from Kalshi WebSocket or the targeted active probe
(`run_public_edge_survival_probe`), which is why the design is a funnel:

1. **Scanner (wide):** detect *which* pairs cross after real fees, and how
   often. Coarse survival (refresh-window-grained) only.
2. **Probe (narrow):** promote the handful that recur to dense streaming /
   active survival probing for the sub-110 ms truth.

The scanner replaces the broad passive soak as the discovery front-end; it is
not a substitute for the narrow survival prober.

## Usage

```bash
# 15-minute scan of the approved registry, 20 pairs/tick, 1 tick/s, real fees
python scripts/run_scanner.py \
    --pairs configs/pairs.approved.yaml \
    --data-dir data_scan_$(date +%Y%m%d) \
    --duration 900

# Wide detection-only stream (no book recording), Polymarket fee pinned
python scripts/run_scanner.py --pairs configs/pairs.candidates.yaml \
    --data-dir data_scan_wide --duration 3600 \
    --batch-size 20 --tick-s 1.0 --poly-bps 1000 --no-record-books
```

CLI flags: `--pairs`, `--data-dir` (required), `--duration` (s),
`--batch-size` (20), `--tick-s` (1.0), `--min-arb-edge` (0.0), `--target-size`
(1.0), `--poly-bps` (pin the Polymarket rate for resolved/offline markets),
`--no-record-books`.

## Analyzing the output

Because book rows land in the recorder layout, the whole battery works
unchanged:

```bash
python scripts/data_quality.py --data-dir data_scan_YYYYMMDD          # DQ gate
python scripts/run_soak_analysis.py --data-dir data_scan_YYYYMMDD --real-fees
```

The opportunity log is self-describing JSONL — one row per detected cross —
suitable for a direct `jq`/pandas pass to rank pairs by hit frequency and edge.

## Testing

`tests/test_live_scanner.py` drives the scanner with an injected paired-fetch
callable (no network): it verifies the rolling cursor covers the universe
inside each refresh window and wraps deterministically, that a positive-cross
snapshot is logged as an opportunity while a negative one is not, that book
recording toggles correctly, and that a per-pair fetch error skips the pair
without killing the tick. The scanner takes its fetch function, sinks, sleeper,
and clock as constructor arguments precisely so it runs deterministically
offline.
