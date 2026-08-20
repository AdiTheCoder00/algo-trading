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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from algo.core.enums import Exchange, Mode

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class RunConfig(BaseModel):
    model_config = _FROZEN

    name: str = "unnamed"
    seed: int = 0
    out_dir: Path = Path("runs")


class BarConfig(BaseModel):
    model_config = _FROZEN

    timeframe_minutes: int = 30
    partial_last_bar: str = Field(default="keep_flagged", pattern="^(keep_flagged|drop)$")
    act_on_partial_bar: bool = False


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


class InstrumentConfig(BaseModel):
    model_config = _FROZEN

    underlying: str
    exchange: Exchange = Exchange.MCX


class QualityConfig(BaseModel):
    model_config = _FROZEN

    reject_crossed_quotes: bool = True
    reject_empty_book: bool = True
    max_stale_seconds: float = 10.0
    on_violation: str = Field(default="skip_bar", pattern="^(skip_bar|fail_run)$")


class DataConfig(BaseModel):
    model_config = _FROZEN

    source: str = Field(default="synthetic", pattern="^(synthetic|csv|parquet|live)$")
    bars_path: Path | None = None
    chain_path: Path | None = None
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

    mode: str = Field(default="fixed_lots", pattern="^(fixed_lots|margin_pct|risk_pct_with_stop)$")
    fixed_lots: int = 1
    margin_pct: Decimal = Decimal("40")
    risk_pct: Decimal = Decimal("1")


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

    take_profit_kind: str = "PCT_OF_MARGIN_AT_ENTRY"
    take_profit_value: Decimal = Decimal("2")
    stop_loss_kind: str = "PCT_OF_MARGIN_AT_ENTRY"
    stop_loss_value: Decimal = Decimal("1")
    evaluate_on: str = Field(default="bar_close", pattern="^(bar_close|tick)$")
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
    exit: ExitConfig = ExitConfig()


class PersistenceConfig(BaseModel):
    model_config = _FROZEN

    live_db: Path = Path("state/live.db")
    research_db: Path = Path("state/research.duckdb")
    wal: bool = True
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
