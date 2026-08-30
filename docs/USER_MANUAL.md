# Research Cockpit — Operator's Manual

*A beginner's guide to running the archived paper-trading research bot. The
installed Python package is named `arbx`.*

> **Project status:** the research verdict is failed / not viable. This manual is
> retained so the data-collection and analysis tooling remains usable. See
> [`FINAL_VERDICT.md`](FINAL_VERDICT.md).

---

## 0. The one thing to know first

**This system cannot place real orders. Anywhere. At all.**

- `configs/runtime.yaml` ships `mode: paper`, `real_orders: 0`,
  `enable_real_orders: false`, and the test suite fails if that ever changes.
- There is no code path, button, API route, or config flag that submits an
  order to a venue. Tests scan the codebase for order endpoints and
  order-shaped methods on every run.
- The Kalshi credentials described in §7 grant **read-only** account viewing
  (balance, positions, resting orders, fills). They add zero trading ability.
- A **kill switch** (§8) stops all runtime activity and can only be cleared by
  you, manually, at the terminal.

All commands below run from the repository root:

```bash
cd /path/to/prediction-market-arbitrage
```

## 1. Setup and launch

Python 3.12 or newer is the only runtime prerequisite. From the cloned
repository, run:

```bash
./run
```

On the first run, the launcher creates `.venv`, installs the pinned runtime,
offers to collect and locally validate a Kalshi read-only key, and starts the
cockpit. Credential input is hidden. Choose `n` to use the public REST paths
without credentials.

For an offline installation/safety check without starting the UI:

```bash
./run --check
```

## 2. Launching the cockpit

Normally `./run` launches the cockpit. After setup, `./.venv/bin/arbx-ui` is
the equivalent direct command.

Open **http://127.0.0.1:8710**. You land on the Paper tab. The five tabs:

| Tab | URL | What it is for |
|---|---|---|
| **Paper** | `/paper` | Start/stop the scanner, run Full Analysis, run the test suite |
| **Pairs** | `/pairs` | Review the pair registry: approve, reject, archive |
| **Data** | `/data` | Browse recorded soak runs and their rows |
| **Documents** | `/docs-viewer` | Read repo docs and notes in the browser |
| **Access & Safety** | `/live` | Kill switch, credential storage, and read-only account badges |

Stop the UI with `Ctrl+C` in its terminal. Restarting it never affects a
scanner soak started separately from the command line.

## 3. Reviewing and approving pairs (Pairs tab)

A "pair" is one Kalshi market matched with one Polymarket market that should
settle identically. The registry flows through three buckets:

1. **Needs approval** — locally discovered candidates waiting for a human decision.
2. **Approved** — pairs you have personally confirmed; these live in
   `configs/pairs.approved.yaml`.
3. **Archived** — rejected or retired pairs (`configs/pairs.archived.yaml`).

Flow: open the Pairs tab → the *Needs approval* queue → click a pair to see
its summary (market titles, rules snapshots, equivalence status) → choose
Approve / Reject / Archive. Every decision has an **are-you-sure** step: you
must type the exact confirmation phrase, which is the decision in capitals
followed by the pair key, e.g.

```text
APPROVE kalshi:KXEXAMPLE-26|poly:0x1234…
```

The dialog shows you the exact expected phrase. Approving promotes the pair
into `pairs.approved.yaml` (with a sha256 integrity sidecar); rejecting or
archiving files it into `pairs.archived.yaml`. Decisions are recorded with
your reviewer name and notes.

**Approval ≠ trading.** Approval only marks a pair eligible for *scanning and
analysis*. Nothing trades.

## 4. Running a scanner soak

The scanner reads one reviewed registry file. The public snapshot includes
`configs/pairs.approved.yaml`; new candidates can be produced by the discovery
and review commands before being added through the review workflow.

The scanner and concurrent REST capture use public market-data endpoints. They do
**not** require a Kalshi key or a Polymarket key.

Then start an **edges-only, triple-probe soak** (the standard research run —
records every detected edge and re-checks each one after 100, 200, and 400 ms
to see if it survives):

```bash
./.venv/bin/python scripts/run_scanner.py \
  --pairs configs/pairs.approved.yaml \
  --data-dir "data/soaks/scan_$(date +%Y%m%d-%H%M%S)" \
  --edges-only \
  --confirm-survival-ms-list 100,200,400
```

Useful extras: `--duration 86400` (stop after 24 h; default runs until
killed), `--batch-size 20` (pairs scanned per rotation batch), `--tick-s 1.0`
(seconds between ticks), `--run-id my-label`. Without `--edges-only` the
scanner also records full order-book rows (much more disk).

Check on a running soak:

```bash
# Which batch is it on and how many pairs?
cat data/soaks/<run-id>/scan_state.json
# How many edge rows so far?
wc -l data/soaks/<run-id>/EDGES_*.jsonl
```

or watch the scanner status card on the **Paper** tab. Stop a soak with
`Ctrl+C` (or engage the kill switch, §8).

## 5. Full Analysis (Paper tab)

On the Paper tab, pick the soak(s) and press **Full Analysis**. Reading the
outputs:

- **Edges** — fee-adjusted edge sizes over time per pair. A real transient
  arbitrage looks like a brief spike that vanishes; treat magnitude with
  suspicion until it passes the checks below.
