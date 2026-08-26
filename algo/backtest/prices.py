"""Where the engine gets a price from.

Brief §4: backtest, paper and live must share one engine, differing only in I/O.
The single-instrument futures case and the multi-leg options case need prices from
genuinely different places — a bar series and an option chain — so the difference
is isolated here rather than forked into a second engine loop.

Two questions, deliberately separate:

* **`mark`** — what is this position worth right now? Used for the equity curve.
* **`fill_reference`** — what price would an order transact at? Used by the fill
  simulator, which then moves it against us for spread and slippage.

They differ. A bar marks at its close but fills at its open, and conflating the
two is how a backtest quietly fills at a price it also used to make the decision.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from algo.core.bar import Bar
from algo.core.chain import OptionChainSnapshot
from algo.core.errors import DataError
from algo.core.instrument import InstrumentId


class PriceSource(Protocol):
    """Supplies marks and fill references for whatever the engine is holding."""

    def mark(self, key: str, ts: datetime) -> Decimal | None: ...

    def fill_reference(self, key: str, ts: datetime) -> Decimal | None: ...


class BarPriceSource:
    """Prices for one instrument, taken from its own bar series.

    Marks at the close, fills at the open — the convention from D-038, which is
    what stops a signal produced on bar `i` from transacting inside bar `i`.
    """

    __slots__ = ("_by_ts", "_key")

    def __init__(self, instrument: InstrumentId, bars: list[Bar]) -> None:
        self._key = instrument.key
        self._by_ts = {bar.ts: bar for bar in bars}

    def add(self, bar: Bar) -> None:
        """Take a bar that arrived after construction.

        A backtest knows every bar up front; a live loop does not. Without this
        the index built here would be a snapshot taken before the session
        started, and the first mark on a newly closed bar would raise - which is
        exactly how this was found (`BacktestEngine.append_bar`).
        """
        self._by_ts[bar.ts] = bar

    def mark(self, key: str, ts: datetime) -> Decimal | None:
        if key != self._key:
            return None
        bar = self._by_ts.get(ts)
        return bar.close if bar else None

    def fill_reference(self, key: str, ts: datetime) -> Decimal | None:
        if key != self._key:
            return None
        bar = self._by_ts.get(ts)
        return bar.open if bar else None


class ChainPriceSource:
    """Prices for option legs, taken from chain snapshots.

    A chain snapshot is taken at a bar close, so mark and fill reference are the
    same instant. The D-038 rule still holds through the engine: an order queued
    on bar `i` is filled against bar `i+1`'s snapshot, never bar `i`'s.

    The fill reference is the **mid**, and the fill simulator then walks it to the
    side we are trading. Using the last trade instead would let a stale print set
    the fill price on an illiquid strike, which is precisely the strike this
    strategy sells.
    """

    __slots__ = ("_prices", "_snapshots")

    def __init__(self, snapshots: list[OptionChainSnapshot]) -> None:
        self._snapshots = {snapshot.ts: snapshot for snapshot in snapshots}
        self._prices: dict[tuple[datetime, str], Decimal] = {}
        for snapshot in snapshots:
            for row in snapshot.rows:
                reference = row.quote.mid if row.quote.mid is not None else row.quote.ltp
                if reference is not None and reference > 0:
                    self._prices[(snapshot.ts, row.option.key)] = reference

    def snapshot_at(self, ts: datetime) -> OptionChainSnapshot | None:
        return self._snapshots.get(ts)

    def mark(self, key: str, ts: datetime) -> Decimal | None:
        return self._prices.get((ts, key))

    def fill_reference(self, key: str, ts: datetime) -> Decimal | None:
        return self._prices.get((ts, key))


class CompositePriceSource:
    """Tries each source in turn. Lets a run hold futures and options together."""

    __slots__ = ("_sources",)

    def __init__(self, *sources: PriceSource) -> None:
        self._sources = sources

    def mark(self, key: str, ts: datetime) -> Decimal | None:
        for source in self._sources:
            found = source.mark(key, ts)
            if found is not None:
                return found
        return None

    def fill_reference(self, key: str, ts: datetime) -> Decimal | None:
        for source in self._sources:
            found = source.fill_reference(key, ts)
            if found is not None:
                return found
        return None


class ChainFeedProvider:
    """Adapts recorded chain snapshots to the `ChainProvider` the context expects."""

    __slots__ = ("_by_key",)

    def __init__(self, snapshots: list[OptionChainSnapshot]) -> None:
        self._by_key: dict[tuple[str, date, datetime], OptionChainSnapshot] = {
            (s.underlying, s.option_expiry, s.ts): s for s in snapshots
        }

    def chain_at(
        self, underlying: str, option_expiry: date, ts: datetime
    ) -> OptionChainSnapshot | None:
        return self._by_key.get((underlying, option_expiry, ts))


def require_mark(source: PriceSource, key: str, ts: datetime) -> Decimal:
    """Fetch a mark or fail loudly.

    A missing mark for an open position is a data gap, not a zero. Marking it at
    zero would show a short option as fully profitable on exactly the bar the feed
    dropped out — which is the most dangerous possible direction for that error.
    """
    found = source.mark(key, ts)
    if found is None:
        raise DataError(f"no mark available for open position {key} at {ts}")
    return found
