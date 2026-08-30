# Polymarket fee schedule — research notes

Pinned config: `configs/fees_polymarket.yaml` (version 1, retrieved
2026-07-02). Every number below is traceable to the sources listed at the
end. Companion doc: `docs/fees_kalshi.md`.

## The headline: Polymarket fees are per-token live data, not a constant

The plan's expectation ("most CLOB markets 0 bps today") is **outdated**.
Polymarket rolled out real taker fees on major categories in 2026. The
official fee page (retrieved 2026-07-02) lists taker fee rates by category:

| Category | Taker rate |
| --- | --- |
| Crypto | 0.07 (700 bps) |
| Economics, Culture, Weather, Other | 0.05 (500 bps) |
| Finance, Politics, Mentions, Tech | 0.04 (400 bps) |
| Sports | 0.03 (300 bps) |
| Geopolitics / world events | 0 (fee-free) |

Category labels are not exposed per-token, and the live endpoint has been
observed to contradict this table (see Unverified), so the model **never**
uses the table — it resolves the rate per token from the public endpoint:

```
GET https://clob.polymarket.com/fee-rate?token_id={token_id}
→ {"base_fee": <int, basis points>}     # e.g. 0, 300, 1000
```

The official docs explicitly warn against hardcoding fee rates and say to
fetch them dynamically from this endpoint. `arbx.fees.polymarket` caches
the resolved `base_fee` per token for `cache_ttl_hours` (6h).

## The formula

Two formulas are published and they disagree:

- **Fee page (marketing docs):** `fee = C × feeRate × P × (1 − P)` where
  `C` = shares and `P` = price in dollars. "Fees peak at 50% probability."
- **On-chain CTFExchange contract** (quoted in py-clob-client issue #326):
  `fee = feeRateBps × min(P, 1 − P) × outcomeTokens / (P × BPS_DIVISOR)`
  when the taker receives outcome tokens (buys). In USD terms
  (tokens × P) both sides of a fill reduce to
  `fee_usd = (feeRateBps / 10_000) × min(P, 1 − P) × C`.

The model implements the **on-chain** formula:

```
taker_fee_usd = size × (base_fee_bps / 10_000) × min(P, 1 − P)
```

Rationale: it is what the settlement contract actually charges, and it is
the conservative choice — `min(P, 1−P) ≥ P×(1−P)` for all `P` in (0, 1)
(equality only in the limit), so it never understates the fee page's curve.
At `P = 0.5` it is exactly 2× the fee-page curve. Fees apply to the matched
fill, computed at match time by the protocol; orders carry no fee fields.

Maker fees: **"Makers are never charged fees."** (official fee page).
`maker_fee_usd = 0`.

Settlement: the fee page documents no fee on redemption/settlement for
winners; `settlement_fee_usd = 0`. (Kept as an explicit field so a future
change is a one-line update.)

Gas: the CLOB is "a hybrid-decentralized trading system — offchain order
matching with onchain settlement"; orders are signed EIP-712 messages and
the operator submits matched trades to Polygon. The trader pays no gas per
fill → `gas_usd = 0` for CLOB orders. (Deposits, withdrawals, and direct
EOA on-chain operations can incur gas; those are account operations, not
per-fill costs, and are out of scope for edge math.)

Rounding: fees are "rounded to 5 decimal places. The smallest fee charged
is 0.00001 USDC." Sub-cent rounding is negligible at our sizes and the
model deliberately does not simulate it.

## Fallback (worst-case, never zero)

On any provider failure — endpoint unreachable, non-2xx, malformed payload,
negative value — the model returns the configured `fallback_bps` with
`source="flat_fallback"`. The config pins `fallback_bps: 1000`:

- 1000 bps is the highest `base_fee` observed in the wild (NBA/MLB sports
  tokens per issue #326), above the highest documented category rate
  (700 bps crypto).
- **Deviation from the plan:** the plan's example said `fallback_bps: 200`,
  written when 0 bps was the norm. After the 2026 fee rollout, 200 bps
  would understate the known worst case 5×; the worst-case invariant wins.

Fallback results are not cached, so a recovering endpoint is re-queried on
the next call.

## Worked examples (arithmetic matches the model)

1. **100 shares @ $0.50, token with `base_fee = 300` (sports)**
   `100 × 0.03 × min(0.5, 0.5) = 100 × 0.03 × 0.5 = $1.50`.
   (Fee-page curve would give `100 × 0.03 × 0.25 = $0.75` — we charge the
   on-chain, larger number.)
2. **100 shares @ $0.05, token with `base_fee = 700` (crypto)**
   `100 × 0.07 × min(0.05, 0.95) = 100 × 0.07 × 0.05 = $0.35`.
3. **100 shares @ $0.50, token with `base_fee = 0` (geopolitics)**
   `$0.00` — fee-free markets stay fee-free; zero is legitimate here
   because it is *observed per-token data*, not an assumption.
4. **100 shares @ $0.50, endpoint down, fallback 1000 bps**
   `100 × 0.10 × 0.5 = $5.00`, `source="flat_fallback"`.

## Unverified / open uncertainties

- **Docs-vs-endpoint contradictions.** py-clob-client issue #326
  (repo archived read-only 2026-05-25, issue unresolved) reports the
  fee page saying Sports = 0.03 while `/fee-rate` returned 0 for NHL
  tokens and **1000 bps** for NBA/MLB tokens. Mitigation: the model always
  trusts the per-token endpoint (which is what the settlement contract
  enforces), never the category table.
  **Confirmed first-hand (2026-07-03):** our own approved-pair tokens
  returned `base_fee: 1000` live (e.g. KXALIENS-27's and
  KXWCCONTINENT-26-EUR's paired tokens — above every documented category
  rate) and `0` for the KXABRAHAMSA-27 (geopolitics) token. The endpoint,
  not the table, is the source of truth.
- **Formula discrepancy.** The fee page's `P×(1−P)` vs the contract's
  `min(P, 1−P)`. We implement the contract form (larger, and what is
  actually charged on-chain). If Polymarket reconciles the docs, revisit;
  before relying on it outside paper research, verify against the venue's
  current charged fee.
- **`fd`/`tbf` metadata fields.** The cross-check
  (`resolve_polymarket_fee_config`) reads
  `tbf`/`fd.r`/`fd.to` from `/clob-markets/{condition_id}`. These fields
  are not in the public REST reference; the cross-check fails closed if
  they disappear, and the fee model itself never depends on them.
- **Settlement fee.** Absence of evidence: the fee page simply does not
  mention any winner/settlement fee. No source affirmatively says "none".
  Watch for changes.

## Sources

- Official fee page (formula, category table, maker-free, rounding) —
  `https://docs.polymarket.com/trading/fees`, retrieved 2026-07-02.
- Official API reference, fee-rate endpoint (`base_fee`, basis points) —
  `https://docs.polymarket.com/api-reference/market-data/get-fee-rate`,
  retrieved 2026-07-02.
- CLOB architecture (hybrid, offchain EIP-712 orders, onchain settlement) —
  `https://docs.polymarket.com/developers/CLOB/introduction`, retrieved
  2026-07-02.
- Docs-vs-endpoint contradiction, on-chain formula quote, observed
  1000 bps — `https://github.com/Polymarket/py-clob-client/issues/326`,
  retrieved 2026-07-02.
