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
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

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

    # -- protocol members this module never reaches, present so the fake
    #    satisfies `Mt5Terminal` structurally and no call site needs a
    #    `type: ignore` that would equally hide a genuinely wrong argument.
    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def symbol_info(self, symbol: str) -> Any:
        return None

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake")

    def symbol_info_tick(self, symbol: str) -> FakeTick | None:
        if not self._tick:
            return None
        return FakeTick(NOW + self.offset)

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> NDArray[np.void] | None:
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


def _feed(terminal: FakeTerminal, *, server_offset: timedelta | None = None) -> Mt5BarFeed:
    # An explicit keyword rather than `**kwargs: object`, which typed the offset
    # as `object` and needed a suppression to pass - one that would equally have
    # hidden a genuinely wrong argument.
    return Mt5BarFeed(
        terminal=terminal,
        symbol="XAUUSD",
        timeframe=TF,
        server_offset=terminal.offset if server_offset is None else server_offset,
    )


class TestMeasuringTheServerClock:
    def test_it_measures_rather_than_assumes(self) -> None:
        assert measure_server_offset(
            FakeTerminal(offset_hours=3),
            "XAUUSD",
            now=NOW,
        ) == timedelta(hours=3)

    def test_a_winter_offset_is_measured_just_as_well(self) -> None:
        """The whole point: EET brokers shift to +2 for a third of the year, and
        a hard-coded +3 would misalign every bar by an hour."""
        assert measure_server_offset(
            FakeTerminal(offset_hours=2),
            "XAUUSD",
            now=NOW,
        ) == timedelta(hours=2)

    def test_network_latency_is_rounded_away(self) -> None:
        """A couple of seconds of round trip must not become a 2-second offset
        applied to every bar."""
        terminal = FakeTerminal(offset_hours=3)
        terminal.offset = timedelta(hours=3, seconds=4)

        assert measure_server_offset(terminal, "XAUUSD", now=NOW) == timedelta(hours=3)

    def test_no_tick_raises_rather_than_defaulting_to_zero(self) -> None:
        """Assuming UTC when the clock cannot be read is how every bar ends up
        silently shifted."""
        with pytest.raises(DataError, match="server clock cannot be measured"):
            measure_server_offset(FakeTerminal(tick=False), "XAUUSD", now=NOW)

    def test_an_implausible_offset_is_refused(self) -> None:
        with pytest.raises(DataError, match="no broker runs"):
            measure_server_offset(FakeTerminal(offset_hours=40), "XAUUSD", now=NOW)


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
                terminal=FakeTerminal(),
                symbol="XAUUSD",
                timeframe=Timeframe(minutes=7),
                server_offset=timedelta(hours=3),
            )

    def test_a_nonsense_count_is_refused(self) -> None:
        with pytest.raises(DataError, match="at least 1"):
            _feed(FakeTerminal()).closed_bars(0)


class TestTheOffsetBoundaries:
    """`_MAX_PLAUSIBLE_OFFSET_HOURS` is the line between a real broker clock and
    a stale tick. The existing refusal test uses 40h, far outside it; these pin
    the edge, where an off-by-one would let a stale tick through or reject a
    legitimate broker."""

    def test_the_largest_plausible_offset_is_accepted(self) -> None:
        assert measure_server_offset(
            FakeTerminal(offset_hours=14), "XAUUSD", now=NOW
        ) == timedelta(hours=14)

    def test_half_an_hour_past_the_limit_is_refused(self) -> None:
        with pytest.raises(DataError, match="which no broker runs"):
            measure_server_offset(FakeTerminal(offset_hours=14.5), "XAUUSD", now=NOW)

    def test_a_broker_west_of_utc_keeps_its_sign(self) -> None:
        """Negative offsets round through the same `round()` as positive ones, and
        a sign dropped here shifts every bar the wrong way by twice the offset."""
        assert measure_server_offset(
            FakeTerminal(offset_hours=-5), "XAUUSD", now=NOW
        ) == timedelta(hours=-5)

    def test_a_broker_on_utc_measures_zero_rather_than_failing(self) -> None:
        """Zero is a real answer, not a missing one - the falsy trap this must
        not fall into, since `not timedelta(0)` is True."""
        measured = measure_server_offset(FakeTerminal(offset_hours=0), "XAUUSD", now=NOW)

        assert measured == timedelta(0)
        assert isinstance(measured, timedelta)

    def test_an_off_grid_offset_snaps_to_the_nearest_half_hour(self) -> None:
        """Broker clocks sit on whole or half hours; 2h50m is latency around 3h,
        not a 2h50m broker."""
        assert measure_server_offset(
            FakeTerminal(offset_hours=2 + 50 / 60), "XAUUSD", now=NOW
        ) == timedelta(hours=3)
        assert measure_server_offset(
            FakeTerminal(offset_hours=2 + 10 / 60), "XAUUSD", now=NOW
        ) == timedelta(hours=2)

    def test_a_tick_stamped_zero_is_treated_as_no_tick(self) -> None:
        """The guard is `not tick.time`, not `tick is None`. A terminal that
        returns a tick object with an empty timestamp would otherwise measure an
        offset against the epoch and get ~56 years."""

        class ZeroTick:
            time = 0

        class ZeroTickTerminal(FakeTerminal):
            def symbol_info_tick(self, symbol: str) -> Any:
                return ZeroTick()

        with pytest.raises(DataError, match="no tick for"):
            measure_server_offset(ZeroTickTerminal(), "XAUUSD", now=NOW)


class TestConstruction:
    def test_the_symbol_is_readable_back(self) -> None:
        """`BarFeed` consumers read `.symbol` to label what they are trading."""
        assert _feed(FakeTerminal()).symbol == "XAUUSD"

    def test_a_timeframe_the_terminal_does_not_expose_is_refused(self) -> None:
        """Distinct from the unknown-timeframe case: M1 is a timeframe this module
        supports, but an older MT5 module may not expose the constant. Guessing an
        integer for it would request an unknown timeframe from the terminal."""
        with pytest.raises(DataError, match="exposes no TIMEFRAME_M1"):
            Mt5BarFeed(
                terminal=FakeTerminal(),
                symbol="XAUUSD",
                timeframe=Timeframe(minutes=1),
                server_offset=timedelta(hours=3),
            )


class TestCountHandling:
    def test_the_default_asks_for_five_hundred_closed_bars(self) -> None:
        terminal = FakeTerminal(bars=600)
        bars = _feed(terminal).closed_bars()

        assert terminal.requested == [501]
        assert len(bars) == 500

    def test_asking_for_one_returns_the_last_closed_bar_not_the_forming_one(
        self,
    ) -> None:
        """The smallest case, where an off-by-one is easiest to get wrong and
        hands a strategy a bar whose close can still move."""
        terminal = FakeTerminal(bars=9)
        bars = _feed(terminal).closed_bars(1)

        assert terminal.requested == [2]
        assert len(bars) == 1
        assert bars[0].ts == NOW - timedelta(minutes=30)

    def test_only_a_forming_bar_available_yields_nothing_rather_than_raising(
        self,
    ) -> None:
        """One row back means the terminal has only the bar still being built.
        That is an empty result, not an error - history is still downloading, and
        the live loop polls again rather than dying."""
        bars = _feed(FakeTerminal(bars=1)).closed_bars(4)

        assert bars == []
