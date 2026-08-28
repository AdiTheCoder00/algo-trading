"""MT5 bars: the two things that would silently corrupt every result.

**The clock.** MT5 stamps bars in broker server time and never says what that
is. Vantage runs UTC+3 in summer and most MT5 brokers shift with EET, so a
hard-coded offset is wrong for a third of the year - and an hour of misalignment
means the strategy reads the wrong session on every bar.

**The forming bar.** `copy_rates_from_pos(..., 0, n)` includes the bar still
being built, whose high, low and close can all still change. Handing that to a
strategy is look-ahead through the back door, and it is the one form the
backtest's own firewall cannot catch: in live there is no future array to
withhold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from algo.core.bar import Timeframe
from algo.core.errors import DataError
from algo.data.mt5_feed import Mt5BarFeed, measure_server_offset

TF = Timeframe(minutes=30)
#: A real UTC instant to measure against, so no test depends on the wall clock.
NOW = datetime(2026, 8, 28, 19, 24, tzinfo=UTC)

_DTYPE = np.dtype(
    [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "i8"),
    ]
)


class FakeTick:
    def __init__(self, ts: datetime) -> None:
        # MT5 hands back server-local seconds, so this is the naive server
        # wall clock read as if it were UTC - exactly what the real API does.
        self.time = int(ts.replace(tzinfo=UTC).timestamp())


class FakeTerminal:
    """Stands in for the MetaTrader5 module. Server time is +3h by default."""

    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385

    def __init__(
        self, *, offset_hours: float = 3.0, bars: int = 5, tick: bool = True
    ) -> None:
        self.offset = timedelta(hours=offset_hours)
        self.requested: list[int] = []
        self._tick = tick
        self._bars = bars

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake")

    def symbol_info_tick(self, symbol: str):
        if not self._tick:
            return None
        return FakeTick(NOW + self.offset)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int):
        self.requested.append(count)
        if self._bars == 0:
            return None
        n = min(count, self._bars)
        # Newest last. The final row is the *forming* bar.
        rows = []
        for i in range(n):
            server_close = NOW + self.offset - timedelta(minutes=30 * (n - 1 - i))
            rows.append(
                (
                    int(server_close.replace(tzinfo=UTC).timestamp()),
                    4450.0 + i,
                    4460.0 + i,
                    4440.0 + i,
                    4455.0 + i,
                    1000 + i,
                )
            )
        return np.array(rows, dtype=_DTYPE)


def _feed(terminal: FakeTerminal, **kwargs: object) -> Mt5BarFeed:
    return Mt5BarFeed(
        terminal=terminal,  # type: ignore[arg-type]
        symbol="XAUUSD",
        timeframe=TF,
        server_offset=kwargs.get("server_offset", terminal.offset),  # type: ignore[arg-type]
    )


class TestMeasuringTheServerClock:
    def test_it_measures_rather_than_assumes(self) -> None:
        assert measure_server_offset(
            FakeTerminal(offset_hours=3),  # type: ignore[arg-type]
            "XAUUSD",
            now=NOW,
        ) == timedelta(hours=3)

    def test_a_winter_offset_is_measured_just_as_well(self) -> None:
        """The whole point: EET brokers shift to +2 for a third of the year, and
        a hard-coded +3 would misalign every bar by an hour."""
        assert measure_server_offset(
            FakeTerminal(offset_hours=2),  # type: ignore[arg-type]
            "XAUUSD",
            now=NOW,
        ) == timedelta(hours=2)

    def test_network_latency_is_rounded_away(self) -> None:
        """A couple of seconds of round trip must not become a 2-second offset
        applied to every bar."""
        terminal = FakeTerminal(offset_hours=3)
        terminal.offset = timedelta(hours=3, seconds=4)

        assert measure_server_offset(terminal, "XAUUSD", now=NOW) == timedelta(hours=3)  # type: ignore[arg-type]

    def test_no_tick_raises_rather_than_defaulting_to_zero(self) -> None:
        """Assuming UTC when the clock cannot be read is how every bar ends up
        silently shifted."""
        with pytest.raises(DataError, match="server clock cannot be measured"):
            measure_server_offset(FakeTerminal(tick=False), "XAUUSD", now=NOW)  # type: ignore[arg-type]

    def test_an_implausible_offset_is_refused(self) -> None:
        with pytest.raises(DataError, match="no broker runs"):
            measure_server_offset(FakeTerminal(offset_hours=40), "XAUUSD", now=NOW)  # type: ignore[arg-type]


class TestTheFormingBarIsNeverReturned:
    def test_the_newest_bar_is_dropped(self) -> None:
        """Position 0 is still being built; its close can still change."""
        terminal = FakeTerminal(bars=5)
        bars = _feed(terminal).closed_bars(4)

        newest_server = NOW + terminal.offset
        assert all(bar.ts < newest_server - terminal.offset for bar in bars)

    def test_one_extra_bar_is_requested_to_pay_for_the_drop(self) -> None:
        """Asking for four and returning three would quietly shorten history."""
        terminal = FakeTerminal(bars=99)
        bars = _feed(terminal).closed_bars(4)

        assert terminal.requested == [5]
        assert len(bars) == 4

    def test_bars_come_back_oldest_first(self) -> None:
        bars = _feed(FakeTerminal(bars=6)).closed_bars(5)

        assert [b.ts for b in bars] == sorted(b.ts for b in bars)


class TestConversion:
    def test_timestamps_are_utc_not_server_time(self) -> None:
        bars = _feed(FakeTerminal(offset_hours=3, bars=3)).closed_bars(2)

        assert all(bar.ts.tzinfo is UTC for bar in bars)
        # The newest closed bar is one timeframe behind the forming one.
        assert bars[-1].ts == NOW - timedelta(minutes=30)

    def test_a_different_offset_shifts_the_bars(self) -> None:
        three = _feed(FakeTerminal(offset_hours=3, bars=3)).closed_bars(1)
        two = _feed(FakeTerminal(offset_hours=2, bars=3)).closed_bars(1)

        assert three[0].ts == two[0].ts, (
            "the same real instant must land on the same UTC timestamp whatever "
            "the broker clock says"
        )

    def test_prices_are_decimals_built_through_str(self) -> None:
        """`Decimal(4458.82)` is legal Python and is not 4458.82."""
        bar = _feed(FakeTerminal(bars=3)).closed_bars(1)[0]

        for value in (bar.open, bar.high, bar.low, bar.close):
            assert isinstance(value, Decimal)
        assert bar.close == Decimal("4455.0")

    def test_tick_volume_is_carried(self) -> None:
        bar = _feed(FakeTerminal(bars=3)).closed_bars(1)[0]

        assert isinstance(bar.volume, int)


class TestItFailsHonestly:
    def test_no_bars_names_the_likely_cause(self) -> None:
        with pytest.raises(DataError, match="still be downloading history"):
            _feed(FakeTerminal(bars=0)).closed_bars(4)

    def test_a_timeframe_mt5_does_not_have_is_refused(self) -> None:
        with pytest.raises(DataError, match="no 7-minute timeframe"):
            Mt5BarFeed(
                terminal=FakeTerminal(),  # type: ignore[arg-type]
                symbol="XAUUSD",
                timeframe=Timeframe(minutes=7),
                server_offset=timedelta(hours=3),
            )

    def test_a_nonsense_count_is_refused(self) -> None:
        with pytest.raises(DataError, match="at least 1"):
            _feed(FakeTerminal()).closed_bars(0)
