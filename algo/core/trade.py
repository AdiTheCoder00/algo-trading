"""Completed round trips, as they appear in the trade log.

This is the record the brief is really about: "I need to know why a trade fired
six weeks later." Every field that answers that question — the reason, the
context the strategy saw, the sizing trace — travels with the trade rather than
living in a log file that may have rotated away.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from algo.core.enums import Side
from algo.core.fill import Charges
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc, iso

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class TradeLeg(BaseModel):
    model_config = _FROZEN

    instrument: InstrumentId
    side: Side
    lots: int
    entry_price: Decimal
    exit_price: Decimal | None = None
    entry_ts: datetime
    exit_ts: datetime | None = None
    charges: Charges = Charges()

    @field_validator("entry_ts")
    @classmethod
    def _aware_entry(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @field_validator("exit_ts")
    @classmethod
    def _aware_exit(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)


class Trade(BaseModel):
    """One strategy position from open to close, across all its legs."""

    model_config = _FROZEN

    trade_id: str
    strategy_id: str
    signal_id: str
    legs: tuple[TradeLeg, ...]
    opened_at: datetime
    closed_at: datetime | None = None
    gross_pnl: Decimal = Decimal("0")
    charges: Charges = Charges()
    r_multiple: Decimal | None = None
    exit_reason: str = ""
    reason: str = Field(min_length=1)
    context: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("opened_at")
    @classmethod
    def _aware_open(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @field_validator("closed_at")
    @classmethod
    def _aware_close(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.charges.total

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_log_row(self) -> dict[str, str]:
        """Flat, sorted, string-valued row for the golden trade log.

        Everything is rendered to a string here so the golden file is byte-stable:
        no float formatting drift, no locale, no timezone rendering surprises.
        """
        return {
            "trade_id": self.trade_id,
            "strategy_id": self.strategy_id,
            "signal_id": self.signal_id,
            "opened_at": iso(self.opened_at),
            "closed_at": iso(self.closed_at) if self.closed_at else "",
            "legs": "|".join(f"{leg.instrument.key}:{leg.side}:{leg.lots}" for leg in self.legs),
            "gross_pnl": str(self.gross_pnl),
            "charges_total": str(self.charges.total),
            "net_pnl": str(self.net_pnl),
            "r_multiple": str(self.r_multiple) if self.r_multiple is not None else "",
            "exit_reason": self.exit_reason,
            "reason": self.reason,
        }
