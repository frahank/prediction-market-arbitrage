# Finding and verifying your own pairs

The 23 pairs bundled with this repository are a worked example, not a supply.
This document is the end-to-end path for finding cross-venue pairs yourself,
verifying that they actually settle on the same condition, and getting them into
your own run.

**Read this first:** pair selection is the highest-consequence judgement in the
project, and the one no automation here is allowed to make on its own. Two
markets with near-identical titles can settle differently on a detail buried in
the fine print. When that happens you are not holding an arbitrage — you are
holding two positions that can both lose. Everything below is built to slow that
decision down, not to speed it up.

## Why the bundled registry is small, and why it shrinks

Prediction markets expire. That is not an edge case; it is the normal life of
every contract in this domain.

The original research ran against a much larger and constantly churning set of
candidates. Most of them are gone. Elections resolve. Tournaments end. Any
market built on a dated cutoff eventually settles and stops trading, and the
market pair goes with it.

You can see this in the repository itself. `configs/pairs.archived.yaml` holds
three 2026 Men's World Cup continent-winner pairs, each with its full decision
log ending in `archive`. They were live, reviewed, and scannable; the tournament
finished and a routine health check retired them. Two of the 23 pairs still in
the active registry close in December 2026 for the same reason — they are F1
season markets, and that season ends.

So treat the active registry as a snapshot with a date on it, not a feed:

- **It only shrinks.** Nothing here adds pairs automatically.
- **Its equivalence findings age.** Each pair was reviewed against the rules
  text as it read on its approval date. Venues amend resolution criteria after
  listing.
- **Liveness is not equivalence.** `make pair-health` proves both legs still
  answer on the public API. It says nothing about whether they still settle
  together. A pair can be open on both venues and resolve on different
  conditions.

If you want to run this on markets that matter today, you will be finding your
own pairs. The rest of this document is how.

## The pipeline

```
discover  →  match  →  snapshot rules  →  prescreen  →  human audit  →  decide  →  scan
  public      title       pin the           cheap        the actual      recorded    your
  markets    heuristic   exact text        red flags     judgement       + hashed    run
```

Every step before "decide" is read-only and uses public endpoints. No
credentials are required for any of it.

### 1. Discover public markets

```bash
./.venv/bin/python scripts/discover_kalshi_public_markets.py \
    --min-volume-24h 1000 --require-two-sided --output kalshi_markets.json

./.venv/bin/python scripts/discover_polymarket_public_markets.py \
    --min-volume-24h 1000 --output polymarket_markets.json
```

Both take liquidity and spread filters (`--help` lists them). Filtering hard
here is worth it: a pair you cannot trade into is not worth reviewing.

### 2. Build a candidate queue

```bash
./.venv/bin/python scripts/review_pair_candidates.py \
    --kalshi-discovery kalshi_markets.json \
    --polymarket-discovery polymarket_markets.json \
    --candidates configs/pairs.candidates.yaml \
    --list
```

This writes candidates into `configs/pairs.candidates.yaml`, which ships empty.
Matching is a **title heuristic**. It proposes; it never approves. A candidate
is a suggestion that two markets might be about the same thing.

### 3. Snapshot the rules and prescreen

```bash
./.venv/bin/python scripts/audit_pair_equivalence.py <PAIR_KEY_OR_KALSHI_ID> \
    --pairs configs/pairs.candidates.yaml \
    --prompt-out audit_prompt.txt
```

This fetches both venues' current rules text, hashes it into
`evidence/<market>/rules_snapshot.json` so the audit is reproducible, runs the
deterministic prescreen, and writes the rules-diff audit prompt.

The prescreen reports the risks it can check cheaply:

| Check | What it flags |
|---|---|
| Resolution structure | Subjective language — high basis risk even when wording matches verbatim |
| Cutoff | Close-time delta beyond ±1h — a timing-basis window where one venue resolves and the other has not |
| Duration | Long-dated markets where collateral carry dominates any fee-band edge |
| Snapshot warnings | Missing or unretrievable rules text on either leg |

