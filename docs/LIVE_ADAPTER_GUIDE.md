# Adapting the research bot for live execution

This document is an engineering map for an experienced developer who chooses to
build a separate live-trading fork or companion package. It is not a live-trading
feature, a turnkey recipe, or financial or legal advice. This repository remains
paper-only: it includes no order submission, cancellation, amendment, wallet
signing, or automatic position-management implementation.

There is deliberately no `live=true` switch. The existing `live` credential
profile only gates loading credentials for authenticated **read-only** account
inspection. `ARBX_ALLOW_LIVE_CREDS=1` does not enable trading.

## Recommended boundary

Keep the execution implementation outside `src/arbx`, ideally in a private
companion package such as `arbx_live`. Treat this repository as an upstream
research dependency and preserve its safety tests unchanged. This separation:

- makes it obvious which package can move money;
- keeps private venue code and operational configuration out of a public repo;
- allows paper-mode updates to be merged without silently weakening the boundary;
- prevents the browser UI from becoming an accidental order-entry surface.

A reasonable companion-package layout is:

```text
arbx_live/
  domain.py            immutable intents, acknowledgements, fills, and reports
  journal.py           durable append-only intent and venue-event journal
  risk.py              pre-trade limits and exposure reservations
  coordinator.py       explicit two-leg state machine
  reconcile.py         venue truth versus local journal
  adapters/
    kalshi.py          one venue adapter
    polymarket.py      one venue adapter
  cli.py               operator-controlled process; no browser order route
```

Do not add order methods to `arbx.accounts`. That package is read-only by type.
Do not turn `ScannerController` or `LiveController` into execution services. The
paper scanner should emit a candidate signal; a separate process should decide
whether a validated signal may become an execution intent.

## What can be reused

| Existing component | Safe reuse in a fork | What it does not prove |
|---|---|---|
| `arbx.pairs.registry.PairSpec` | Market identifiers, orientation, and reviewed metadata | Current settlement equivalence or legal eligibility |
| `arbx.capture.rest_concurrent.ConcurrentRestSource` | Concurrent public books and measured receive skew | Executable price, fill certainty, or atomicity |
| `arbx.capture.kalshi_ws` and `polymarket_ws` | Market-data events | Private order/fill state |
| `arbx.fees.FeeEngine` | Conservative fee estimates | That a pinned schedule is still current |
| `arbx.modeling.executable` | Depth, staleness, and latency haircuts | A promise of realized profit |
| `arbx.accounts.kalshi_auth.KalshiSigner` | Kalshi RSA-PSS request signing | Permission to send a mutating request |
| `arbx.accounts.AccountClient` | Read-only balances, positions, fills, and open orders | Execution, cancellation, or recovery |
| `arbx.exec.KillSwitch` | Durable fail-closed sentinel | Venue-wide cancellation unless the companion wires that behavior explicitly |

Revalidate pair rules, fees, tick sizes, minimum quantities, and venue API
versions at process startup. A committed registry or fee file is never sufficient
for a live decision.

## Execution-domain contract

Define venue-neutral types before writing either adapter. At minimum, an immutable
order intent needs:

- intent ID and deterministic per-venue client order ID;
- pair key, venue, instrument/token ID, outcome orientation, and side;
- limit price, quantity, time-in-force, and hard expiration/deadline;
- the market-data sequence/timestamps and calculated edge that authorized it;
- the exact risk-policy version and limits applied;
- a state indicating simulation, demo, canary, or production.

The adapter boundary should have explicit operations for preflight, submit,
cancel, cancel-all, fetch-one, list-open, and list-fills. Responses should be
typed acknowledgements rather than raw dictionaries and should preserve both the
client ID and venue ID. A timeout must return an **unknown outcome**, not “failed”:
the venue may have accepted an order before the response was lost.

Never retry a submission with a new client ID after an ambiguous timeout. First
query by the original client ID, reconcile open orders and fills, and only then
decide whether another intent is permitted.

## Required state machine

Cross-venue execution is not atomic. Model it as a durable state machine rather
than two adjacent HTTP calls:

```text
OBSERVED
  -> VALIDATED
  -> RISK_RESERVED
  -> LEG_1_SUBMITTING
  -> LEG_1_ACKNOWLEDGED / LEG_1_UNKNOWN / LEG_1_REJECTED
  -> LEG_2_SUBMITTING
  -> HEDGED / PARTIALLY_HEDGED / UNHEDGED / UNKNOWN
  -> RECONCILING
  -> CLOSED or OPERATOR_ATTENTION
```

Persist each transition before causing the next external side effect. On restart,
reconcile every nonterminal intent against venue open orders, fills, balances, and
positions before accepting new work. Do not infer a fill from a changed public
book. Private fill/order streams should accelerate reconciliation, but authenticated
REST remains the recovery source of truth.

The developer must choose and document a failed-leg policy. Possible policies
include cancelling the resting leg, reducing/crossing the unmatched exposure,
or stopping for manual intervention. None is universally safe; each can realize a
loss. A maximum permitted unhedged notional and maximum unhedged time must be hard
limits, not configuration suggestions.

## Pre-trade gates

Every intent should fail closed unless all of these are current and positive:

1. The global kill switch is clear and its cancellation hook is healthy.
2. The operator has explicitly selected demo/canary/production for this process.
3. Venue and jurisdiction eligibility checks pass; never bypass geoblocking,
   account restrictions, KYC, sanctions, or venue terms.
4. Both contracts and their settlement rules were revalidated for equivalence.
5. Both books are fresh, sequence-valid, and within a hard cross-venue skew bound.
6. Prices conform to current tick sizes; quantity meets minimums and available
   depth after a conservative haircut.
