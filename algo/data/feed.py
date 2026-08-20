"""Feed interfaces.

Brief §2.8: no network calls in unit tests. Every feed is an interface with a fake
implementation, and the backtest, paper and live paths differ only in which
implementation is plugged in.

Feeds yield **closed** bars, in strictly increasing time order. That guarantee is
what the rest of the engine builds on, so it is checked at the boundary rather
than assumed downstream.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Protocol, runtime_checkable

from algo.core.bar import Bar, Timeframe
from algo.core.chain import OptionChainSnapshot
from algo.core.instrument import InstrumentId


@runtime_checkable
class BarFeed(Protocol):
    """A source of closed bars for one instrument at one timeframe."""

    @property
    def instrument(self) -> InstrumentId: ...

    @property
    def timeframe(self) -> Timeframe: ...

    def __iter__(self) -> Iterator[Bar]: ...


@runtime_checkable
class ChainFeed(Protocol):
    """A source of option chain snapshots, aligned to bar closes."""

    @property
    def underlying(self) -> str: ...

    def snapshots(self, option_expiry: date) -> Iterator[OptionChainSnapshot]: ...


class InMemoryBarFeed:
    """A feed backed by a list. The fake used throughout the tests.

    Holds a tuple copy so a test cannot mutate the bars after handing them over —
    which would otherwise make a determinism failure look like a feed bug.
    """

    __slots__ = ("_bars", "_instrument", "_timeframe")

    def __init__(self, instrument: InstrumentId, timeframe: Timeframe, bars: list[Bar]) -> None:
        self._instrument = instrument
        self._timeframe = timeframe
        self._bars = tuple(bars)

    @property
    def instrument(self) -> InstrumentId:
        return self._instrument

    @property
    def timeframe(self) -> Timeframe:
        return self._timeframe

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)

    def __len__(self) -> int:
        return len(self._bars)
