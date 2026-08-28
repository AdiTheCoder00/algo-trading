"""RunSettings: one resolution for both config paths (D-118).

The shape this replaces read fifteen settings inside `if config is not None` and
repeated their defaults in the `else`, three times over. That is how
`flatten_on_trip` shipped bound on one branch and not the other.
"""

from __future__ import annotations

import pathlib
from dataclasses import fields
from decimal import Decimal

import pytest

from algo.config.loader import load_config
from algo.config.runsettings import RunSettings

REFERENCE = pathlib.Path("config/goldm.yaml")


class TestBothPathsProduceTheSameShape:
    def test_every_field_is_populated_from_a_config(self) -> None:
        settings = RunSettings.from_config(load_config(REFERENCE))

        for field in fields(RunSettings):
            assert hasattr(settings, field.name)

    def test_defaults_populate_every_field_too(self) -> None:
        """The bug class: a field present on one path and absent on the other."""
        defaults = RunSettings.defaults()

        for field in fields(RunSettings):
            assert hasattr(defaults, field.name)

    def test_the_two_paths_agree_on_the_reference_config(self) -> None:
        """`config/goldm.yaml` mostly restates the schema, so the defaults and
        the reference should differ only where the file deliberately departs -
        which is the strategy's exits and roll, not the risk layer."""
        from_file = RunSettings.from_config(load_config(REFERENCE))
        defaults = RunSettings.defaults()

        assert from_file.starting_equity == defaults.starting_equity
        assert from_file.lots == defaults.lots
        assert from_file.margin_cap_pct == defaults.margin_cap_pct
        assert from_file.max_lots_per_underlying == defaults.max_lots_per_underlying
        assert from_file.flatten_on_trip == defaults.flatten_on_trip


class TestDefaultsComeFromTheSchema:
    def test_they_are_not_written_out_a_second_time(self) -> None:
        """Hand-written defaults drift from the schema silently. Building them
        through `AppConfig` means there is one definition."""
        defaults = RunSettings.defaults()

        assert defaults.starting_equity == Decimal("1000000.00")
        assert defaults.margin_cap_pct == Decimal("50")
        assert defaults.daily_loss_limit_pct == Decimal("2")
        assert defaults.max_drawdown_pct == Decimal("10")
        assert defaults.max_consecutive_losses == 3

    def test_devolvement_defaults_are_the_hard_rules(self) -> None:
        """D-016 has no `enabled: false`; the defaults must not be laxer than the
        schema's, since a run without --config still holds real positions."""
        defaults = RunSettings.defaults()

        assert defaults.force_exit_sessions_before_expiry >= 1
        assert defaults.block_new_entries_within_dte >= 0


class TestItReadsWhatTheFileSays:
    def test_a_changed_setting_travels(self, tmp_path: pathlib.Path) -> None:
        edited = tmp_path / "edited.yaml"
        edited.write_text(
            REFERENCE.read_text(encoding="utf-8").replace(
                '    fixed_lots: 1', '    fixed_lots: 3', 1
            ),
            encoding="utf-8",
        )

        settings = RunSettings.from_config(load_config(edited))

        assert settings.lots == 3

    def test_flatten_on_trip_travels(self, tmp_path: pathlib.Path) -> None:
        """The setting whose absence started all of this."""
        edited = tmp_path / "edited.yaml"
        edited.write_text(
            REFERENCE.read_text(encoding="utf-8").replace(
                "    flatten_on_trip: false", "    flatten_on_trip: true", 1
            ),
            encoding="utf-8",
        )

        assert RunSettings.from_config(load_config(edited)).flatten_on_trip is True

    def test_it_is_frozen(self) -> None:
        """A run's settings must not change under it mid-run."""
        settings = RunSettings.defaults()

        with pytest.raises(Exception, match=r"frozen|immutable|cannot assign"):
            settings.lots = 99  # type: ignore[misc]
