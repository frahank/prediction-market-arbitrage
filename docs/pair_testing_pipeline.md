# How to test a pair for arb-ness

The standard workflow for deciding whether a Kalshi↔Polymarket pair is a real,
capturable arbitrage — or just basis, noise, or a mirage. Run this for **every
new pair** before it earns any trust. Everything here is paper/public-data only;
no order is ever placed.

## The one rule

**Equivalence → economics → execution, in that order.** Never skip ahead. An
edge on a pair that isn't truly equivalent is not profit — it's an unhedged bet
that can lose without limit. An edge that survives fees but not latency is not
capturable. Test in the order that can kill a pair cheapest, first.

## What makes a real arb (all four must hold)

1. **Equivalent** — Kalshi YES pays exactly when Polymarket YES pays. Same
   event, same resolution rules, same cutoff.
2. **Transient** — the edge *appears and disappears*. An edge that persists for
   hours is basis (the contracts differ) or dead displayed liquidity, not arb.
3. **Survives cost** — clears *real* per-venue fees, slippage, and carry.
4. **Survives latency** — still there when your order would land (measured by
   survival probe, not assumed).

## The pipeline

### Stage 0 — Equivalence gate (do this before spending soak time)
Answers: *is this pair even a hedge?* Cheapest kill. Pull both venues' rules,
prescreen the R1–R7 taxonomy, and get an AI + human rules-diff.

```
.venv/bin/python scripts/run_targeted_soak.py --pair <PAIR_KEY> --hours 0 \
    --data-dir data_probe_<pair>          # snapshot + audit pack, no long soak
```

Produces an evidence pack under `evidence/<pair>/<date>/`:
`rules_snapshot.json`, `prescreen.json`, `audit_prompt.txt`, `human_checklist.md`.
Feed `audit_prompt.txt` to an AI, save its answer as `ai_audit.md`.

- **Kill if:** rules differ, cutoffs differ, grouping is dirty (e.g. UEFA ≠
  geographic Europe), or resolution is subjective. → record `reject`, stop.
- Use the human review in
  [docs/pair_equivalence_checklist.md](pair_equivalence_checklist.md).

### Stage 1 — Low-skew capture (the ≥24h soak)
Answers: *does an edge ever appear, and how often?* **Capture must be
low-skew.** Both legs recorded seconds apart produce fake edges (the market
moved between captures). Use concurrent REST, not sequential.

```
.venv/bin/python scripts/run_targeted_soak.py --pair <PAIR_KEY> --hours 24 \
    --data-dir data_soak_<pair> --rest-interval-s 5
```

- Confirm `capture_summary.json` shows paired-skew **p95 ≤ 25ms**. If it is
  seconds, the Kalshi leg is REST-polling rather than streaming — the
  numbers are provisional; label them, don't trust them.
- Keep the machine awake for the soak.

### Stage 2 — Data-quality gate
Answers: *can I trust this soak?* Non-gating freshness is fine; recorder health
and `crossed_books` must pass.

```
.venv/bin/python scripts/data_quality.py --data-dir data_soak_<pair>
```

- **Kill if:** recorder health fails or `crossed_books` trips (bid>ask at rest =
  the legacy swap bug; re-run analysis with `--legacy-book-fix`).

### Stage 3 — Real-fee edge + episode analysis → the report
Answers: *is it transient or persistent, and does it clear real fees?* Candidate
counts collapse ~18× between assumed 1¢ and 2¢ fees, so **always use real
fees.** Pin `--poly-bps 1000` for resolved markets (their live `/fee-rate` 404s
to worst-case otherwise).

```
.venv/bin/python scripts/run_soak_analysis.py --data-dir data_soak_<pair> \
    --real-fees --poly-bps 1000
```

Reads the buckets: **`transient_candidate`** (good — appears/disappears),
**`basis_suspect`** (persistent = not arb, exclude), **`unusable`** (no fresh
edge). Legacy/pre-2026-07-03 dirs **must** add `--legacy-book-fix`.

- **Kill if:** the pair is `basis_suspect` or shows zero qualifying rows under
  real fees.

### Stage 4 — Survival probe
Answers: *does the edge survive order-routing latency?* The soak analysis probes
qualifying edges at rungs [25, 50, 100, 250, 500, 1000]ms and assigns a tier.

- **Kill if:** edges die before the measured end-to-end reaction time. The
  default model uses 250ms; measure your own host with `scripts/latency_study.py`.

### Stage 5 — Profitability model → verdict
Answers: *after fees, carry, fill probability, and failed-leg risk, does it make
money?* Start the local UI, select the completed soak on the Paper tab, and run
**Full Analysis**. The assumptions and viability thresholds are explicit in
`configs/modeling.yaml`. Treat the resulting **`viable | marginal | not_viable`**
classification as a model output, never realized profit.

### Stage 6 — Record the decision
Every pair ends with a logged decision (never a silent one):

```
.venv/bin/python scripts/pair_decide.py --pair <PAIR_KEY> \
    --decision approve|reject|needs_more_data|archive \
    --rationale "<one line>" --auditor <you|model-id>
```

Only `verified_equivalent`/`tail_divergence_documented` + a latest `approve`
flips `include_in_strategy_metrics` on — the YAML flag alone is never enough.

## Quick reference

| Stage | Question | Command | Kills the pair if |
|---|---|---|---|
| 0 Equivalence | Is it a hedge? | `run_targeted_soak --hours 0` | rules/cutoff/grouping differ |
| 1 Capture | Does an edge appear? | `run_targeted_soak --hours 24` | skew p95 > 25ms (untrusted) |
| 2 DQ gate | Trustworthy soak? | `data_quality` | recorder health / crossed_books fail |
| 3 Edge+fees | Transient & fee-positive? | `run_soak_analysis --real-fees` | basis_suspect / 0 rows |
| 4 Survival | Survives latency? | (in soak analysis probes) | dies < 250ms |
| 5 Profit | Makes money? | UI Full Analysis | not_viable |
| 6 Decide | — | `pair_decide` | logged reject |

## Red flags — what a fake edge looks like

- **Persistent** (in-edge in >25% of cycles) → basis, not arb.
- **Seconds of capture skew** → the "edge" is quote drift between the two grabs.
- **Positive only at flat 1¢ fees** → dies under real fees.
- **Huge displayed size** (>1000 contracts on a thin market) → not real depth.
- **Long-dated** → carry eats the edge even if everything else holds.

## Testing many pairs at once

Go wide before going deep: scan a rate-limited reviewed registry, use Stage 3
to identify transient candidates, then run the expensive survival and modeling
steps only on the few pairs that remain.

## Reminders

- Paper-only, public GETs. `configs/runtime.yaml` stays `mode: paper`,
  `real_orders: 0`.
- Any known pre-fix soak directory must
  pass `--legacy-book-fix` (see [docs/book_semantics_fix.md](book_semantics_fix.md)).
