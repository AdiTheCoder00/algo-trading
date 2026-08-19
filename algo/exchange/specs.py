"""Effective-dated contract specifications.

Decision D-010: every constant carries `effective_from` and a `source`. Lot sizes,
tick sizes and per-order caps are revised by the exchange, so a backtest spanning
a revision must use the values in force on each date — not today's values applied
retroactively, which would quietly restate every historical position size.

A date with no spec in force raises. There is no "nearest" fallback: guessing a
lot size is how a backtest produces confident, wrong numbers.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from algo.core.enums import Exchange
from algo.core.errors import ConfigError, SpecError
from algo.core.instrument import InstrumentSpec

SPEC_DIR = Path(__file__).parent / "data"


class ContractSpecStore:
    """All known specifications, indexed by (underlying, exchange)."""

    __slots__ = ("_by_key",)

    def __init__(self, specs: Iterable[InstrumentSpec]) -> None:
        by_key: dict[tuple[str, Exchange], list[InstrumentSpec]] = {}
        for spec in specs:
            by_key.setdefault((spec.underlying, spec.exchange), []).append(spec)
        for entries in by_key.values():
            entries.sort(key=lambda s: s.effective_from)
            _reject_overlaps(entries)
        self._by_key = by_key

    @classmethod
    def from_yaml(cls, path: Path) -> ContractSpecStore:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "specs" not in raw:
            raise ConfigError(f"{path} must contain a top-level 'specs' list")
        entries: list[Any] = raw["specs"]
        return cls(InstrumentSpec.model_validate(entry) for entry in entries)

    @classmethod
    def default(cls) -> ContractSpecStore:
        """Every spec file shipped with the package."""
        files = sorted(SPEC_DIR.glob("spec_*.yaml"))
        if not files:
            raise ConfigError(f"no spec files found in {SPEC_DIR}")
        specs: list[InstrumentSpec] = []
        for file in files:
            raw = yaml.safe_load(file.read_text(encoding="utf-8"))
            specs.extend(InstrumentSpec.model_validate(e) for e in raw["specs"])
        return cls(specs)

    def spec_for(self, underlying: str, exchange: Exchange, on: date) -> InstrumentSpec:
        entries = self._by_key.get((underlying, exchange))
        if not entries:
            raise SpecError(f"no contract specification known for {underlying} on {exchange}")
        for spec in entries:
            if spec.covers(on):
                return spec
        raise SpecError(
            f"no {underlying} specification in force on {on}. Known ranges: "
            + ", ".join(f"{s.effective_from}..{s.effective_to or 'open'}" for s in entries)
        )

    def underlyings(self) -> tuple[tuple[str, Exchange], ...]:
        return tuple(sorted(self._by_key, key=lambda k: (k[0], k[1].value)))


def _reject_overlaps(entries: list[InstrumentSpec]) -> None:
    """Two specs covering the same day would make lot size depend on list order."""
    for earlier, later in pairwise(entries):
        if earlier.effective_to is None:
            raise ConfigError(
                f"{earlier.underlying} spec from {earlier.effective_from} is open-ended "
                f"but is followed by another from {later.effective_from}; close the first."
            )
        if later.effective_from <= earlier.effective_to:
            raise ConfigError(
                f"overlapping {earlier.underlying} specs: "
                f"{earlier.effective_from}..{earlier.effective_to} and from {later.effective_from}"
            )
