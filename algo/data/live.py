"""Live-data plumbing shared by every feed, regardless of broker.

`SessionWindow` and `TickBarBuilder` know nothing about SmartAPI or Kotak Neo:
they exist to answer "when is the session live" and "when has a bar closed",
which are the two questions every live path must answer the same way. They
live here so `smartapi_feed` (bars) and `kotak_feed` (chain) can share them
without importing each other's broker specifics.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from algo.core.bar import Bar, Timeframe
from algo.core.timeutil import ensure_utc
from algo.exchange.calendar import MarketCalendar


class TickBarBuilder:
    """Aggregates ticks into bars, emitting each only once its interval has closed.

    Pure and deterministic: feed it (timestamp, price, volume-delta) triples and
    it yields `Bar`s at interval boundaries. Bars are labelled by their **close**
    instant (bar.py:56) — the boundary that ended them — and `close_partial()`
    emits the in-flight bar at session end, flagged `is_partial=True`.
    """

    __slots__ = ("_bars", "_close", "_frame", "_high", "_low", "_open", "_ts", "_volume")

    def __init__(self, timeframe: Timeframe) -> None:
        self._frame = timeframe
        self._ts: datetime | None = None
        self._open = Decimal("0")
        self._close = Decimal("0")
        self._high = Decimal("0")
        self._low = Decimal("0")
        self._volume = 0
        self._bars: list[Bar] = []

    def feed(self, ts: datetime, price: Decimal, volume: int = 0) -> None:
        ts = ensure_utc(ts)
        if self._ts is None:
            self._ts = ts
            self._open = self._close = self._high = self._low = price
            self._volume = volume
            return
        boundary = self._ts + timedelta(minutes=self._frame.minutes)
        if ts >= boundary:
            self._emit(boundary, partial=False)
            self._ts = ts
            self._open = self._close = self._high = self._low = price
            self._volume = volume
            return
        self._close = price
        self._high = max(self._high, price)
        self._low = min(self._low, price)
        self._volume += volume

    def close_partial(self) -> None:
        if self._ts is None:
            return
        if self._bars and self._bars[-1].ts >= self._ts:
            return
        self._emit(self._ts, partial=True)

    def _emit(self, close_ts: datetime, *, partial: bool) -> None:
        if self._ts is None:
            return
        self._bars.append(
            Bar(
                ts=close_ts,
                timeframe=self._frame,
                open=self._open,
                high=self._high,
                low=self._low,
                close=self._close,
                volume=self._volume,
                is_partial=partial,
            )
        )

    def drained(self) -> list[Bar]:
        bars, self._bars = self._bars, []
        return bars


class SessionWindow:
    """When a trading session starts and ends on a given IST day.

    Delegates to the exchange calendar, which owns the DST-dependent close time
    (D-014: MCX closes 23:30 IST while US daylight saving is in force, 23:55
    otherwise). Hardcoding that here would be the exact bug the calendar exists
    to prevent.
    """

    __slots__ = ("_calendar",)

    def __init__(self, calendar: MarketCalendar) -> None:
        self._calendar = calendar

    def open_at(self, session_day: date) -> datetime:
        return self._calendar.session_open(session_day)

    def close_at(self, session_day: date) -> datetime:
        return self._calendar.session_close(session_day)

    def day_for(self, instant: datetime) -> date:
        from algo.core.timeutil import ist_date

        return ist_date(instant)

    def is_live(self, instant: datetime) -> bool:
        session_day = self.day_for(instant)
        if not self._calendar.is_trading_day(session_day):
            return False
        return self.open_at(session_day) <= ensure_utc(instant) < self.close_at(session_day)
