"""MetaTrader 5 bars, for XAUUSD on a Vantage account.

MT5 is a different world from the rest of this project and the differences are
not cosmetic. Three of them decide whether the bars are usable at all.

## Its clock is not UTC

`copy_rates_*` and `symbol_info_tick` return **broker server time**, and MT5's
API never says what that is. Vantage runs UTC+3 in summer; most MT5 brokers
follow EET, which means the offset **changes twice a year**. Hard-coding it would
be silently wrong for a third of the year - and every bar misaligned by an hour
is a strategy reading the wrong session.

So the offset is *measured* against a real clock at session start
(`measure_server_offset`) rather than assumed, and is re-measurable whenever the
caller wants. Everything this module returns is tz-aware UTC, like the rest of
the codebase.

## The newest bar has not closed

`copy_rates_from_pos(symbol, tf, 0, n)` includes the bar that is **still
forming**. Handing that to a strategy is look-ahead by the back door: its high,
low and close can all still change, so a decision taken on it is a decision taken
on information that did not exist. `closed_bars` drops it. This is the one form
of look-ahead the backtest's own firewall cannot catch, because in live there is
no future array to withhold.

## Prices arrive as floats

MT5 hands back numpy float64. Every price crosses into `Decimal` through `str`
(brief S2.5) - `Decimal(4458.82)` is legal Python and produces
4458.8199999999997...; `Decimal("4458.82")` is the price.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from algo.core.bar import Bar, Timeframe
from algo.core.clock import SystemClock
from algo.core.errors import DataError

#: MT5 timeframe constants, by minutes. Only the ones this project resamples to.
_TIMEFRAME_MINUTES: dict[int, str] = {
    1: "TIMEFRAME_M1",
    5: "TIMEFRAME_M5",
    15: "TIMEFRAME_M15",
    30: "TIMEFRAME_M30",
    60: "TIMEFRAME_H1",
    240: "TIMEFRAME_H4",
}

#: Broker clocks sit on whole or half hours. Anything else means the measurement
#: caught a stale tick rather than a real offset.
_OFFSET_QUANTUM_MINUTES = 30

#: No legitimate MT5 server clock is further than this from UTC.
_MAX_PLAUSIBLE_OFFSET_HOURS = 14


@runtime_checkable
class Mt5Terminal(Protocol):
    """The MetaTrader5 surface this module needs, so tests need no terminal."""

    def initialize(self, *args: Any, **kwargs: Any) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple[int, str]: ...
    def symbol_select(self, symbol: str, enable: bool) -> bool: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def symbol_info_tick(self, symbol: str) -> Any: ...
    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> Any: ...


def _decimal(value: object) -> Decimal:
    """Through `str`, never `Decimal(float)`. See the module docstring."""
    return Decimal(str(value))


def measure_server_offset(
    terminal: Mt5Terminal, symbol: str, *, now: datetime | None = None
) -> timedelta:
    """How far the broker's clock sits from UTC, measured rather than assumed.

    Compares the newest tick's timestamp - which MT5 stamps in server time -
    against a real UTC clock, then rounds to the nearest half hour, because that
    is the granularity broker clocks actually use and the raw difference carries
    a second or two of network latency.

    Raises rather than guessing when the tick is missing or the result is
    implausible: a wrong offset silently shifts every bar, which is worse than
    not starting.
    """
    tick = terminal.symbol_info_tick(symbol)
    if tick is None or not getattr(tick, "time", 0):
        raise DataError(
            f"no tick for {symbol}, so the server clock cannot be measured. The "
            "symbol may not be selected in Market Watch, or the market is closed "
            "and the terminal holds no quote."
        )
    # Read through the clock module rather than the wall clock here (D-015).
    # The `now` parameter is still the injection point; this is only its default.
    reference = now or SystemClock().now()
    # `tick.time` is server-local seconds. Reading it as UTC and differencing
    # against real UTC is exactly the offset.
    as_if_utc = datetime.fromtimestamp(tick.time, UTC)
    raw_minutes = (as_if_utc - reference).total_seconds() / 60
    rounded = round(raw_minutes / _OFFSET_QUANTUM_MINUTES) * _OFFSET_QUANTUM_MINUTES
    if abs(rounded) > _MAX_PLAUSIBLE_OFFSET_HOURS * 60:
        raise DataError(
            f"measured a server clock offset of {rounded / 60:.1f}h for {symbol}, "
            "which no broker runs. The tick is probably stale - refusing to shift "
            "every bar by a number this suspect."
        )
    return timedelta(minutes=rounded)


class Mt5BarFeed:
    """Closed XAUUSD bars from a running MT5 terminal, stamped in UTC.

    Satisfies the `BarFeed` protocol `LiveLoop` consumes, so the live loop needs
    no knowledge that MT5 is behind it.
    """

    __slots__ = ("_offset", "_symbol", "_terminal", "_tf_const", "_timeframe")

    def __init__(
        self,
        *,
        terminal: Mt5Terminal,
        symbol: str,
        timeframe: Timeframe,
        server_offset: timedelta,
    ) -> None:
        if timeframe.minutes not in _TIMEFRAME_MINUTES:
            raise DataError(
                f"MT5 has no {timeframe.minutes}-minute timeframe; available: "
                f"{sorted(_TIMEFRAME_MINUTES)}"
            )
        constant = _TIMEFRAME_MINUTES[timeframe.minutes]
        resolved = getattr(terminal, constant, None)
        if resolved is None:
            raise DataError(f"the MT5 module exposes no {constant}")
        self._terminal = terminal
        self._symbol = symbol
        self._timeframe = timeframe
        self._tf_const = resolved
        self._offset = server_offset

    @property
    def symbol(self) -> str:
        return self._symbol

    def closed_bars(self, count: int = 500) -> Sequence[Bar]:
        """The most recent **closed** bars, oldest first.

        `count + 1` is requested and the newest dropped, because position 0 is
        the bar still forming - see the module docstring. A caller asking for one
        bar gets the last closed one, not the one being built.
        """
        if count < 1:
            raise DataError(f"count must be at least 1, got {count}")
        rates = self._terminal.copy_rates_from_pos(
            self._symbol, self._tf_const, 0, count + 1
        )
        if rates is None or len(rates) == 0:
            raise DataError(
                f"MT5 returned no {self._timeframe.label} bars for {self._symbol}: "
                f"{self._terminal.last_error()}. The terminal may still be "
                "downloading history for this symbol."
            )
        return [self._to_bar(row) for row in rates[:-1]]

    def _to_bar(self, row: Any) -> Bar:
        return Bar(
            ts=datetime.fromtimestamp(int(row["time"]), UTC) - self._offset,
            timeframe=self._timeframe,
            open=_decimal(row["open"]),
            high=_decimal(row["high"]),
            low=_decimal(row["low"]),
            close=_decimal(row["close"]),
            # MT5 gives tick_volume (count of price changes) and, on some feeds,
            # real_volume. Tick volume is what is always populated on a CFD, and
            # it is a proxy for activity, not for contracts traded - the
            # distinction matters wherever volume gates tradeability.
            volume=int(row["tick_volume"]),
        )
