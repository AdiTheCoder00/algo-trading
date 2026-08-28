"""Every config field must decide something, or refuse (D-115).

A scan found 21 of 79 fields that nothing ever read. That is worse than a missing
setting: `flatten_on_trip: false` and `allow_unverified_calendar: false` both
read as safety policy and neither was consulted, so the file described a system
that did not exist.

The rule this file pins: a field either changes behaviour, or an unsupported
value raises at load. What must never happen again is a value being accepted and
silently ignored - so each test below sets the unsupported value and asserts the
load fails.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest
from pydantic import ValidationError

from algo.config.schema import AppConfig

SCHEMA = pathlib.Path("algo/config/schema.py")


def _load(**overrides: Any) -> AppConfig:
    base: dict[str, Any] = {"mode": "backtest", "instruments": [{"underlying": "GOLDM"}]}
    base.update(overrides)
    return AppConfig(**base)


class TestUnsupportedValuesAreRefused:
    """Each of these is a field whose behaviour is fixed in code. Offering the
    knob is fine; accepting a value that does nothing is not."""

    def test_a_non_ist_exchange_timezone_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="Asia/Kolkata"):
            _load(market={"timezone": "Europe/London"})

    def test_a_different_dst_reference_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="America/New_York"):
            _load(market={"dst_reference_zone": "Europe/London"})

    def test_acting_on_the_partial_bar_is_refused(self) -> None:
        """D-014 makes this structural in the strategy, not configurable."""
        with pytest.raises(ValidationError, match="not implemented"):
            _load(market={"bar": {"act_on_partial_bar": True}})

    @pytest.mark.parametrize("field", ["reject_crossed_quotes", "reject_empty_book"])
    def test_quality_rejects_cannot_be_switched_off(self, field: str) -> None:
        """`Quote.status` enforces both unconditionally; accepting `false` would
        advertise an opt-out that does not exist."""
        with pytest.raises(ValidationError, match="not supported"):
            _load(data={"quality": {field: False}})

    def test_tick_exits_are_refused(self) -> None:
        """Q15: `check_exit` holds the intrabar logic and is wired to nothing."""
        with pytest.raises(ValidationError, match="not implemented"):
            _load(strategy={"exit": {"evaluate_on": "tick"}})

    def test_the_supported_values_all_load(self) -> None:
        """The other half - a refusing validator that refuses everything would
        pass every test above while breaking the product."""
        config = _load()

        assert config.market.timezone == "Asia/Kolkata"
        assert config.market.bar.act_on_partial_bar is False
        assert config.data.quality.reject_crossed_quotes is True
        assert config.strategy.exit.evaluate_on == "bar_close"


class TestNoFieldIsSilentlyIgnored:
    """The scan itself, kept as a test so the class of bug cannot come back."""

    def _fields(self) -> list[tuple[str, str]]:
        tree = ast.parse(SCHEMA.read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        name = stmt.target.id
                        if not name.startswith("_") and name != "model_config":
                            out.append((node.name, name))
        return out

    def _read_or_validated(self) -> set[str]:
        names: set[str] = set()
        # Read anywhere in the package or its tests...
        for path in list(pathlib.Path("algo").rglob("*.py")) + list(
            pathlib.Path("tests").rglob("*.py")
        ):
            if path == SCHEMA:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.keyword) and node.arg:
                    names.add(node.arg)
        # ...or guarded by a validator that refuses unsupported values.
        tree = ast.parse(SCHEMA.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    # `field_validator` is imported by name here, so the
                    # decorator is a Name - checking only `.attr` (the
                    # `pydantic.field_validator` form) silently matched nothing.
                    func = dec.func
                    label = getattr(func, "id", None) or getattr(func, "attr", "")
                    if label == "field_validator":
                        names.update(
                            a.value
                            for a in dec.args
                            if isinstance(a, ast.Constant) and isinstance(a.value, str)
                        )
        return names

    def test_every_field_is_read_or_guarded(self) -> None:
        known = self._read_or_validated()
        inert = [f"{c}.{f}" for c, f in self._fields() if f not in known]

        assert inert == [], (
            "these config fields are accepted and then ignored - either wire them "
            f"up, guard them with a refusing validator, or delete them: {inert}"
        )
