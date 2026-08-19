"""Fills, and the itemised charges attached to them.

Brief §10 asks for cost drag as a percentage of gross P&L, itemised. That is only
possible if charges are carried per fill from the start — reconstructing them
later from a total is guesswork, and guesswork about costs is how a losing
strategy looks profitable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from algo.core.enums import Side
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Charges(BaseModel):
    """The MCX charge stack, itemised. Every component separately, never blended.

    Decision D-011: these values are not authoritative until calibrated against a
    real Angel One contract note and locked by a test that reproduces it to the
    paisa. Until then the model runs and reports, but the totals carry a caveat.
    """

    model_config = _FROZEN

    brokerage: Decimal = Decimal("0")
    ctt: Decimal = Decimal("0")
    exchange_txn: Decimal = Decimal("0")
    sebi_fee: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")
    gst: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.ctt
            + self.exchange_txn
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
        )

    def __add__(self, other: Charges) -> Charges:
        return Charges(
            brokerage=self.brokerage + other.brokerage,
            ctt=self.ctt + other.ctt,
            exchange_txn=self.exchange_txn + other.exchange_txn,
            sebi_fee=self.sebi_fee + other.sebi_fee,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            gst=self.gst + other.gst,
        )


class Fill(BaseModel):
    """One execution.

    `slippage` records the difference between the price we intended and the price
    we got, signed against us. Carrying it per fill is what lets the backtest
    report predicted-versus-realised cost drag, which is the check that falsifies
    the engine in Milestone 3.
    """

    model_config = _FROZEN

    fill_id: str
    client_order_id: str
    signal_id: str
    instrument: InstrumentId
    side: Side
    lots: int
    qty: Decimal
    price: Decimal
    ts: datetime
    charges: Charges = Charges()
    slippage: Decimal = Decimal("0")
    is_modelled: bool = False

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @field_validator("price")
    @classmethod
    def _positive_price(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"fill price must be positive, got {v}")
        return v
