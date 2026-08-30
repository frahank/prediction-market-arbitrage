# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Corrector for legacy book rows with swapped bid/ask sides.
"""Recover book rows recorded before the bid/ask semantics fix.

Some rows recorded before docs/book_semantics_fix.md have their sides mislabeled: the
normalizers fed ask-side ladders into ``yes_levels``, so the stored
``bid_px_*``/``bid_sz_*`` ladder is actually the ask ladder and vice versa,
and ``best_bid``/``best_ask`` are swapped. The swap is *exact* — no
information was lost — so a legacy row is fully recovered by swapping the two
ladders and recomputing ``spread``.

``mid``, freshness fields, timestamps, and sizes are label-independent and
carry over unchanged. ``book_json`` retains the original (mislabeled)
yes/no-level payload for provenance; corrected rows are stamped
``legacy_book_fix: true`` so downstream tooling can tell recovered rows from
natively-correct ones.
"""
from __future__ import annotations

from typing import Any

_TOP_N = 5


def row_is_swapped(row: dict[str, Any]) -> bool:
    """True when the row's labels are inverted (bid above ask).

    A single venue's book can never be genuinely crossed — its matching
    engine would have traded through it — so bid > ask identifies a
    legacy-labeled row.
    """
    bb, ba = row.get("best_bid"), row.get("best_ask")
    return (
        isinstance(bb, (int, float))
        and isinstance(ba, (int, float))
        and bb > ba
    )


def unswap_legacy_book_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a corrected copy of a legacy book row (idempotent).

    Rows that are not swapped (already-correct captures, one-sided books)
    pass through unchanged except for the ``legacy_book_fix`` stamp, so the
    corrector can be applied blanket-wise to a mixed data dir.
    """
    fixed = dict(row)
    fixed["legacy_book_fix"] = True
    if not row_is_swapped(row):
        return fixed

    fixed["best_bid"], fixed["best_ask"] = row.get("best_ask"), row.get("best_bid")
    for i in range(1, _TOP_N + 1):
        for prefix in ("px", "sz"):
            bid_key, ask_key = f"bid_{prefix}_{i}", f"ask_{prefix}_{i}"
            fixed[bid_key], fixed[ask_key] = row.get(ask_key), row.get(bid_key)
    bb, ba = fixed.get("best_bid"), fixed.get("best_ask")
    if isinstance(bb, (int, float)) and isinstance(ba, (int, float)):
        fixed["spread"] = ba - bb
    return fixed
