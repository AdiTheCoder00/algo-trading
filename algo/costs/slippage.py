"""Slippage models.

Brief §6: "market orders slip against you by a configurable number of points,
stops slip more than limits." Both parts matter. A stop is a market order fired
into a move that is already going against you, so modelling it at the same
slippage as a resting limit understates the cost of exactly the trades that hurt.

Slippage here is *additional* to crossing the spread, which the fill simulator
applies separately. Keeping the two apart means a report can say which of the two
is eating the edge.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from algo.core.errors import DomainError


class SlippageModel(Protocol):
    """Extra price movement against us, on top of the spread."""

    def extra(self, *, tick: Decimal, is_stop: bool) -> Decimal: ...


class TickSlippage:
    """A fixed number of ticks, more for stops than for limits."""

    __slots__ = ("_market_ticks", "_stop_ticks")

    def __init__(self, *, market_ticks: int = 0, stop_ticks: int = 2) -> None:
        if market_ticks < 0 or stop_ticks < 0:
            raise DomainError("slippage cannot be negative")
        if stop_ticks < market_ticks:
            raise DomainError(
                f"stops must slip at least as much as market orders "
                f"({stop_ticks} < {market_ticks}) — see brief §6"
            )
        self._market_ticks = market_ticks
        self._stop_ticks = stop_ticks

    def extra(self, *, tick: Decimal, is_stop: bool) -> Decimal:
        ticks = self._stop_ticks if is_stop else self._market_ticks
        return tick * Decimal(ticks)

    def __repr__(self) -> str:
        return f"TickSlippage(market={self._market_ticks}, stop={self._stop_ticks})"


class NoSlippage:
    """Zero slippage. For the zero-cost falsification only."""

    def extra(self, *, tick: Decimal, is_stop: bool) -> Decimal:
        del tick, is_stop
        return Decimal("0")
