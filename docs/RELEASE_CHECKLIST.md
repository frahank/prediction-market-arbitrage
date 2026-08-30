# Public release checklist

## Status — release submitted

Updated 2026-08-21.

The local paper-only release gate is complete. The repository owner reports that
the remaining credentialed verification, public-repository review, and submission
steps are also complete. No credential values are recorded in this document.

This checkout currently has no local commit history or Git remote, so the external
publication state is owner-reported rather than independently verifiable from this
copy. Sync or clone the submitted repository before using local Git history as the
publication record.

## Completed release gate

- [x] `./run --check` completes without credentials.
- [x] `make lint` and the complete test suite pass.
- [x] The analytics environment and `tests/test_compaction.py` pass.
- [x] The wheel and source archive build successfully.
- [x] `scripts/verify_distribution.py` confirms packaged UI assets.
- [x] Public pair health reports 23 of 23 active pairs healthy; three completed
  pairs were archived with updated integrity hashes.
- [x] Secret, paper-boundary, registry-integrity, and release-candidate tests pass.
- [x] Ignored local data, credentials, logs, environments, caches, and build
  artifacts were reviewed for exclusion from the public tree.
- [x] The owner reports completing the interactive least-privilege Kalshi
  credential/WebSocket check locally.
- [x] The localhost UI was verified on `127.0.0.1`; all intended tabs, packaged
  assets, and the status API rendered while paper mode and zero real orders
  remained enforced.
- [x] The owner reports completing the repository name, license, public copy,
  initial submission, and publication review.

## Verification snapshot

- 385 tests collected: 384 passed and the real-credential Kalshi WebSocket test
  was skipped during the automated no-credential run; the owner subsequently
  reports completing that credentialed check.
- Lint and dependency-integrity checks passed.
- Fresh-copy one-command bootstrap passed on macOS.
- Linux CI is configured in `.github/workflows/ci.yml`; its external run result is
  part of the owner-reported submission rather than locally inspectable here.
- Public read-only smoke tests produced 23 paired snapshots and a bounded paper
  scanner run.
- The project still contains no order-submission or cancellation implementation.

## Re-run before any later release

Run this checklist from a clean clone on Python 3.12:

1. `./run --check` completes without credentials.
2. `make lint` and `make test` pass.
3. Install `requirements-analytics.lock` and run `tests/test_compaction.py`.
4. `make build` creates one wheel and one source archive.
5. `./.venv/bin/python scripts/verify_distribution.py dist` passes.
6. `make pair-health` reports every active pair healthy, or unavailable pairs
   are reviewed and explicitly retired before release.
7. `tests/test_secrets.py` and `tests/test_safety_boundary.py` pass from the
   exact tree that will be committed.
8. Review `git status --ignored` for local data, credentials, logs, and editor
   artifacts.
9. Test `./run` interactively with a newly generated least-privilege Kalshi
   read-only key. Enter the key locally; never paste it into an issue or chat.
10. Confirm the UI binds only to `127.0.0.1`, all tabs render, and the Access &
    Safety page still reports paper mode with real orders disabled.

Publishing, repository creation, and credentialed verification require the
repository owner's explicit authorization. They are intentionally outside the
automated release workflow.
