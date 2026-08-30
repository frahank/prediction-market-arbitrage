# Scope: TEST — configs/modeling.yaml provenance + coverage (P5-T3).
from __future__ import annotations

import re
from pathlib import Path

from arbx.analysis.survival import SURVIVAL_TIER_COLORS
from arbx.modeling.executable import DEFAULT_MODELING_YAML, load_modeling_config

# A line that assigns a scalar value: "key: value", "- key: value", or a bare
# "- item" list entry. Mapping/sequence headers ("key:") carry no value and
# need no provenance.
_SCALAR_LINE = re.compile(r"^\s*(?:-\s+)?[\w.]+:\s*\S|^\s*-\s+\S")


def test_params_have_provenance_comments():
    text = Path(DEFAULT_MODELING_YAML).read_text(encoding="utf-8")
    missing = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        value_part = line.split("#", 1)[0]
        if _SCALAR_LINE.match(value_part) and "# source:" not in line:
            missing.append(f"{lineno}: {stripped}")
    assert not missing, "modeling.yaml keys without a '# source:' comment:\n" + "\n".join(missing)


def test_fill_probability_covers_every_survival_tier():
    config = load_modeling_config()
    tiers = (config.get("fill_probability") or {}).get("tiers") or {}
    # every tier the survival classifier can emit must have a pinned P(fill)
    assert set(SURVIVAL_TIER_COLORS) <= set(tiers)
    assert all(0.0 <= float(v) <= 1.0 for v in tiers.values())
    assert 0.0 <= float((config["fill_probability"]).get("unprobed")) <= 1.0
