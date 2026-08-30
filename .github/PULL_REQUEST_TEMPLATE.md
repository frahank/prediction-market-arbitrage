## What this changes

<!-- One or two sentences. Link an issue if there is one. -->

## Why

<!-- What problem does this solve? If it fixes a defect, say how the defect
     shows up rather than just naming the fix. -->

## Checklist

- [ ] `make lint` passes
- [ ] `make typecheck` passes
- [ ] `make test` passes
- [ ] `./run --check` passes
- [ ] New behavior has a test; a bug fix has a test that fails without it

### Safety boundary

This project is paper-only and read-only by design. Confirm:

- [ ] No order-submission or order-cancellation capability is added
- [ ] `tests/test_safety_boundary.py`, `tests/test_ui_safety.py`, and
      `tests/test_accounts_readonly.py` are unmodified, or the change to them
      is explained above
- [ ] `configs/runtime.yaml` still ships in paper mode with real orders disabled
- [ ] No credentials, PEM files, `.env` files, or captured auth headers are
      included

### Pair registry

- [ ] Not applicable
- [ ] Registry changes include rules review, orientation confirmation, a
      decision-log entry, and a refreshed integrity sidecar written through the
      registry helpers

<!-- Matching titles do not prove equivalent settlement. See
     docs/pair_equivalence_checklist.md. -->