- **Survival probes** — did the edge still exist 100 / 200 / 400 ms later?
  An edge that never survives even 100 ms is not executable by anyone slower
  than that (i.e., us).
- **Basis-suspect flags** — an "edge" that persists for hours is not free
  money; it is a *basis*: the two markets don't actually settle identically
  (or a fee/rule difference). Persistent edge ⇒ suspect the pair's
  equivalence, not your luck.
- **Skew gates** — an edge computed from two books captured at very different
  moments is an artifact. Only rows where the two venues were captured within
  a small inter-leg skew (≤ 25 ms is the clean bar) are trustworthy evidence.

## 6. The Access & Safety tab

Everything on this tab is about authenticated data access and safe shutdown,
not trading.

The hero strip shows the runtime mode (always **PAPER**), whether real orders
are enabled (always **Off**), and the kill-switch state.

## 7. Kalshi credentials (read-only)

Credentials are optional for polling and required only for authenticated Kalshi
WebSocket/account reads. True two-venue event-driven recording therefore requires
a Kalshi API key; the Polymarket market-data stream is public and needs no key.

To see your real Kalshi balance/positions/orders/fills in read-only form, the
bot needs an API key. Create one in your Kalshi account settings — you get a
**key ID** and download a **PEM private-key file**. Protect the PEM file:

```bash
chmod 600 /path/to/your/kalshi-key.pem
```

**Option A — CLI (recommended):**

```bash
./.venv/bin/arbx-store-credentials kalshi live
```

It prompts (input hidden) for `api_key_id` and `private_key_pem_path`, then
writes `~/.arbx/credentials/kalshi.live.yaml` with permissions 600 and prints
only the path — never the values.

**Option B — Access & Safety tab:** fill the "Kalshi live" credential form. Same storage,
same rules.

What "read-only" means here:

- The stored key is used to *sign requests that read* account state:
  balance, positions, resting orders, fills — plus the authenticated
  market-data WebSocket.
- There is **no code that can place or cancel an order**, and a test scans
  every account class on every run to keep it that way.
- Loading `live`-profile credentials additionally requires the environment
  gate `ARBX_ALLOW_LIVE_CREDS=1`; without it the stored file is inert. To see
  the "CONNECTED (READ-ONLY)" badge on the Access & Safety tab, start the UI with the
  gate set:

```bash
ARBX_ALLOW_LIVE_CREDS=1 ./.venv/bin/python scripts/run_ui.py
```

- Credential values are registered with the redaction layer so they can never
  appear in logs, errors, or the UI. Secrets never enter the repo.

The per-venue badge in the Credentials panel shows **CONNECTED (READ-ONLY)**
once a cheap authenticated balance read succeeds (re-checked at most once a
minute). Polymarket always shows NOT CONNECTED — its read-only client is not
built yet.

## 8. The KILL switch

Engage it from the Access & Safety tab: type a reason and press **KILL SWITCH**. This
writes a sentinel file at `~/.arbx/KILL`; every runtime component refuses to
run while it exists.

**There is deliberately no un-kill button.** Clearing it is a manual,
operator-only act at the terminal:

```bash
rm ~/.arbx/KILL
```

The environment variable `ARBX_KILL=1` also forces the killed state for any
process started with it (cleared by unsetting the variable).

## 9. Safety boundaries (summary)

| Boundary | Enforced by |
|---|---|
| Paper only, deny-by-default | `configs/runtime.yaml` + `tests/test_safety_boundary.py` |
| No order endpoints in the tree | `tests/test_safety_boundary.py` (string scan, allowlist of 1 read-only file) |
| Account clients read-only by type | `tests/test_accounts_readonly.py` |
| No UI route can trade or flip mode | `tests/test_ui_safety.py`, `tests/test_ui_live_routes.py` |
| No secrets in the repo | `tests/test_secrets.py` tree scan |
| Kill switch, manual clear only | `tests/test_killswitch.py` |

## 10. Troubleshooting

- **UI port already in use** — another cockpit is running. Find it with
  `lsof -i :8710` and stop it, or change `port` in `configs/ui.yaml`.
- **`ModuleNotFoundError` / venv broken** — recreate the venv (§1).
- **Tests fail after you edited configs** — approved and archived registries have
  `.sha256` integrity sidecars; use the review workflow rather than hand-editing.
- **"credential file … has permissions … refusing"** — the store requires 600:
  `chmod 600 ~/.arbx/credentials/kalshi.live.yaml` (same for your PEM file).
- **"loading live credentials requires ARBX_ALLOW_LIVE_CREDS=1"** — expected;
  set the env gate only when you intend read-only account access (§7).
- **Badge says NOT CONNECTED with "balance probe failed with status 401"** —
  the key ID and PEM don't match a valid Kalshi API key; re-create the key and
  re-store.
- **Everything refuses to run / "kill switch engaged"** — someone (you)
  engaged the kill switch. Read the reason on the Access & Safety tab; clear it with
  `rm ~/.arbx/KILL` when you are satisfied.
- **A soak seems stuck** — check `scan_state.json` cursor movement and the log
  file you started it with; a venue outage shows up as retries in the log, and
  the scanner resumes by itself.
