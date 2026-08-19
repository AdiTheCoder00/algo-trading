"""Position book and cash accounting — shared by backtest, paper and live.

The accounting model is deliberately the simple one: a fill moves cash, and
equity is cash plus the mark-to-market value of what is held.

    equity = cash + sum(qty * mark * multiplier)

Everything else follows from that identity, including the one the property test
asserts on every single event:

    equity == starting_equity + realised + unrealised - charges

Both formulations are checked against each other rather than one being derived
from the other, because an accounting bug that keeps its own books consistent is
invisible. Two independent routes to the same number is the only way to notice.

There is no I/O here. The portfolio is pure state, so it can be driven from a
test with a handful of fills and no fixtures at all.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.core.fill import Charges, Fill
from algo.core.instrument import InstrumentId
from algo.core.position import Position
from algo.core.timeutil import ensure_utc


class EquityPoint(BaseModel):
    """One observation of the account, for the equity curve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    charges: Decimal
    open_positions: int


class Portfolio:
    """Cash, positions and the equity curve."""

    __slots__ = ("_cash", "_charges", "_curve", "_positions", "_realised", "_starting_equity")

    def __init__(self, starting_equity: Decimal) -> None:
        if starting_equity <= 0:
            raise DomainError(f"starting equity must be positive, got {starting_equity}")
        self._starting_equity = starting_equity
        self._cash = starting_equity
        self._positions: dict[str, Position] = {}
        self._charges = Charges()
        self._realised = Decimal("0")
        self._curve: list[EquityPoint] = []

    # ------------------------------------------------------------------ state
    @property
    def starting_equity(self) -> Decimal:
        return self._starting_equity

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def realised_pnl(self) -> Decimal:
        return self._realised

    @property
    def charges(self) -> Charges:
        return self._charges

    @property
    def curve(self) -> tuple[EquityPoint, ...]:
        return tuple(self._curve)

    def position(self, instrument: InstrumentId) -> Position | None:
        return self._positions.get(instrument.key)

    def open_positions(self) -> tuple[Position, ...]:
        """Sorted by instrument key, so iteration order is reproducible."""
        return tuple(
            self._positions[key]
            for key in sorted(self._positions)
            if not self._positions[key].is_flat
        )

    @property
    def is_flat(self) -> bool:
        return not self.open_positions()

    def positions_by_key(self) -> dict[str, Position]:
        return dict(self._positions)

    # ----------------------------------------------------------------- fills
    def apply_fill(self, fill: Fill, *, multiplier: Decimal) -> None:
        """Fold a fill into cash and into the position book.

        Cash moves by the notional of the fill plus the charges, which are always
        a debit regardless of side. Realised P&L is taken from the position's own
        arithmetic rather than recomputed here, so there is exactly one place that
        knows how a round trip is closed out.
        """
        key = fill.instrument.key
        existing = self._positions.get(key) or Position(
            instrument=fill.instrument, multiplier=multiplier
        )
        if existing.multiplier != multiplier:
            raise DomainError(
                f"multiplier changed for {key}: {existing.multiplier} -> {multiplier}"
            )

        notional = fill.price * fill.qty * multiplier
        if fill.side is Side.BUY:
            self._cash -= notional
        else:
            self._cash += notional
        self._cash -= fill.charges.total

        updated = existing.apply(fill)
        self._realised += updated.realised_pnl - existing.realised_pnl
        self._charges = self._charges + fill.charges
        self._positions[key] = updated

    # ------------------------------------------------------------------ mark
    def market_value(self, marks: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for key, position in self._positions.items():
            if position.is_flat:
                continue
            mark = marks.get(key)
            if mark is None:
                raise DomainError(f"no mark supplied for open position {key}")
            total += position.qty * mark * position.multiplier
        return total

    def unrealised_pnl(self, marks: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for key, position in self._positions.items():
            if position.is_flat:
                continue
            mark = marks.get(key)
            if mark is None:
                raise DomainError(f"no mark supplied for open position {key}")
            total += position.unrealised_pnl(mark)
        return total

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self._cash + self.market_value(marks)

    def record(self, ts: datetime, marks: dict[str, Decimal]) -> EquityPoint:
        """Append one point to the equity curve and return it."""
        market_value = self.market_value(marks)
        unrealised = self.unrealised_pnl(marks)
        point = EquityPoint(
            ts=ensure_utc(ts),
            cash=self._cash,
            market_value=market_value,
            equity=self._cash + market_value,
            realised_pnl=self._realised,
            unrealised_pnl=unrealised,
            charges=self._charges.total,
            open_positions=len(self.open_positions()),
        )
        self._curve.append(point)
        return point

    # ------------------------------------------------------------- invariants
    def check_identity(self, marks: dict[str, Decimal]) -> None:
        """Assert equity reconciles both ways. Raises if it does not.

        Called after every event in the engine. It is cheap, and an accounting
        drift caught on the bar it happened is a five-minute fix, while the same
        drift found in a tearsheet three weeks later is an archaeology project.
        """
        by_cash = self.equity(marks)
        by_pnl = (
            self._starting_equity
            + self._realised
            + self.unrealised_pnl(marks)
            - self._charges.total
        )
        if by_cash != by_pnl:
            raise DomainError(
                "portfolio identity violated: "
                f"cash+value = {by_cash}, start+realised+unrealised-charges = {by_pnl}, "
                f"difference {by_cash - by_pnl}"
            )
