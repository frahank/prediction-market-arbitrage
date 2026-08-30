# Pair-equivalence human checklist

The final, signed layer of the equivalence workflow. A pair may only reach
`equivalence.status: verified_equivalent` (or `tail_divergence_documented`) in
`configs/pairs.approved.yaml` after a human completes every step below and the
signed copy is stored in the pair's evidence pack as `human_checklist.md`.
Machine prescreen (`arbx.pairs.equivalence.prescreen`) and the AI audit
(`ai_audit.md`) are inputs to this review, never substitutes for it.

> Why this exists: a pair that resolves differently on any realistic outcome is
> not a hedge — it silently loses the full notional at settlement. This is the
> one failure mode with unbounded downside in the whole strategy.

## Checklist

Work from the pinned `rules_snapshot.json` in the evidence pack (never from
memory or the market title), with both venue pages open for cross-checking.

1. **Read both rule texts verbatim.** Kalshi `rules_primary` + `rules_secondary`
   and the Polymarket description, top to bottom. Copy the operative resolution
   sentence of each into the sign-off block below.
2. **Confirm the resolution source.** Who decides, from what data, and when?
   (Kalshi: the rules text; Polymarket: `resolutionSource` or UMA.) Note any
   difference in arbiter or source data.
3. **Enumerate the winners.** List the realistic outcome set (actual countries /
   candidates / teams). For each, mark Kalshi YES/NO and Polymarket YES/NO from
   the rules. Any mismatch on a realistic outcome ⇒ `basis`, stop here.
4. **Check grouping labels (R3).** If either venue resolves by a grouping scheme
   (confederation vs geographic continent, party vs registration, etc.), name
   both schemes and verify they partition the realistic outcome set identically.
   List every member whose label differs — each is a tail risk or a basis.
5. **Compare cutoffs to the minute (R4).** Kalshi `close_time` vs Polymarket
   `endDate` (registry `polymarket_close_time` if the API omits it). Record the
   delta in hours and what real-world event inside the window would resolve the
   venues apart.
6. **Confirm YES-token orientation.** The Polymarket YES token
   (`polymarket_identifiers.yes_token_id`) must mean the same event-direction as
   Kalshi YES. Check the market question phrasing on both venue pages; record it
   in `orientation_confirmed.kalshi_yes_meaning`.
7. **Record tail risks.** Every divergence found in steps 3–6 that you judge
   acceptably improbable goes into `equivalence.tail_risks` verbatim — accepted
   tails are documented tails.
8. **Sign and date.** Fill the block below; save as `human_checklist.md` in
   `evidence/<kalshi_market_id>/<date>/`; then record the decision with
   `scripts/pair_decide.py`, which appends to the registry
   `decision_log` and re-hashes the sidecar.

## Sign-off block (copy into the evidence pack)

```
pair_key:
kalshi_market_id:
rules_snapshot_sha256:
operative_kalshi_criterion:
operative_polymarket_criterion:
resolution_sources_match: yes/no — notes:
realistic_outcomes_enumerated: (list with YES/NO per venue)
grouping_schemes: (or n/a)
cutoff_delta_hours:
yes_token_orientation_confirmed: yes/no
tail_risks_accepted: (list)
verdict: verified_equivalent | tail_divergence_documented | basis | needs_more_data
reviewer:
date:
```

## Standing rules

- The AI audit's JSON verdict must parse (`parse_audit_verdict`) and its status
  must not be *more permissive* than yours; if the model says `basis` and you
  say `verified_equivalent`, resolve the disagreement in writing before signing.
- A pair whose rules text changes after signing (sha256 mismatch vs
  `equivalence.rules_snapshot_sha256`) reverts to `unreviewed` and must be
  re-audited.
- Subjective resolution language ("definitively states", "announces",
  "normalizes") caps the verdict at `tail_divergence_documented` — never
  `verified_equivalent` — regardless of wording similarity (R2).
