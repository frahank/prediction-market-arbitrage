# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-08-30

First public release. This is the paper-only, read-only research subset of a
larger private system; it ships 23 reviewed market pairs rather than the full
97, and carries no execution layer, database tier, or live-order path.

### Added

- Read-only public market-data clients for Kalshi and Polymarket.
- Concurrent REST and WebSocket capture with monotonic receive stamps,
  cross-venue skew measurement, and freshness classification.
- Order-book normalization, including the bid/ask semantics correction that
  removed the project's earlier apparent profitability.
- Pair registries with settlement-equivalence review state, sha256 integrity
  sidecars, and a deny-by-default strategy-eligibility gate.
- Per-venue fee models priced in `Decimal`, composed by a cross-venue
  `FeeEngine` that resolves unknown markets to a conservative worst case.
- Executable-edge, episode, survival, latency, and expected-value analysis.
- A local FastAPI research cockpit on loopback, with a strict
  Content-Security-Policy and a request-parameter allowlist.
- Deny-by-default paper mode behind three independent gates, a kill switch with
  no un-engage method, and a credential store outside the checkout at mode 600.
- 416 tests, including architecture tests that assert account clients stay
  read-only by type and that no live-execution module exists.
- `docs/ARCHITECTURE.md`, documenting the layering, the three extension seams,
  what adding a third venue would actually cost, and the module cycles that
  exist today.

### Security

- Value-targeted redaction covering PEM blocks, JWTs, `Authorization`,
  `Cookie`, and `KALSHI-ACCESS-*` header values, and sensitive `key: value`
  pairs. The previous implementation matched sensitive *words* and blanked the
  label while leaving the adjacent secret intact.
- Documented threat model for the cockpit: it has no authentication, and its
  Host and Origin checks stop hostile web pages but not other local processes.

### Fixed

- Scanner subprocess streams are written to files in the run directory instead
  of to pipes the parent could not drain, removing a deadlock that would have
  hung any run whose child wrote past the OS pipe buffer.
- `./run` no longer exits non-zero when stdin is not a terminal.
- The scanner's command allowlist is a real check; it was previously a
  tautology that could not fail.
- The `[hidden]` attribute now hides elements whose class sets `display`,
  which had left a red "KILL ENGAGED" banner permanently visible while the
  kill switch read "Clear".

[Unreleased]: https://github.com/frahank/prediction-market-arbitrage/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/frahank/prediction-market-arbitrage/releases/tag/v0.1.0
