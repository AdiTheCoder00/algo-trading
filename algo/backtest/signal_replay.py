"""Replay bars through a strategy and collect what it *said*, not what it earned.

`cfd_runner.run_cfd_backtest` answers "what did this strategy make after the
venue's costs": it fills, charges spread and financing, prices a stop at the
level it triggered on, and returns round trips. That is the right answer for a
study and the wrong one for a monitor, which needs to know only whether the rule
fired on the newest closed bar - and must not invent a fill to find out.

This is that second question, and it exists as shared code for one reason: a
Telegram alert that disagreed with the backtest would be worse than no alert,
because it would put someone in a trade no measurement covers. Both callers walk
the same bars into the same `BarContext` the strategy cannot distinguish, so the
only way they can disagree is if the strategy is non-deterministic, which
`tests/test_determinism.py` already forbids.

## The position is bookkeeping, not a broker

A strategy's exit rule needs a position to exit, and `ProtectiveExits` needs an
entry price to measure a stop against. `PaperBook` supplies exactly that much:
side and the signal bar's close. It has no cash, charges nothing, and reports no
P&L - anything that wants a P&L number must use `cfd_runner`, which is honest
about the costs. One lot, always, because the strategy reads only the *sign* of
the quantity and a notional here would be a number nobody chose.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.enums import Exchange, Side, SignalAction
from algo.core.instrument import InstrumentId
from algo.core.position import Position
from algo.core.signal import Signal
from algo.exchange.forex_calendar import ForexCalendar
from algo.exchange.specs import ContractSpecStore
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext, PositionView, SessionInfo


@dataclass(frozen=True, slots=True)
class Fired:
    """One signal and the bar that produced it."""

    bar: Bar
    signal: Signal

    @property
    def is_entry(self) -> bool:
        return self.signal.action is SignalAction.OPEN

    @property
    def side(self) -> Side:
        """The side of the signal's first leg - the direction it asks for.

        On a CLOSE that is the side that *flattens* the position, so a closing
        BUY is the exit of a short. `ProtectiveExits.ExitDecision` states the
        same convention, and a caller writing a message has to say which it
        means rather than printing this word.
        """
        return self.signal.legs[0].direction


class PaperBook:
    """The position a replayed strategy is shown. See the module docstring."""

    __slots__ = ("_entry", "_instrument", "_side")

    LOTS = 1

    def __init__(self, instrument: InstrumentId) -> None:
        self._instrument = instrument
        self._side: Side | None = None
        self._entry = Decimal("0")

    def open(self, side: Side, price: Decimal) -> None:
        self._side, self._entry = side, price

    def close(self) -> None:
        self._side = None

    @property
    def side(self) -> Side | None:
        return self._side

    def position(self) -> Position | None:
        if self._side is None:
            return None
        signed = Decimal(self.LOTS if self._side is Side.BUY else -self.LOTS)
        return Position(
            instrument=self._instrument,
            lots=int(signed),
            qty=signed,
            cost_basis=Decimal(self.LOTS) * self._entry,
        )


def forex_session_of(ts: datetime) -> date:
    """Which 24-hour trading day an instant belongs to, for a rolling market.

    `cfd_runner.session_date_for`'s reasoning, and its fallback: the calendar
    raises past its verified range, and refusing to place a bar in a session
    over a calendar edge would stop a monitor dead. Falling back to the bar's
    own date only shifts which midnight a session-scoped indicator rolls at.
    """
    calendar = ForexCalendar()
    for candidate in (ts.date(), ts.date() + timedelta(days=1)):
        try:
            if calendar.session_open(candidate) <= ts < calendar.session_close(candidate):
                return candidate
        except Exception:  # noqa: BLE001 - a candidate may simply not be a session
            continue
    return ts.date()


def replay_signals(
    bars: Sequence[Bar],
    *,
    strategy_factory: Callable[[], Strategy],
    instrument: InstrumentId,
    timeframe: Timeframe,
    session_of: Callable[[datetime], date],
    exchange: Exchange = Exchange.OTC,
) -> list[Fired]:
    """Feed `bars` through one fresh strategy and collect every signal.

    `strategy_factory` builds the strategy rather than taking one, for
    `run_cfd_backtest`'s reason: an incremental strategy carries indicator state,
    and one reused across two series would answer the second with the first's
    memory. A fresh instance per replay is also what makes a monitor able to
    re-derive its whole view from the bars on every poll.

    The window is bounded to the strategy's own warmup, so a caller passing
    years of history pays O(warmup) per bar rather than O(n) - the same bound,
    and the same reason, as `cfd_runner`'s sliding window.
    """
    strategy = strategy_factory()
    book = PaperBook(instrument)
    window_size = max(strategy.warmup_bars() + 5, 10)
    specs = ContractSpecStore.default()
    bars_in_session = max(int(23 * 60 / timeframe.minutes), 1)
    fired: list[Fired] = []

    for index, bar in enumerate(bars):
        held = book.position()
        ctx = BarContext(
            window=BarWindow.of(tuple(bars[max(0, index + 1 - window_size) : index + 1])),
            session=SessionInfo(
                session_date=session_of(bar.ts),
                # Neither field is read by a strategy that trades a rolling
                # market, and inventing a value would be a number this module
                # made up. `cfd_runner` passes the same placeholders.
                is_us_dst=False,
                minutes_to_close=0,
                is_partial_bar=False,
                bar_index=index,
                bars_in_session=bars_in_session,
            ),
            specs=specs,
            positions=PositionView({} if held is None else {instrument.key: held}),
            timeframe=timeframe,
            exchange=exchange,
        )
        for signal in strategy.on_bar(ctx):
            fired.append(Fired(bar=bar, signal=signal))
            if signal.action is SignalAction.OPEN:
                book.open(signal.legs[0].direction, bar.close)
            else:
                book.close()
        # Drained and dropped: the notes explain why a bar produced nothing,
        # which is a debugging aid for a study and noise for a monitor. Leaving
        # them to accumulate would grow the strategy's list for the whole replay.
        strategy.drain_notes()
    return fired
