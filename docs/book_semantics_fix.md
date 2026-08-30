# Book bid/ask semantics fix (2026-07-03)

## What was wrong

Both ported venue normalizers put **ask-side ladders into `yes_levels`**, the
slot the recorder's row mapping reads as the **bid** ladder. Every
`book_observations` row ever recorded — in the source research repo and in
this repo before this fix — had `best_bid`/`best_ask` (and the
`bid_px_*`/`ask_px_*` ladders, sizes included) swapped:

- `arbx/venues/kalshi_public.py` (`_book_from_orderbook_fp`): fed
  `1 − no_dollars` (the executable YES **ask** ladder) into `yes_levels` and
  `1 − yes_dollars` into `no_levels`. The recorder then complemented the NO
  side again, so `best_ask` came out as the YES **bid**.
- `arbx/venues/polymarket_public.py` (`_to_fixture_shape`): fed the payload's
  `asks` into `yes_levels` and complemented `bids` into `no_levels` — same
  double-inversion.

## How it was proven (2026-07-02, live)

For `KXALIENS-27` and its paired Polymarket token, at the same moment:

| Source of truth | Quote | Our normalized row |
|---|---|---|
| Kalshi `GET /markets/KXALIENS-27`: `yes_bid_dollars=0.0800`, `yes_ask_dollars=0.0810` | 0.080 / 0.081 | `best_bid=0.081`, `best_ask=0.080` — **swapped** |
| Polymarket `/book`: best bid 0.06, best ask 0.07; `/midpoint` = 0.065 | 0.06 / 0.07 | `best_bid=0.07`, `best_ask=0.06` — **swapped** |

Systemic scan: **100% of sampled rows** in `data_strategy_30` and
`data_strategy_30_modeling_*` (20,000 rows per venue) have
`best_bid > best_ask`. A single venue's book can never be genuinely crossed —
its own matching engine would trade through it — so this is a labeling
inversion, not market data.

## Consequences for prior analysis

`arbx/analysis/edges.py` implements the documented convention
(`best_ask` = cost to buy YES). On swapped rows, each computed direction's
`raw_edge` equals the **negative of the other direction's true edge** — i.e.
`raw_edge(kalshi_yes_poly_no)` as computed = `poly_conv_ask − kalshi_conv_bid`,
which is positive whenever there is **no** arb the other way. Computed
"edges" were therefore systematically inflated by both venues' spreads, and:

- the multi-hour "persistent positive edges" observed in the 2026-06-30
  modeling run are largely explained by this inversion (not only by
  contract-basis mismatch);
- any edge or profitability report produced from the mislabeled rows without
  correction is invalid and must be regenerated.

## The fix (this repo)

1. `kalshi_public._book_from_orderbook_fp`: `yes_levels`/`no_levels` now
   carry each side's **bid ladder, best-first** (`bid_levels_from_raw`),
   which is what `recorder.book_to_observation` assumes
   (`best_bid = yes_levels[0].price`, `best_ask = 1 − no_levels[0].price`).
2. `kalshi_public._levels_from_kalshi` (cents path): bids sorted best-first
   (the live API returns ascending).
3. `polymarket_public._to_fixture_shape`: payload `bids` → `yes_levels`
   (best-first); payload `asks` → `no_levels` as their NO-bid complement.
4. Regression tests pinned to the live venue quotes above:
   `tests/test_book_semantics.py`.
5. New DQ **health gate** `crossed_books` (`arbx/data/quality.py`): any row
   with `best_bid > best_ask` fails the recorder acceptance gate. This is
   the check that would have caught the inversion on day one.

## Recovering legacy data

The swap is exact — the stored "bid" ladder is the true ask ladder and vice
versa, sizes included — so no recorded information was lost.
`arbx.data.legacy.unswap_legacy_book_row` recovers a legacy row (idempotent;
stamps `legacy_book_fix: true`), and `derive_edges(..., row_transform=...)`
applies it during re-derivation. `scripts/run_soak_analysis.py --legacy-book-fix`
re-scores old soaks on recovered rows.

Rule of thumb: any data dir whose DQ report fails `crossed_books` must be
read through the corrector; new captures (post-fix) must pass it.

## Legacy captures

Captures produced by the predecessor venue adapters may contain the same
inversion. Their raw ladders remain recoverable through the compatibility
corrector in this repository.
