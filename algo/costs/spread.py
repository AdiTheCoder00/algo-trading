"""Spread models.

Brief §6 wants spread modelled from replayed bid/ask where available. It is not
available yet — the recorder starts at Milestone 1.5 — so until then the engine
uses a modelled spread and **records that it did**. A report that cannot tell a
measured spread from an assumed one is overstating what it knows.

On a thin GOLDM option book the spread is expected to dominate every other cost,
which is why this is a first-class model with its own tests rather than a
constant buried in the fill simulator.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from algo.core.errors import DomainError
from algo.core.money import quantize_to_tick


class SpreadModel(Protocol):
    """Half the bid-ask spread, in price units.

    Half, because a fill crosses one side of the book. A round trip crosses twice
    and therefore pays the full spread — which is the arithmetic the coin-flip
    falsification depends on.
    """

    def half_spread(self, price: Decimal, tick: Decimal) -> Decimal: ...

    @property
    def is_measured(self) -> bool:
        """False for a modelled spread, true only when taken from a real book."""
        ...


class FixedTickSpread:
    """A constant spread of `ticks` ticks, regardless of price or moneyness.

    Crude on purpose. It is the model whose cost is exactly predictable by hand,
    which is what makes the Milestone 3 falsification a real test rather than a
    comparison of one implementation against itself.
    """

    __slots__ = ("_ticks",)

    def __init__(self, ticks: int) -> None:
        if ticks < 0:
            raise DomainError(f"spread cannot be negative, got {ticks} ticks")
        self._ticks = ticks

    def half_spread(self, price: Decimal, tick: Decimal) -> Decimal:
        del price
        return tick * Decimal(self._ticks) / Decimal("2")

    @property
    def is_measured(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"FixedTickSpread({self._ticks} ticks)"


class PctOfPriceSpread:
    """Spread as a percentage of price, floored at one tick.

    Closer to how an option book actually behaves — a ₹3,000 premium and a ₹200
    premium do not carry the same absolute spread — but still an assumption until
    the recorder measures the real thing.
    """

    __slots__ = ("_pct", "_min_ticks")

    def __init__(self, pct: Decimal, *, min_ticks: int = 1) -> None:
        if pct < 0:
            raise DomainError(f"spread percentage cannot be negative, got {pct}")
        self._pct = pct
        self._min_ticks = min_ticks

    def half_spread(self, price: Decimal, tick: Decimal) -> Decimal:
        modelled = price * self._pct / Decimal("200")  # half of pct% of price
        floor = tick * Decimal(self._min_ticks) / Decimal("2")
        chosen = max(modelled, floor)
        return quantize_to_tick(chosen, tick / Decimal("2"), side="SELL")

    @property
    def is_measured(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"PctOfPriceSpread({self._pct}%, min {self._min_ticks} ticks)"


class NoSpread:
    """Zero spread. For the zero-cost falsification only."""

    def half_spread(self, price: Decimal, tick: Decimal) -> Decimal:
        del price, tick
        return Decimal("0")

    @property
    def is_measured(self) -> bool:
        return False
