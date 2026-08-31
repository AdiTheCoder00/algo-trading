"""Shared helpers for CLI commands.

Extracted from the monolithic ``main.py`` so that every command module can
use them without importing from each other.  None of these are public API.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from algo.config.loader import load_config
from algo.exchange.calendar import synthetic_calendar

if TYPE_CHECKING:
    from algo.config.schema import AppConfig
    from algo.exchange.calendar import MarketCalendar
    from algo.strategy.delta_strangle import DeltaStrangle

#: One day inside US daylight saving and one outside, so both session-length
#: regimes are exercised every time ``verify`` runs.
US_DST_DAY = date(2026, 8, 19)
STANDARD_DAY = date(2026, 11, 10)


def calendar_for(config_path: Path | None) -> MarketCalendar:
    """The calendar a real-data command should use.

    ``synthetic_calendar`` says of itself "never for a real run", yet every command
    built one until D-115 — so ``market.allow_unverified_calendar`` decided nothing
    and MCX holidays went unmodelled.  This routes the real-data paths through the
    configured policy instead.  With no config file there is no policy to honour,
    so the unverified calendar is explicit rather than accidental.
    """
    from algo.exchange.calendar import mcx_calendar

    if config_path is None:
        return mcx_calendar(holidays_file=None, allow_unverified=True)
    cfg = load_config(config_path)
    return mcx_calendar(
        holidays_file=cfg.market.holidays_file,
        allow_unverified=cfg.market.allow_unverified_calendar,
    )


def strangle_from_config(cfg: AppConfig, symbol: str) -> DeltaStrangle:
    """Build the strategy from config.

    One function rather than the same argument list at each call site: the two
    real-data backtest commands were already identical here, and a strategy
    setting that reached only one of them would make the two commands quietly
    different strategies.
    """
    from algo.core.signal import ComboExit
    from algo.strategy.delta_strangle import DeltaStrangle

    exit_cfg = cfg.strategy.exit
    return DeltaStrangle(
        underlying=symbol,
        target_delta=cfg.strategy.target_delta,
        delta_tolerance=cfg.strategy.delta_tolerance,
        entry_times_ist=cfg.strategy.entry_bars_ist,
        min_dte=cfg.strategy.min_dte,
        max_dte=cfg.strategy.max_dte,
        take_profit=ComboExit(
            kind=exit_cfg.take_profit_kind, value=exit_cfg.take_profit_value
        ),
        stop_loss=ComboExit(kind=exit_cfg.stop_loss_kind, value=exit_cfg.stop_loss_value),
        no_stop=exit_cfg.no_stop_loss,
        strike_multiple=cfg.strategy.strike_multiple,
        roll_at_front_dte=cfg.strategy.roll_at_front_dte,
        cycle_offset=cfg.strategy.cycle_offset,
    )
