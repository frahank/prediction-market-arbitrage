# Contributing

This is an archived, paper-only research project. Contributions that improve
reproducibility, read-only data collection, analysis correctness,
documentation, or safety are welcome. Order creation, cancellation, or other
live-execution capability is out of scope.

## Local setup

Python 3.12 or newer is required on macOS or Linux.

```bash
./run --check
./.venv/bin/python -m pip install -r requirements-dev.lock
make lint
make test
make build
./.venv/bin/python scripts/verify_distribution.py dist
```

Tests must not require personal credentials. The credentialed Kalshi
WebSocket integration remains explicitly opt-in and is skipped by default.

## Safety rules

- Keep the runtime in paper mode and preserve the no-order boundary.
- Never commit credentials, PEM files, `.env` files, captured authorization
  headers, or generated research datasets.
- Store test fixtures under `tests/fixtures/` and use unmistakably fake
  credential canaries.
- Update registry integrity sidecars through the existing registry helpers,
  not by manually editing their hashes.
- Run the complete release check before opening a pull request.

## Pair-registry changes

Matching titles do not prove equivalent settlement. New pairs require rules
review, orientation confirmation, a decision-log entry, and a refreshed
integrity sidecar. Run `make pair-health` before proposing an active-registry
change, but treat venue availability as health evidence rather than proof of
contract equivalence.
