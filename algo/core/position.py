"""Positions and the accounting for them.

Quantities are **signed**: negative is short. A short strangle is two negative
positions, and keeping the sign in the quantity rather than in a separate flag
means P&L arithmetic needs no branch on direction — which is one fewer place for
a sign error to hide.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from algo.core.enums import Side
from algo.core.fill import Charges, Fill
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Position(BaseModel):
    """Open exposure in one instrument, with weighted-average entry.

    Immutable: applying a fill returns a new `Position`. Mutation in place would
    make the event ledger and the position book capable of disagreeing, and the
    whole point of the ledger is that it cannot.
    """

    model_config = _FROZEN

    instrument: InstrumentId
    lots: int = 0
    qty: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realised_pnl: Decimal = Decimal("0")
    charges: Charges = Charges()
    opened_at: datetime | None = None
    multiplier: Decimal = Decimal("1")

    @field_validator("opened_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    def unrealised_pnl(self, mark: Decimal) -> Decimal:
        """Mark-to-market against `mark`, the instrument's own current price.

        Brief-critical: shorts are marked at the option's own recorded price, not
        at a model price. A model mark on an illiquid short option is how an
        equity curve stays smooth while the real position cannot be closed.
        """
        if self.qty == 0:
            return Decimal("0")
        return (mark - self.average_price) * self.qty * self.multiplier

    def apply(self, fill: Fill) -> Position:
        """Fold a fill into this position, returning the new state.

        Increasing a position re-weights the average price. Reducing or closing
        realises P&L against the existing average. Crossing through zero — a fill
        larger than the open quantity — realises the whole existing position and
        opens the remainder at the fill price.
        """
        signed = fill.qty * fill.side.sign
        new_qty = self.qty + signed
        lots_delta = fill.lots * fill.side.sign
        charges = self.charges + fill.charges

        if self.qty == 0:
            return self.model_copy(
                update={
                    "qty": new_qty,
                    "lots": lots_delta,
                    "average_price": fill.price,
                    "charges": charges,
                    "opened_at": self.opened_at or fill.ts,
                }
            )

        same_direction = (self.qty > 0) == (signed > 0)
        if same_direction:
            total = self.qty + signed
            weighted = (self.average_price * self.qty + fill.price * signed) / total
            return self.model_copy(
                update={
                    "qty": total,
                    "lots": self.lots + lots_delta,
                    "average_price": weighted,
                    "charges": charges,
                }
            )

        closing = min(abs(signed), abs(self.qty))
        direction = Decimal(1) if self.qty > 0 else Decimal(-1)
        realised = (fill.price - self.average_price) * closing * direction * self.multiplier

        if abs(signed) <= abs(self.qty):
            return self.model_copy(
                update={
                    "qty": new_qty,
                    "lots": self.lots + lots_delta,
                    "average_price": self.average_price if new_qty != 0 else Decimal("0"),
                    "realised_pnl": self.realised_pnl + realised,
                    "charges": charges,
                    "opened_at": self.opened_at if new_qty != 0 else None,
                }
            )

        # Crossed through flat: the remainder opens a new position the other way.
        return self.model_copy(
            update={
                "qty": new_qty,
                "lots": self.lots + lots_delta,
                "average_price": fill.price,
                "realised_pnl": self.realised_pnl + realised,
                "charges": charges,
                "opened_at": fill.ts,
            }
        )

    @property
    def side(self) -> Side | None:
        if self.qty == 0:
            return None
        return Side.BUY if self.qty > 0 else Side.SELL
