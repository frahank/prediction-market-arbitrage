# Security

This public snapshot is intended for paper research and read-only market-data
collection. It does not include live credentials and does not support placing or
cancelling orders.

## Reporting a vulnerability

Report privately through GitHub Security Advisories ("Security" → "Report a
vulnerability"). Please do not open a public issue for a security report.

This is archived research software maintained on a best-effort basis. Expect an
acknowledgement within about two weeks; there is no guaranteed patch timeline.

**In scope:** credential handling, the local cockpit's request handling, and
anything that could cause the paper-only boundary to place a real order.

**Out of scope:** exposing the cockpit outside loopback — it is designed for
127.0.0.1 only and enforces that at startup — and venue-side API behaviour.

## Threat model for the local cockpit

The cockpit has **no authentication**. It is a single-user research tool, and
its boundary is built from two request checks rather than a login:

- the `Host` header must name a loopback address, which rejects a rebound DNS
  name that resolves to 127.0.0.1;
- an `Origin` header, when the browser sends one, must also be loopback, and
  write operations must carry `Content-Type: application/json`, which forces a
  CORS preflight that this app — serving no CORS headers — always fails.

Together those stop a web page in the operator's browser from driving the
cockpit. **They do not stop another process on the same machine.** Any local
process, running as any user, can send `Host: 127.0.0.1` with no `Origin` header
and reach every operation, including `store_credentials`. A non-browser client is
not subject to preflight and there is no token for it to be missing.

This is accepted for a single-user research tool on a trusted workstation. It
means:

- **Do not run the cockpit on a shared, multi-user, or multi-tenant host.**
- Prefer `arbx-store-credentials` (the CLI) over the cockpit's credential form.
  The CLI writes the same mode-600 file without putting key material on an
  unauthenticated HTTP endpoint.
- Treat the cockpit port as equivalent to a local shell: anything that can reach
  it can do what you can do in it.

Responses carry `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`. The CSP allows no
inline script or style, so the Documents tab's rendered-Markdown pane cannot
execute injected markup even if the server-side renderer's `html=False` setting
were lost.

## Credential rules

- Never commit `.env` files, PEM files, private keys, wallet keys, credential
  YAML, API secrets, passphrases, cookies, or captured authorization headers.
- Store venue credentials outside the repository. The built-in credential store
  uses `~/.arbx/credentials/` with mode-600 files.
- Use a least-privilege Kalshi key for authenticated market-data streaming.
- Treat every generated signature and bearer value as sensitive even when it is
  short-lived.
- Do not add an order-capable client without a separate security and safety review.

If a real credential is ever committed, revoke or rotate it immediately. Removing
the line in a later commit is not enough; the credential remains in Git history.
Rewrite the affected history before publishing again.

## Public-release checks

Before publishing a fork, run:

```bash
./run --check
./.venv/bin/python -m pytest tests/test_secrets.py tests/test_safety_boundary.py -q
rg --hidden -g '!/.git/**' -g '!data/**' -g '!logs/**' \
  'BEGIN .*PRIVATE KEY|api_secret: *[^"'"' ]|wallet_private_key: *[^"'"' ]'
```

Review every match. Empty credential templates and explicit fake test canaries are
expected; real values are not.
