# Safety boundary

This repository is paper-only research software. It contains market-data capture,
read-only account inspection, analysis, and a local UI. It does not contain venue
order submission, cancellation, or position-management implementations.

## Enforced invariants

1. **Paper mode is the only shipped mode.** `configs/runtime.yaml` keeps
   `mode: paper`, `real_orders: 0`, and `enable_real_orders: false`.
2. **No order mutations exist.** Source and route scans reject order-placement,
   cancellation, mode-changing, or trading-shaped endpoints and methods.
3. **Account access is read-only.** The Kalshi account client can list balances,
   positions, fills, and resting orders with GET requests only. It exposes no
   mutating HTTP method.
4. **Credentials stay outside the repository.** Credential references are stored
   under `~/.arbx/credentials/` with restrictive permissions. Private keys and
   secret values are never written into this checkout.
5. **The UI cannot bypass services.** UI modules do not import venue or capture
   adapters directly. They call named application-service operations, and those
   operations expose no order or mode mutation.
6. **Runtime shutdown is fail-closed.** The kill switch stops managed activity and
   can only be cleared manually outside the UI.

Kalshi WebSocket authentication does not change this boundary: those credentials
authorize market-data streaming and read-only account requests, not trading through
this codebase.

Experienced users considering a separate implementation should read
[`LIVE_ADAPTER_GUIDE.md`](LIVE_ADAPTER_GUIDE.md). The guide documents extension
seams and acceptance gates; it does not relax any invariant in this file or add an
order-capable path to this repository.

## Regression tests

The main guards are:

- `tests/test_safety_boundary.py` — scans the source tree for order endpoints,
  verifies paper defaults, and proves the UI/service tree is included;
- `tests/test_ui_safety.py` — rejects direct UI imports of venue/capture adapters
  and rejects order- or mode-shaped routes and service operations;
- `tests/test_accounts_readonly.py` — permits the Kalshi resting-order GET while
  proving the account client has no mutating request path;
- `tests/test_secrets.py` — scans committed files for private keys and populated
  credentials and validates the empty credential templates;
- `tests/test_killswitch.py` — verifies fail-closed runtime behavior.

This is a code boundary, not a claim that public market data is safe, complete, or
suitable for financial decisions. API terms, jurisdictional rules, and venue access
requirements remain the user's responsibility.
