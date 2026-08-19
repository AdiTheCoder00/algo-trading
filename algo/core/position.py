"""Positions and the accounting for them.

Quantities are **signed**: negative is short. A short strangle is two negative
positions, and keeping the sign in the quantity rather than in a separate flag
means the P&L arithmetic needs no branch on direction — one fewer place for a
sign error to hide.

**Cost basis, not average price.** The position stores the exact total it paid
(or received), and derives an average only for display. Storing the average
instead requires dividing by the quantity, and `2000 / 3` has no exact decimal
representation — so a position built from three fills carries a rounding error
into every subsequent P&L figure. That error is around 1e-21, which sounds
harmless right up until the equity identity stops balancing and nobody can say
why.

This was found by `Portfolio.check_identity` on its first run against the
coin-flip strategy, which is exactly what that check is for.

On a partial close the basis is split proportionally and the remainder taken by
**subtraction**, never by a second division. The split between realised and
unrealised can therefore round by up to a paisa, but their sum cannot drift —
which is the property the equity curve actually depends on.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from algo.core.enums import Side
from algo.core.fill import Charges, Fill
from algo.core.instrument import InstrumentId
from algo.core.money import quantize_paisa
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Position(BaseModel):
    """Open exposure in one instrument.

    Immutable: applying a fill returns a new `Position`. Mutating in place would
    let the event ledger and the position book disagree, and the whole point of
    the ledger is that they cannot.
    """

    model_config = _FROZEN

    instrument: InstrumentId
    lots: int = 0
    qty: Decimal = Decimal("0")
    cost_basis: Decimal = Decimal("0")
    """Total paid (long) or received (short) in price units, unsigned. Exact."""
    realised_pnl: Decimal = Decimal("0")
    charges: Charges = Charges()
    opened_at: datetime | None = None
    multiplier: Decimal = Decimal("1")

    @field_validator("opened_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)

    # ------------------------------------------------------------------ state
    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def direction(self) -> int:
        if self.qty > 0:
            return 1
        return -1 if self.qty < 0 else 0

    @property
    def side(self) -> Side | None:
        if self.qty == 0:
            return None
        return Side.BUY if self.qty > 0 else Side.SELL

    @property
    def average_price(self) -> Decimal:
        """Derived for display and logging only — never used in P&L arithmetic.

        This is the one place a division happens, and its result never feeds back
        into cash, realised or unrealised.
        """
        if self.qty == 0:
            return Decimal("0")
        return self.cost_basis / abs(self.qty)

    # ------------------------------------------------------------------- P&L
    def unrealised_pnl(self, mark: Decimal) -> Decimal:
        """Mark-to-market against `mark`, the instrument's own current price.

        Computed from the exact cost basis rather than from an average, so no
        division enters the number.

        Shorts are marked at the option's own recorded price, not at a model
        price. A model mark on an illiquid short option is how an equity curve
        stays smooth while the position cannot actually be closed.
        """
        if self.qty == 0:
            return Decimal("0")
        magnitude = abs(self.qty)
        return (mark * magnitude - self.cost_basis) * Decimal(self.direction) * self.multiplier

    def apply(self, fill: Fill) -> Position:
        """Fold a fill into this position, returning the new state."""
        signed = fill.qty * fill.side.sign
        lots_delta = fill.lots * fill.side.sign
        charges = self.charges + fill.charges

        if self.qty == 0:
            return self.model_copy(
                update={
                    "qty": signed,
                    "lots": lots_delta,
                    "cost_basis": fill.price * fill.qty,
                    "charges": charges,
                    "opened_at": self.opened_at or fill.ts,
                }
            )

        same_direction = (self.qty > 0) == (signed > 0)
        if same_direction:
            return self.model_copy(
                update={
                    "qty": self.qty + signed,
                    "lots": self.lots + lots_delta,
                    "cost_basis": self.cost_basis + fill.price * fill.qty,
                    "charges": charges,
                }
            )

        magnitude = abs(self.qty)
        closing = min(abs(signed), magnitude)
        direction = Decimal(self.direction)

        # Proportional allocation, then the remainder by subtraction — so the two
        # parts always sum back to the original basis exactly.
        closed_basis = (
            self.cost_basis
            if closing == magnitude
            else quantize_paisa(self.cost_basis * closing / magnitude)
        )
        remaining_basis = self.cost_basis - closed_basis
        realised = (fill.price * closing - closed_basis) * direction * self.multiplier

        new_qty = self.qty + signed
        if abs(signed) <= magnitude:
            return self.model_copy(
                update={
                    "qty": new_qty,
                    "lots": self.lots + lots_delta,
                    "cost_basis": remaining_basis if new_qty != 0 else Decimal("0"),
                    "realised_pnl": self.realised_pnl + realised,
                    "charges": charges,
                    "opened_at": self.opened_at if new_qty != 0 else None,
                }
            )

        # Crossed through flat: the remainder opens a new position the other way.
        overshoot = abs(signed) - magnitude
        return self.model_copy(
            update={
                "qty": new_qty,
                "lots": self.lots + lots_delta,
                "cost_basis": fill.price * overshoot,
                "realised_pnl": self.realised_pnl + realised,
                "charges": charges,
                "opened_at": fill.ts,
            }
        )
