# AnalysisSummary v1 — formulas and caveats

`arbx.services.analysis.AnalysisServiceImpl` runs the ported analysis battery
over selected soaks and returns the standardized `AnalysisSummary`
(`arbx.ui.schemas`). Everything below is **model-based** (`basis: "model_v1"`)
until the replay track lands; the UI must caption `profit_score` as a
candidate score, never realized profit.

## Pipeline

`dq → edges → episodes → survival → fees → ev → summary`, one background job
at a time (a second `run_full_analysis` returns `conflict`). Progress is
persisted to `reports/analysis_jobs/<job_id>.json` after every stage, so
`get_analysis_status` survives a service crash (crash-resumable read side).

Legacy soaks (anything the SoakStore flags for the book-semantics corrector)
never contribute their stored edge rows — those are pre-fix swap artifacts.
Edges are re-derived from book rows routed through
`arbx.data.legacy.unswap_legacy_book_row` (flat-fee derivation; caveat
attached). A legacy soak without book rows contributes nothing and the caveat
says so. All computation uses **strategy rows only**
(`include_in_strategy_metrics: true`).

## Pinned v1 formulas

| Field | Definition |
|---|---|
| `profit_score` | Sum of `episodes.rank_opportunities` scores over transient, non-basis episodes across the selected soaks. A research/candidate score. |
| `min_latency_needed_ms` | p50 of `survived_through_ms` across qualifying rows (`episodes.qualifies`). `None` when no qualifying row carries survival data. |
| `chance_of_profit` | `P(survival ≥ assumed_reaction_latency_ms) × qualifying_rate`. `qualifying_rate` = qualifying rows / baseline (non-probe) rows. `P(survival)` is measured from probed qualifying rows when any exist; otherwise the `configs/modeling.yaml` `fill_probability.tiers` placeholder for the smallest tier ≥ the assumed latency (caveat attached). `assumed_reaction_latency_ms` lives in `configs/modeling.yaml` `analysis:`. |
| `chance_of_loss` | `1 − chance_of_profit × (1 − leg_failure_prob)` — the not-profit mass plus the leg-failure slice of would-be profits (`failed_leg.leg_failure_prob`). Conservative by construction. |
| `would_have_made_money_live` | Model EV (`arbx.modeling.ev.pair_ev` + `ViabilityThresholds`) per pair and direction under the `clean_concurrency` scenario: **viable** if any pair is viable; **marginal** if at least two are marginal; **not_viable** otherwise; **insufficient_data** with zero qualifying episodes. This is a model classification, not realized profit. `basis: "model_v1"`. |
| `fee_sensitivity` | Candidate-row counts at 1¢ / 2¢ (flat-fee shift via `episodes.fee_sensitivity`) and at the stored real-fee edge (`"real"` = qualifying rows as recorded). |
| `dq` | `quality.analyze` per soak: `passed` = recorder health across all soaks with books; freshness reported separately; per-soak detail string. |
| `sample` | `{snapshots, qualifying_rows, soak_hours}` — the n behind the chance figures; honesty requires it visible. |
| `graph` | `{"kind": "edge_timeline_v1", "payload": {"series": {pair_key: [[ts, fee_adj_edge, survival_tier], ...]}}}`, ≤500 points per pair. Produced by `build_graph_payload` — swapping the visualization is that ONE function. Operator-decided default (2026-07-05). |

## Caveats (always attached)

- Sampling resolution is cycle-grained — one snapshot per rotation cycle;
  never sub-cycle survival.
- Public REST refetch floor (~110ms) bounds what survival can observe.
- Legacy-correction note whenever a legacy soak contributed rows.
- Placeholder-probability note whenever `P(survival)` came from the modeling
  placeholder tiers instead of probes.

## Upgrade path to replay-grounded verdicts

If execution-grounded replay or a ledger is added later, `chance_of_profit`/
`chance_of_loss` and the verdict switch from model formulas to replayed
fills over recorded books; `basis` becomes `"replay"` and the placeholder
caveats disappear. The seam is already shaped for it: only the `_analyze`
internals change, `AnalysisSummary` does not.
