"""Configuration schema.

Every monetary and percentage value is a `Decimal` parsed from a **string** in
YAML (`"1000000.00"`, not `1000000.00`). YAML would otherwise parse the bare
number as a float and hand a float into money math at the very first step,
defeating brief §2.5 before the engine has even started.

The whole config is frozen after load and hashed. That hash goes into every run's
metadata and into every signal id, so a result can always be tied to the exact
settings that produced it — and a replay under edited settings cannot be mistaken
for the original run.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from algo.core.enums import Exchange, Mode

#: Mirrors `algo.core.signal.ComboExit.kind` exactly. Kept as a second Literal
#: rather than imported from there: importing core.signal into config would pull
#: pydantic's frozen-model machinery into the config-loading path for a single
#: type alias, and the two are checked against each other by
#: tests/test_config.py so they cannot silently drift apart.
ComboExitKind = Literal[
    "PCT_OF_MARGIN_AT_ENTRY",
    "PCT_OF_EQUITY_AT_ENTRY",
    "PCT_OF_CREDIT",
    "MULTIPLE_OF_CREDIT",
    "ABS_INR",
    "DELTA_BREACH",
    "UNDERLYING_MOVE_PCT",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class RunConfig(BaseModel):
    model_config = _FROZEN

    name: str = "unnamed"
    seed: int = 0


class BarConfig(BaseModel):
    model_config = _FROZEN

    timeframe_minutes: int = 30
    partial_last_bar: str = Field(default="keep_flagged", pattern="^(keep_flagged|drop)$")
    act_on_partial_bar: bool = Field(
        default=False,
        description="Fixed at False. D-014 forbids a strategy acting on the "
        "23:30-23:55 stub; the risk layer still may. Kept as a field because the "
        "distinction is worth stating, validated because it cannot be changed.",
    )

    @field_validator("act_on_partial_bar")
    @classmethod
    def _must_stay_false(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "act_on_partial_bar: true is not implemented - D-014 makes the "
                "partial-bar rule structural in DeltaStrangle, not configurable. "
                "Remove the setting rather than setting a value that does nothing."
            )
        return v


class SessionConfig(BaseModel):
    model_config = _FROZEN

    open_ist: time = time(9, 0)
    close_ist_us_dst: time = time(23, 30)
    close_ist_standard: time = time(23, 55)


class MarketConfig(BaseModel):
    model_config = _FROZEN

    timezone: str = "Asia/Kolkata"
    dst_reference_zone: str = "America/New_York"
    calendar: str = "mcx"
    holidays_file: Path | None = None
    allow_unverified_calendar: bool = False
    session: SessionConfig = SessionConfig()
    bar: BarConfig = BarConfig()

    @field_validator("timezone")
    @classmethod
    def _only_ist(cls, v: str) -> str:
        # `algo.core.timeutil` binds IST at import. A different value here would
        # be read by nobody, which is worse than not offering the knob.
        if v != "Asia/Kolkata":
            raise ValueError(
                f"timezone must be Asia/Kolkata, got {v!r} - the exchange clock is "
                "compiled into algo.core.timeutil, not read from config."
            )
        return v

    @field_validator("dst_reference_zone")
    @classmethod
    def _only_new_york(cls, v: str) -> str:
        if v != "America/New_York":
            raise ValueError(
                f"dst_reference_zone must be America/New_York, got {v!r} - the "
                "session close keys off US DST (D-017) and the zone is compiled "
                "into algo.core.timeutil."
            )
        return v


class InstrumentConfig(BaseModel):
    model_config = _FROZEN

    underlying: str
    exchange: Exchange = Exchange.MCX


class QualityConfig(BaseModel):
    model_config = _FROZEN

    reject_crossed_quotes: bool = True
    reject_empty_book: bool = True
    max_stale_seconds: float = Field(
        default=120.0,
        gt=0,
        description="How old the live chain snapshot may be before it stops "
        "pricing anything (algo/live/chain.py).",
    )

    @field_validator("reject_crossed_quotes", "reject_empty_book")
    @classmethod
    def _cannot_be_disabled(cls, v: bool, info: ValidationInfo) -> bool:
        # `Quote.status` checks both unconditionally. Accepting `false` here
        # would read as "filling against a crossed book is available if you want
        # it", and it is not.
        if not v:
            raise ValueError(
                f"{info.field_name}: false is not supported - Quote.status refuses "
                "these unconditionally and there is no way to opt out."
            )
        return v


class DataConfig(BaseModel):
    model_config = _FROZEN

    source: str = Field(default="synthetic", pattern="^(synthetic|csv|parquet|live)$")
    master_snapshot: Path = Field(
        default=Path("state/master_mcx.json"),
        description="Frozen Angel One instrument master (bar data), fetched by "
        "`algo live`.",
    )
    live_master_snapshot: Path = Field(
        default=Path("state/kotak_master.json"),
        description="Frozen Kotak Neo scrip master (live quotes and orders), "
        "fetched by `algo live`.",
    )
    quality: QualityConfig = QualityConfig()


class SizingConfig(BaseModel):
    model_config = _FROZEN

    mode: str = Field(default="fixed_lots", pattern="^fixed_lots$")
    fixed_lots: int = 1


class CapsConfig(BaseModel):
    model_config = _FROZEN

    max_concurrent_positions: int = 1
    max_lots_per_underlying: int = 5
    max_total_margin_pct: Decimal = Decimal("50")


class DevolvementConfig(BaseModel):
    """Decision D-016. These are hard rules; the fields tune *when*, not *whether*.

    There is deliberately no `enabled: false`. An in-the-money short leg left at
    option expiry becomes a futures position bound for physical delivery of gold,
    and a configuration file is not the right place to be able to switch that off.
    """

    model_config = _FROZEN

    force_exit_sessions_before_expiry: int = 1
    block_new_entries_within_dte: int = 2


class KillSwitchConfig(BaseModel):
    model_config = _FROZEN

    daily_loss_limit_pct: Decimal = Decimal("2")
    max_consecutive_losses: int = 3
    max_drawdown_pct: Decimal = Decimal("10")
    flatten_on_trip: bool = False


class RiskConfig(BaseModel):
    model_config = _FROZEN

    starting_equity: Decimal = Decimal("1000000.00")
    sizing: SizingConfig = SizingConfig()
    caps: CapsConfig = CapsConfig()
    devolvement: DevolvementConfig = DevolvementConfig()
    kill_switch: KillSwitchConfig = KillSwitchConfig()

    @field_validator("starting_equity")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"starting equity must be positive, got {v}")
        return v


class ExitConfig(BaseModel):
    model_config = _FROZEN

    take_profit_kind: ComboExitKind = "PCT_OF_MARGIN_AT_ENTRY"
    take_profit_value: Decimal = Decimal("2")
    stop_loss_kind: ComboExitKind = "PCT_OF_MARGIN_AT_ENTRY"
    stop_loss_value: Decimal = Decimal("1")
    no_stop_loss: bool = Field(
        default=False,
        description="Run with NO loss exit at all (D-102). Deliberately a "
        "separate flag rather than a null stop_loss_value: a safety level must "
        "not be removable by omitting a line, only by saying so explicitly.",
    )
    evaluate_on: str = Field(
        default="bar_close",
        pattern="^(bar_close|tick)$",
        description="Only bar_close is implemented. Q15 keeps `tick` as the "
        "intended live behaviour; the validator refuses it until it exists.",
    )
    min_stop_to_cost_ratio: Decimal = Decimal("3")
    on_stop_viability_breach: str = Field(default="warn", pattern="^(warn|refuse)$")


class StrategyConfig(BaseModel):
    model_config = _FROZEN

    id: str = "goldm_delta_strangle_v1"
    target_delta: Decimal = Decimal("0.25")
    delta_tolerance: Decimal = Decimal("0.05")
    entry_bars_ist: tuple[time, ...] = (time(9, 30),)
    cadence: str = Field(default="per_expiry_cycle", pattern="^(per_expiry_cycle|every_day)$")
    min_dte: int = 5
    max_dte: int = 45
    strike_multiple: Decimal | None = Field(
        default=None,
        description="Only consider strikes that are exact multiples of this "
        "(D-103). None considers every listed strike.",
    )
    roll_at_front_dte: int | None = Field(
        default=None,
        description="Only enter once the FRONT cycle is this many days from "
        "expiry; 0 means its expiry day itself. None enters on any qualifying "
        "session (D-104).",
    )
    cycle_offset: int = Field(
        default=0,
        ge=0,
        description="Which listed expiry to sell: 0 the front one, 1 the next "
        "after it. Pairs with roll_at_front_dte to roll into the next month on "
        "the current month's expiry day.",
    )
    exit: ExitConfig = ExitConfig()

    @field_validator("exit")
    @classmethod
    def _tick_exits_are_not_implemented(cls, v: ExitConfig) -> ExitConfig:
        # `check_exit` in execution/fills.py holds the intrabar logic and is not
        # wired to anything (Q15). Accepting `tick` would let a config claim a
        # protection the engine does not apply.
        if v.evaluate_on == "tick":
            raise ValueError(
                "exit.evaluate_on: tick is not implemented - the engine evaluates "
                "stops and targets at bar granularity only (Q15). Use bar_close, "
                "and read the backtest as optimistic on fast moves."
            )
        return v

    @field_validator("roll_at_front_dte")
    @classmethod
    def _not_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(
                f"roll_at_front_dte cannot be negative, got {v} - 0 already means "
                "the front cycle's own expiry day"
            )
        return v

    @field_validator("strike_multiple")
    @classmethod
    def _positive_multiple(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError(f"strike_multiple must be positive, got {v}")
        return v


class PersistenceConfig(BaseModel):
    model_config = _FROZEN

    live_db: Path = Path("state/live.db")
    live_broker_state: Path = Field(
        default=Path("state/kotak_broker.json"),
        description="The Kotak adapter's ledger — our client ids mapped to the "
        "broker's, so a restart can still answer order_by_client_id.",
    )


class LoggingConfig(BaseModel):
    model_config = _FROZEN

    level: str = "INFO"
    json_format: bool = True
    file: Path | None = None


class AppConfig(BaseModel):
    """The whole resolved configuration for one run."""

    model_config = _FROZEN

    mode: Mode
    run: RunConfig = RunConfig()
    market: MarketConfig = MarketConfig()
    instruments: tuple[InstrumentConfig, ...]
    data: DataConfig = DataConfig()
    risk: RiskConfig = RiskConfig()
    strategy: StrategyConfig = StrategyConfig()
    persistence: PersistenceConfig = PersistenceConfig()
    logging: LoggingConfig = LoggingConfig()

    @field_validator("instruments")
    @classmethod
    def _at_least_one(cls, v: tuple[InstrumentConfig, ...]) -> tuple[InstrumentConfig, ...]:
        if not v:
            raise ValueError("at least one instrument must be configured")
        return v
