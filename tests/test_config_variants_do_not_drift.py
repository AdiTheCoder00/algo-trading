"""The variant configs must differ from the reference only where they say they do.

`config/goldm.yaml` is the strategy. The two bhavcopy variants exist to change a
named handful of settings so the archive can be run against - and they were made
by copying, which means every later edit to the reference has to be repeated by
hand three times.

It already went wrong once: `max_stale_seconds` was raised to 120 in the
reference (D-115) and left at 10 in both variants, so a run through a variant
would have used a freshness bound nobody chose. This pins the intended
differences so any other divergence fails here instead of being discovered in a
result.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from pydantic import BaseModel

from algo.config.loader import load_config
from algo.config.schema import AppConfig

REFERENCE = pathlib.Path("config/goldm.yaml")

#: What each variant is *allowed* to change, and why. Anything else is drift.
#: Paths are dotted from the config root.
INTENDED: dict[str, dict[str, Any]] = {
    "config/goldm_bhavcopy_frontcycle.yaml": {
        # The archive lists only the front expiry on any session, so the roll
        # (D-104) cannot be exercised against it - see the file's own header.
        "strategy.roll_at_front_dte": None,
        "strategy.cycle_offset": 0,
        "run.name": None,  # free text, compared loosely below
    },
    "config/goldm_bhavcopy_allstrikes.yaml": {
        "strategy.roll_at_front_dte": None,
        "strategy.cycle_offset": 0,
        # The A/B control for D-103: every listed strike, not just the thousands.
        "strategy.strike_multiple": None,
        "run.name": None,
    },
}


def _flatten(model: AppConfig) -> dict[str, Any]:
    """Every leaf setting, keyed by its dotted path."""

    def walk(value: Any, prefix: str) -> dict[str, Any]:
        if isinstance(value, BaseModel):
            out: dict[str, Any] = {}
            for name in type(value).model_fields:
                out.update(walk(getattr(value, name), f"{prefix}.{name}" if prefix else name))
            return out
        return {prefix: value}

    return walk(model, "")


@pytest.mark.parametrize("variant", sorted(INTENDED))
def test_a_variant_differs_only_where_it_declares(variant: str) -> None:
    reference = _flatten(load_config(REFERENCE))
    candidate = _flatten(load_config(pathlib.Path(variant)))
    allowed = INTENDED[variant]

    unexpected = {
        key: (reference.get(key), candidate.get(key))
        for key in reference.keys() | candidate.keys()
        if reference.get(key) != candidate.get(key) and key not in allowed
    }

    assert not unexpected, (
        f"{variant} has drifted from {REFERENCE} on settings it does not claim to "
        f"change (reference, variant): {unexpected}"
    )


@pytest.mark.parametrize("variant", sorted(INTENDED))
def test_the_declared_differences_are_actually_different(variant: str) -> None:
    """The other half. A stale entry in INTENDED would silently widen the
    allowance and let real drift through under its cover."""
    reference = _flatten(load_config(REFERENCE))
    candidate = _flatten(load_config(pathlib.Path(variant)))

    inert = [
        key
        for key in INTENDED[variant]
        if key != "run.name" and reference.get(key) == candidate.get(key)
    ]

    assert not inert, (
        f"{variant} declares it changes {inert}, but those match the reference - "
        "remove the entry rather than leaving an allowance nothing uses"
    )


def test_the_variants_keep_the_settings_that_carry_risk() -> None:
    """The point of the exercise: a variant is a *narrower* run, never a laxer
    one. These are the settings a drifted copy would quietly relax."""
    reference = load_config(REFERENCE)

    for variant in sorted(INTENDED):
        config = load_config(pathlib.Path(variant))

        assert config.risk.starting_equity == reference.risk.starting_equity
        assert config.risk.caps == reference.risk.caps
        assert config.risk.kill_switch == reference.risk.kill_switch
        assert config.risk.devolvement == reference.risk.devolvement
        assert config.strategy.exit == reference.strategy.exit
        assert config.data.quality == reference.data.quality
