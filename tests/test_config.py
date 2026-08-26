"""Configuration loading and the live-trading gate. Brief §2.1 and §2.5."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from algo.config.loader import config_hash, load_config
from algo.config.modes import LIVE_ENV_VAR, resolve_mode
from algo.config.schema import AppConfig
from algo.core.enums import Mode
from algo.core.errors import ConfigError, ModeError

REFERENCE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "goldm.yaml"


class TestLiveGate:
    """Live requires the config, the env var and the CLI flag to all agree."""

    def test_backtest_needs_no_ceremony(self) -> None:
        assert resolve_mode(Mode.BACKTEST, env={}) is Mode.BACKTEST

    def test_paper_needs_no_ceremony(self) -> None:
        assert resolve_mode(Mode.PAPER, env={}) is Mode.PAPER

    def test_live_without_the_env_var_is_refused(self) -> None:
        with pytest.raises(ModeError, match="TRADING_MODE is unset"):
            resolve_mode(Mode.LIVE, env={}, real_money_flag=True)

    def test_live_without_the_flag_is_refused(self) -> None:
        with pytest.raises(ModeError, match="i-understand-this-is-real-money"):
            resolve_mode(Mode.LIVE, env={LIVE_ENV_VAR: "live"}, real_money_flag=False)

    def test_live_with_neither_is_refused(self) -> None:
        with pytest.raises(ModeError):
            resolve_mode(Mode.LIVE, env={}, real_money_flag=False)

    def test_live_with_everything_is_allowed(self) -> None:
        assert (
            resolve_mode(Mode.LIVE, env={LIVE_ENV_VAR: "live"}, real_money_flag=True) is Mode.LIVE
        )

    def test_env_and_config_disagreeing_is_refused(self) -> None:
        """The opposite mistake: env says live, config says backtest. Do not guess."""
        with pytest.raises(ModeError, match="Refusing to guess"):
            resolve_mode(Mode.BACKTEST, env={LIVE_ENV_VAR: "live"}, real_money_flag=True)

    def test_there_is_no_default_that_reaches_live(self) -> None:
        """§2.1: no default, no fallback, no 'if unset assume live'."""
        for junk in ("", "LIVE ", "true", "1", "yes", "prod"):
            with pytest.raises(ModeError):
                resolve_mode(Mode.LIVE, env={LIVE_ENV_VAR: junk}, real_money_flag=True)


class TestReferenceConfig:
    def test_it_loads(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={})
        assert config.mode is Mode.BACKTEST
        assert config.instruments[0].underlying == "GOLDM"

    def test_money_values_are_decimals_not_floats(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={})
        assert isinstance(config.risk.starting_equity, Decimal)
        assert config.risk.starting_equity == Decimal("1000000.00")
        assert isinstance(config.strategy.exit.take_profit_value, Decimal)
        assert config.strategy.exit.take_profit_value == Decimal("4")
        assert isinstance(config.strategy.exit.stop_loss_value, Decimal)
        assert config.strategy.exit.stop_loss_value == Decimal("1")
        assert isinstance(config.strategy.strike_multiple, Decimal)
        assert config.strategy.strike_multiple == Decimal("1000")

    def test_the_answers_given_are_what_is_configured(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={})
        assert config.risk.sizing.mode == "fixed_lots"
        assert config.risk.sizing.fixed_lots == 1
        assert config.strategy.cadence == "per_expiry_cycle"
        assert config.strategy.exit.stop_loss_kind == "PCT_OF_MARGIN_AT_ENTRY"
        assert str(config.strategy.entry_bars_ist[0]) == "09:30:00"

    def test_the_reference_config_runs_without_a_stop(self) -> None:
        """D-102. Pinned explicitly: `stop_loss_value` is still 1 in the file, so
        nothing but this flag distinguishes a run with a stop from one without,
        and a silent revert would otherwise look like a passing test."""
        config = load_config(REFERENCE_CONFIG, env={})
        assert config.strategy.exit.no_stop_loss is True

    def test_the_reference_config_rolls_into_the_next_cycle(self) -> None:
        """D-104: enter on the front cycle's expiry day, sell the one after."""
        config = load_config(REFERENCE_CONFIG, env={})
        assert config.strategy.roll_at_front_dte == 0
        assert config.strategy.cycle_offset == 1

    def test_config_is_frozen(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={})
        with pytest.raises(Exception, match=r"frozen|immutable"):
            config.mode = Mode.LIVE


class TestFloatMoneyRejection:
    def test_unquoted_money_is_refused_with_a_fix(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "mode: backtest\n"
            "instruments:\n  - underlying: GOLDM\n"
            "risk:\n  starting_equity: 1000000.00\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="unquoted YAML float"):
            load_config(path, env={})

    def test_quoted_money_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "good.yaml"
        path.write_text(
            "mode: backtest\n"
            "instruments:\n  - underlying: GOLDM\n"
            'risk:\n  starting_equity: "1000000.00"\n',
            encoding="utf-8",
        )
        assert load_config(path, env={}).risk.starting_equity == Decimal("1000000.00")


class TestOverrides:
    def test_env_overrides_yaml(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={"ALGO_RUN__SEED": "99"})
        assert config.run.seed == 99

    def test_cli_overrides_env(self) -> None:
        config = load_config(
            REFERENCE_CONFIG, overrides={"run.seed": 7}, env={"ALGO_RUN__SEED": "99"}
        )
        assert config.run.seed == 7

    def test_unknown_keys_are_refused(self, tmp_path: Path) -> None:
        """A typo in a config key must not be silently ignored."""
        path = tmp_path / "typo.yaml"
        path.write_text(
            "mode: backtest\n"
            "instruments:\n  - underlying: GOLDM\n"
            'risk:\n  strating_equity: "1"\n',  # deliberate typo
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(path, env={})


class TestConfigHash:
    def test_is_stable_across_loads(self) -> None:
        a = load_config(REFERENCE_CONFIG, env={})
        b = load_config(REFERENCE_CONFIG, env={})
        assert config_hash(a) == config_hash(b)

    def test_changes_when_a_parameter_changes(self) -> None:
        base = load_config(REFERENCE_CONFIG, env={})
        tweaked = load_config(REFERENCE_CONFIG, overrides={"strategy.target_delta": "0.30"}, env={})
        assert config_hash(base) != config_hash(tweaked)

    def test_hash_is_not_affected_by_key_order(self) -> None:
        base: AppConfig = load_config(REFERENCE_CONFIG, env={})
        rebuilt = AppConfig.model_validate(base.model_dump(mode="json"))
        assert config_hash(base) == config_hash(rebuilt)


class TestComboExitKindStaysInSync:
    """`ExitConfig`'s kind fields are a second Literal, not an import of
    `ComboExit.kind` (config would otherwise pull in core.signal's frozen-model
    machinery for one type alias). A second copy can silently drift, so it is
    checked here instead of trusted."""

    def test_config_and_signal_declare_the_same_exit_kinds(self) -> None:
        from typing import get_args

        from algo.config.schema import ComboExitKind
        from algo.core.signal import ComboExit

        config_kinds = set(get_args(ComboExitKind))
        signal_kinds = set(get_args(ComboExit.model_fields["kind"].annotation))
        assert config_kinds == signal_kinds
