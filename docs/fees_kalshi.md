# Kalshi fee schedule — research notes

Pinned config: `configs/fees_kalshi.yaml` (version 1, retrieved 2026-07-02).
Every number below is traceable to the sources listed at the end.

## The formula

Kalshi charges a trading fee per order, as a function of contract count and
price, only on the matched (executed) portion:

```
taker fee = ceil_to_cent(0.07  × C × P × (1 − P))
maker fee = ceil_to_cent(0.0175 × C × P × (1 − P))   # designated series only
```

where `C` is the number of contracts, `P` is the contract price in dollars
(50¢ = 0.5), and `ceil_to_cent` rounds **up** to the next whole cent per
order. Quoted from the official fee schedule PDF (effective Feb 5, 2026):

> fees = round up(0.07 x C x P x (1-P)) …
> P = the price of a contract in dollars (50 cents is 0.5),
> C = the number of contracts being traded,
> round up = rounds to the next cent

Taker fees apply to orders "immediately matched with orders sitting on the
orderbook". Resting (maker) orders pay nothing **except** on series
designated as having maker fees, where the 0.0175 formula applies when the
resting order eventually trades. Cancelling a resting order is free.

There is **no settlement fee** and **no membership fee** (PDF, "Settlement
Fees" / "Membership Fees" sections).

## Per-series multipliers (public API)

The public trading API exposes `fee_type` and `fee_multiplier` per series
(`GET /trade-api/v2/series/{series_ticker}`, no auth). `fee_multiplier`
scales the 0.07 base:

- `quadratic`, multiplier 1 → the general 0.07 formula (the default;
  verified on KXHIGHNY and all approved-pair series below).
- `quadratic`, multiplier 0.5 → 0.035 formula. Verified on KXINX and
  KXNASDAQ100 on 2026-07-02, matching the PDF's reduced index table
  (e.g. 100 @ $0.50 → 0.035 × 100 × 0.25 = $0.875 → **$0.88**, exactly the
  PDF table value).
- `quadratic_with_maker_fees` → maker orders also pay, per the 0.0175
  maker formula.
- Other observed types: `margin_market_maker_program_fees` (crypto
  perpetuals, multiplier 0) and a `flat` type in the API enum — neither
  applies to binary event contracts in our pair universe (see Unverified).

### Scheduled change effective 2026-07-03T17:00Z

The official fee-change feed
(`GET /trade-api/v2/series/fee_changes?show_historical=true` on
`external-api.kalshi.com`) publishes, effective 2026-07-03T17:00Z:

- KXINX, KXINXU, KXINXPOS, KXINXMAXY, KXINXMINY, KXNASDAQ100,
  KXNASDAQ100U → `quadratic`, multiplier **1** (the 0.5 index discount is
  removed).
- KXINXY, KXNASDAQ100Y → `quadratic_with_maker_fees`, multiplier **1**.

The pinned config uses the **post-change** state: it takes effect the day
after `retrieved_at`, and pinning the expiring discount would understate
fees. Hence `series_overrides` contains only the two maker-fee series; the
former 0.035 index discount is deliberately absent.

### Approved-pair series (verified 2026-07-02)

All Kalshi series in `configs/pairs.approved.yaml` were queried directly:
KXABRAHAMSA, KXALIENS, KXF1, KXPRESNOMD, KXWCCONTINENT, OAIAGI — all
`quadratic`, multiplier 1. The general formula applies to every approved
pair; no override needed.

## Rounding

The schedule PDF rounds each order's fee up to the next cent. The API docs
(`getting_started/fee_rounding`) detail the exchange mechanics: the trade
fee is rounded up to the nearest $0.0001, a per-order accumulator adds a
rounding fee / issues whole-cent rebates so that "total fees converge to
what a single equivalent fill would cost", and non-direct member balances
round to $0.01. Net effect for a non-direct member: **ceil to whole cent
per order**, which is what the config pins (`rounding:
ceil_cent_per_order`). This is exact for single-fill orders and a
worst-case (never-understating) bound across multi-fill orders. Maker-fee
rounding overpayments are reimbursed monthly only above $10 — ignore that
credit for modeling (conservative).

## Worked examples (arithmetic matches the config)

1. **100 contracts @ $0.50, taker, general series**
   `0.07 × 100 × 0.50 × 0.50 = $1.75` → ceil to cent → **$1.75**.
   Official PDF table row: 100 contracts @ $0.50 → $1.75. ✓
2. **100 contracts @ $0.05, taker, general series**
   `0.07 × 100 × 0.05 × 0.95 = $0.3325` → ceil to cent → **$0.34**.
   Official PDF table row: 100 contracts @ $0.05 → $0.34. ✓
3. **1 contract @ $0.50, taker, general series**
   `0.07 × 1 × 0.25 = $0.0175` → ceil to cent → **$0.02**.
   Official PDF table row: 1 contract @ $0.50 → $0.02. ✓
4. **100 contracts @ $0.50, maker, KXINXY (maker-fee series)**
   `0.0175 × 100 × 0.25 = $0.4375` → ceil to cent → **$0.44**.
   (Maker formula from the PDF; KXINXY designation from the fee-change
   feed, effective 2026-07-03T17:00Z.)

## Unverified / open uncertainties

- **The latest fee-schedule PDF revision could not be fetched.** The live
  `kalshi.com/docs/kalshi-fee-schedule.pdf` and `kalshi.com/fee-schedule`
  sit behind a Vercel bot checkpoint (HTTP 429); the Internet Archive's
  last successful capture is 2026-02-18 (the Feb 5, 2026 revision), and
  search results reference a newer "Fee Schedule for June 2026". Mitigation:
  the live public API (series objects + fee-change feed, queried
  2026-07-02) is used as the current source of truth for per-series fee
  state, and it is consistent with the Feb 5 PDF formulas. Re-fetch the PDF
  from a browser and re-verify before relying on it outside paper research.
- **Maker multiplier scaling.** The PDF gives one maker formula (0.0175);
  whether a series `fee_multiplier ≠ 1` also scales the maker formula is
  not explicitly documented. Both pinned maker-fee series have multiplier
  1, so the question is moot today; revisit if a discounted
  `quadratic_with_maker_fees` series ever appears.
- **`flat` fee type.** Present in the API enum; no formula published in the
  documents reviewed, and no series in our universe uses it. Per the
  worst-case invariant, the fee model must treat any unknown or
  unrecognized `fee_type` as the general 0.07 taker formula and flag it,
  never as zero.
- **Fee-change cadence.** Series fee state is mutable (the feed schedules
  changes days ahead). The pinned overrides reflect 2026-07-03T17:00Z.
  Any reuse for a series outside the reviewed list must
  re-query `fee_multiplier`/`fee_type` rather than trust this snapshot.

## Sources

- Official Kalshi fee schedule PDF, "Last updated and effective: Feb 5,
  2026" — `https://kalshi.com/docs/kalshi-fee-schedule.pdf`, retrieved via
  Internet Archive capture
  `http://web.archive.org/web/20260218003606/https://kalshi.com/docs/kalshi-fee-schedule.pdf`.
- Kalshi API docs, fee rounding —
  `https://docs.kalshi.com/getting_started/fee_rounding.md`.
- Kalshi API docs, series fee changes —
  `https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes.md`.
- Live public API, queried 2026-07-02:
  `https://external-api.kalshi.com/trade-api/v2/series/fee_changes?show_historical=true`
  and `https://api.elections.kalshi.com/trade-api/v2/series/{series_ticker}`
  for KXINX, INX, KXNASDAQ100, KXHIGHNY, and all six approved-pair series.
