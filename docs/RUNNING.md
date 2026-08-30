# Running it

Setup, the no-credential paths, and Kalshi-authenticated streaming. The
[README](../README.md) covers what the project is and what it found; this file
covers how to operate it.

## Developer setup

Python 3.12 is required.

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements-dev.lock
./.venv/bin/pip install --no-build-isolation --no-deps -e .
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check src tests scripts
```

`requirements.lock`, `requirements-dev.lock`, and
`requirements-analytics.lock` pin the complete supported environments. CI
runs the bootstrap, tests, lint, and distribution checks on both macOS and
Linux. The optional analytics tier has its own Linux test job.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development workflow and
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) for the exact local
release gate.

## No-key polling examples

Discover public markets:

```bash
./.venv/bin/python scripts/discover_kalshi_public_markets.py --help
./.venv/bin/python scripts/discover_polymarket_public_markets.py --help
```

Run concurrent public REST capture with a reviewed pair registry:

```bash
./.venv/bin/python scripts/run_capture.py \
  --mode rest_concurrent \
  --pairs configs/pairs.approved.yaml \
  --data-dir data/soaks/example-rest \
  --duration 600
```

These paths use public read endpoints and do not need a Kalshi or Polymarket key.
They still make network requests and remain subject to venue availability, rate
limits, terms, and jurisdictional restrictions.

## Kalshi-authenticated streaming

Kalshi WebSocket market data requires an API key ID plus its RSA PEM. Store the
credential outside the repository:

```bash
./.venv/bin/arbx-store-credentials kalshi paper
```

The command writes a mode-600 YAML reference under
`~/.arbx/credentials/`; the PEM stays at the external path you provide. Then run:

```bash
./.venv/bin/python scripts/run_streaming_soak.py \
  --kalshi-stream ws \
  --kalshi-profile paper \
  --pairs configs/pairs.approved.yaml \
  --data-dir data/soaks/example-streaming \
  --hours 1
```

Never commit credential YAML, PEM files, `.env` files, wallet keys, or generated
auth headers. See [`SECURITY.md`](SECURITY.md) and
[`docs/USER_MANUAL.md`](docs/USER_MANUAL.md) for the operational details.