**A clean prescreen is not an approval.** It means the cheap checks found
nothing, and the expensive check — reading the rules — has not happened yet.
The prescreen's own score is `flagged` or `needs_human`. There is deliberately
no `pass`.

### 4. Audit the rules — the part that matters

`audit_prompt.txt` contains both venues' verbatim rules text and a structured
review asking for a grouping check, a date check, a resolution-structure
classification, and a verdict with tail risks and a confidence value.

You can work through it yourself, or hand it to a model as a first pass. The
original research used both: an LLM screen followed by a human read of the
actual contract language. **The model pass is a filter, not an authority.** It
is good at surfacing a grouping mismatch you would skim past; it is not the
thing that should decide your money.

What actually sinks pairs, in the order it showed up in practice:

- **Grouping schemes that do not align.** One venue resolves by confederation,
  the other by geography. "South America" and "CONMEBOL" are close enough to
  look identical and far enough apart to lose.
- **Cutoff windows.** Two markets on the same event with close times 15 hours
  apart have a window where one has resolved and the other has not.
- **Subjective resolution language.** "Definitively states", "announces",
  "normalizes" — wording that requires a judgement call means two venues can
  reach opposite judgements from the same facts.
- **Replacement and cancellation clauses.** What happens if the named person
  withdraws, or the event is postponed? The venues often differ, and it is
  usually the last paragraph nobody reads.

`docs/pair_equivalence_checklist.md` is the standalone human checklist for this
step, and worth reading before your first audit.

To validate a model's response and get the command that records it:

```bash
./.venv/bin/python scripts/audit_pair_equivalence.py <PAIR> --verdict-in response.txt
```

A malformed or missing verdict block is rejected rather than treated as a pass.

### 5. Record the decision

```bash
./.venv/bin/python scripts/pair_decide.py \
    --pair <PAIR_KEY> --decision approve \
    --rationale "read both rules texts; grouping and cutoff align" \
    --auditor "your name"
```

Decisions are append-only. The registry's sha256 sidecar is re-hashed through
the registry helpers, and the loader refuses a registry whose hash does not
match — so a hand-edited registry fails closed rather than scanning quietly.

The consequence is enforced in code, not convention: `include_in_strategy_metrics`
is forced to `false` unless equivalence is verified **and** the latest decision
log entry is `approve`. A pair you have not genuinely approved cannot reach your
reported numbers.

### 6. Check health, then scan

```bash
make pair-health                       # both legs still live and reachable
./run                                  # scan from the cockpit's Paper tab
```

Re-run `pair-health` before any long collection session. Re-run the audit in
step 3–4 whenever a venue may have amended its rules, and archive what has
closed:

```bash
./.venv/bin/python scripts/pair_decide.py --pair <PAIR_KEY> \
    --decision archive --rationale "event resolved 2026-07-19"
```

## Using your own registry

Every tool takes `--pairs`, so you never have to edit the bundled file:

```bash
cp configs/pairs.approved.yaml configs/pairs.mine.yaml
./.venv/bin/python scripts/run_capture.py --pairs configs/pairs.mine.yaml ...
```

To point the cockpit at it, set `pairs_approved_path` in `configs/ui.yaml`.
After any manual edit, refresh the integrity sidecar through the helpers rather
than writing the hash by hand:

```bash
./.venv/bin/python -c "
from pathlib import Path
from arbx.pairs.registry import write_registry_integrity
write_registry_integrity(Path('configs/pairs.mine.yaml'))"
```

## What this will not do for you

- It will not tell you a pair is safe. Every path ends at a person recording a
  decision under their own name.
- It will not keep the registry current. Nothing prunes expired markets for you.
- It will not find you an edge. The bundled registry produced none, and that is
  the documented result of this project — see
  [`FINAL_VERDICT.md`](FINAL_VERDICT.md).
