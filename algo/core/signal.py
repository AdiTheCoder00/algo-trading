"""Signals: intent, not orders. Brief §5.

A signal carries no lot size and no rupee amount. "A strategy that computes lot
size is a bug" — sizing belongs to the risk layer, which is the only layer that
knows about equity, margin and exposure.

Deviation from §5, decision D-008: `legs` is a tuple rather than a single
`direction`. A strangle is two legs that only make sense together — if the call
fills and the put rejects, what remains is a naked short call, a completely
different risk. That has to be expressible in the intent itself, or the execution
layer is left guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from algo.core.enums import Atomicity, SignalAction, Side
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class PriceIntent(BaseModel):
    model_config = _FROZEN

    kind: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Decimal | None = None

    @classmethod
    def market(cls) -> PriceIntent:
        return cls(kind="MARKET")

    @classmethod
    def limit(cls, price: Decimal) -> PriceIntent:
        return cls(kind="LIMIT", limit_price=price)


class TakeProfit(BaseModel):
    model_config = _FROZEN

    price: Decimal
    fraction: Decimal = Decimal("1")


class ComboExit(BaseModel):
    """An exit level for the position as a whole, not per leg.

    Decision D-025: a percentage is resolved to an absolute rupee level **once**,
    at entry, and frozen into the signal's context. A level that floated with live
    equity would make the same trade exit at a different price because of
    unrelated P&L elsewhere in the account.
    """

    model_config = _FROZEN

    kind: Literal[
        "PCT_OF_MARGIN_AT_ENTRY",
        "PCT_OF_EQUITY_AT_ENTRY",
        "PCT_OF_CREDIT",
        "MULTIPLE_OF_CREDIT",
        "ABS_INR",
        "DELTA_BREACH",
        "UNDERLYING_MOVE_PCT",
    ]
    value: Decimal


class SignalLeg(BaseModel):
    model_config = _FROZEN

    instrument: InstrumentId
    direction: Side
    entry: PriceIntent = Field(default_factory=PriceIntent.market)
    ratio: int = 1
    stop_price: Decimal | None = None
    take_profits: tuple[TakeProfit, ...] = ()

    @field_validator("ratio")
    @classmethod
    def _positive_ratio(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"leg ratio must be >= 1, got {v}")
        return v


class Signal(BaseModel):
    """One intent, produced on one closed bar.

    `reason` is mandatory and validated non-empty. It is written to the trade log,
    the ledger and the structlog record, because brief §5 asks a specific question:
    six weeks later, why did this fire? A blank reason makes that unanswerable, so
    it is not permitted to be blank.
    """

    model_config = _FROZEN

    signal_id: str
    strategy_id: str
    ts: datetime
    action: SignalAction
    legs: tuple[SignalLeg, ...]
    atomicity: Atomicity = Atomicity.ALL_OR_NONE
    combo_stop: ComboExit | None = None
    combo_take_profit: ComboExit | None = None
    time_exit: datetime | None = None
    confidence: Decimal = Decimal("1")
    reason: str = Field(min_length=1)
    context: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @field_validator("reason")
    @classmethod
    def _meaningful_reason(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("signal reason must not be blank — see brief §5")
        return v

    @field_validator("legs")
    @classmethod
    def _at_least_one_leg(cls, v: tuple[SignalLeg, ...]) -> tuple[SignalLeg, ...]:
        if not v:
            raise ValueError("signal must carry at least one leg")
        return v

    @property
    def leg_keys(self) -> tuple[str, ...]:
        return tuple(f"{leg.instrument.key}:{leg.direction}" for leg in self.legs)