7. Fees, collateral, gas/settlement costs, and worst-case slippage are refreshed.
8. Available balances and position limits cover the trade and failed-leg reserve.
9. Per-order, per-market, per-venue, daily-loss, open-order, and gross/net exposure
   caps all pass atomically.
10. The deterministic intent/client IDs have never reached a terminal or ambiguous
    submission state.

Risk reservations must be concurrency-safe. Two signals must not both spend the
same balance or exposure headroom.

## Venue integration notes

### Kalshi

Start in Kalshi's demo environment with separate demo credentials. Production and
demo credentials are not interchangeable. Use the current official authentication,
environment, order-lifecycle, and rate-limit documentation linked below; do not
copy endpoint shapes from historical comments in this repository.

As of the last documentation review (2026-08-21), Kalshi documents a V2
event-market order shape and warns that the legacy order endpoint is being
deprecated. Preserve client order IDs, sign the full path without query parameters,
handle token-based read/write budgets independently, and implement bounded 429
backoff. Demo testing should cover submission, partial/full fill, lookup by client
ID, cancellation, private fill events, restart reconciliation, and kill-switch
cancellation before any production credential is introduced.

### Polymarket

Use the current CLOB V2 SDK or protocol documentation rather than adapting an old
CLOB V1 example. Order authentication and order-payload signing are separate
concerns, and a wallet/private key is involved in signing. A live fork should use
an OS keychain, hardware-backed signer, or dedicated secret manager; do not expose
wallet material to the UI or commit it to configuration.

Polymarket documents geography restrictions and a geoblock preflight endpoint.
Fail closed when eligibility is blocked or cannot be established, and never route
traffic through another region to evade a restriction. Refresh token tick size,
minimum quantity, fee data, and negative-risk status before constructing an order.
Because there is no equivalent local mock-funds environment documented for the
production CLOB flow, exhaustive adapter tests should use fake transports; any
eligible production canary remains an explicit operator decision with the smallest
permitted exposure.

## Test progression

Do not move to the next stage until the previous stage is repeatable and audited.

| Stage | External effects | Exit gate |
|---|---|---|
| 0. Pure simulation | None | Deterministic replay, property tests, and risk invariants pass |
| 1. Fault injection | None | Timeouts, 429/5xx, malformed replies, disconnects, duplicates, and crashes all fail closed |
| 2. Kalshi demo | Mock funds only | Full lifecycle and restart reconciliation pass for days, not one run |
| 3. Shadow production | Reads only | Intended orders are journaled but never submitted; expected versus observed fills reviewed |
| 4. Single-venue canary | Real effect | Explicit operator approval, minimum size, one intent at a time, immediate reconciliation |
| 5. Two-venue canary | Real effect | Failed-leg drills, cancel-all, loss/exposure caps, and on-call procedure proven |

The fault suite should include at least:

- timeout before send, during send, and after venue acceptance;
- duplicate/out-of-order private events and stale public books;
- one leg rejected, partially filled, delayed, or filled after cancellation;
- cancel rejected or acknowledged while a late fill arrives;
- rate limiting, venue maintenance, clock drift, and signature failure;
- process termination after every persisted state transition;
- journal corruption/read-only disk and lost network during reconciliation;
- manual kill switch while each leg is in every nonterminal state.

## Production readiness evidence

An experienced operator should be able to answer “yes” to all of the following
before considering real funds:

- Can every external side effect be tied to one durable, idempotent intent?
- Can a fresh process recover the exact venue state without trusting memory?
- Does the kill switch stop new intents and attempt bounded cancellation while
  still recording late fills?
- Are all risk limits enforced below the strategy layer and impossible for the UI
  or strategy to bypass?
- Does an ambiguous response halt and reconcile instead of retrying blindly?
- Are settlement equivalence and current venue restrictions checked independently
  of market-title similarity?
- Are logs useful without ever containing keys, signatures, wallet secrets, or
  full authenticated headers?
- Is there a human runbook for unhedged exposure, venue outage, and credential
  compromise?

If any answer is no, the adapter is not live-ready.

## Official documentation to re-check

These links are intentionally external because venue contracts change. They were
last reviewed on 2026-08-21:

### Kalshi

- [API environments and endpoints](https://docs.kalshi.com/getting_started/api_environments)
- [API-key authentication and signing](https://docs.kalshi.com/getting_started/api_keys)
- [Demo environment](https://docs.kalshi.com/getting_started/demo_env)
- [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)
- [Cancel Order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2)
- [Rate limits and tiers](https://docs.kalshi.com/getting_started/rate_limits)
- [API changelog](https://docs.kalshi.com/changelog)

### Polymarket

- [CLOB trading overview and authentication](https://docs.polymarket.com/trading/overview)
- [CLOB V2 migration notes](https://docs.polymarket.com/v2-migration)
- [Public market parameters](https://docs.polymarket.com/trading/clients/public)
- [Post-order reference](https://docs.polymarket.com/api-reference/trade/post-a-new-order)
- [Cancel-all reference](https://docs.polymarket.com/api-reference/trade/cancel-all-orders)
- [Rate limits](https://docs.polymarket.com/api-reference/rate-limits)
- [Geographic restrictions](https://docs.polymarket.com/api-reference/geoblock)
- [Documentation changelog](https://docs.polymarket.com/changelog)

The user of a live fork is responsible for current API behavior, account security,
financial risk, taxes, licensing, jurisdiction, and venue terms. The maintainers of
this paper-only repository do not support or certify third-party live adapters.
