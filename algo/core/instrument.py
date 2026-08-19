"""Instrument identity and contract specifications.

Decision D-009: no broker token appears here. Angel One's `symboltoken` and
`tradingsymbol` live only in the adapter's instrument map. `core/` never learns
what a symboltoken is, which is what makes a second broker an adapter rather than
a refactor — and means a reissued token cannot corrupt a historical record.

MCX options are options **on futures** (D-018), so an `OptionId` names the futures
contract it settles into. That relationship is not decoration: it is what the
devolvement guard reads.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from algo.core.enums import Exchange, Right

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class FutureId(BaseModel):
    """A futures contract, identified by underlying and its own expiry."""

    model_config = _FROZEN

    kind: Literal["future"] = "future"
    underlying: str
    expiry: date
    exchange: Exchange = Exchange.MCX

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.underlying}:FUT:{self.expiry:%Y%m%d}"

    def __str__(self) -> str:
        return self.key


class OptionId(BaseModel):
    """An option contract on a futures contract.

    `option_expiry` is deliberately not derived from `underlying_future.expiry`.
    They are different dates — the option expires first — and conflating them is
    precisely the error that walks a short leg into devolvement (C-004, D-023).
    """

    model_config = _FROZEN

    kind: Literal["option"] = "option"
    underlying_future: FutureId
    option_expiry: date
    strike: Decimal
    right: Right
    exchange: Exchange = Exchange.MCX

    @property
    def underlying(self) -> str:
        return self.underlying_future.underlying

    @property
    def key(self) -> str:
        return (
            f"{self.exchange}:{self.underlying}:{self.option_expiry:%Y%m%d}"
            f":{self.strike}:{self.right}"
        )

    def __str__(self) -> str:
        return self.key


InstrumentId: TypeAlias = Annotated[FutureId | OptionId, Field(discriminator="kind")]


class InstrumentSpec(BaseModel):
    """Exchange contract specification, valid over a date range.

    Decision D-010: `source` is mandatory. Lot sizes, tick sizes, strike intervals
    and per-order quantity caps are all revised by the exchange over time, and a
    constant without a date and a citation is an unfalsifiable claim. A backtest
    spanning a revision must use the values in force on each date, which is why
    this is a range and not a singleton.
    """

    model_config = _FROZEN

    underlying: str
    exchange: Exchange

    lot_size: Decimal = Field(description="Contract size in the commodity's own units, e.g. 100 g")
    multiplier: Decimal = Field(
        description="Rupees per one point of quoted price, per lot. "
        "GOLDM quotes per 10 g and trades 100 g, so multiplier = 10."
    )
    tick_size: Decimal
    strike_interval: Decimal | None = None
    max_order_qty_lots: int | None = Field(
        default=None, description="Exchange cap on a single order; larger sizes must be sliced."
    )
    min_lots: int = 1
    max_lots: int | None = None
    dpr_pct: Decimal | None = Field(
        default=None, description="Daily price range / circuit, as a percentage."
    )

    effective_from: date
    effective_to: date | None = None
    source: str = Field(min_length=1, description="Circular or document reference. Mandatory.")

    def covers(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on <= self.effective_to
