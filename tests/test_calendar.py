"""Session clock and the DST canary.

Brief §2.6 warns that a hardcoded offset "will break every session filter twice a
year". For MCX the trap is sharper than usual: the close time moves with **US**
daylight saving, and India has none. These tests pin both regimes and both 2026
transitions, because the bug would otherwise present as a silent one-bar-a-day
error for eight months.
"""

from __future__ import annotations

from datetime import date, time
from itertools import pairwise

import pytest

from algo.core.bar import M15, M30
from algo.core.errors import CalendarError
from algo.core.timeutil import is_us_dst, to_ist
from algo.exchange.calendar import MarketCalendar, synthetic_calendar
from tests.conftest import SUMMER_DAY, WINTER_DAY


class TestUsDstDetection:
    """US DST runs 2nd Sunday of March to 1st Sunday of November."""

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 1, 15), False),
            (date(2026, 3, 6), False),  # Friday before the switch
            (date(2026, 3, 9), True),  # Monday after — MCX moved to 23:30 on this date
            (date(2026, 8, 19), True),
            (date(2026, 10, 30), True),  # Friday before the switch back
            (date(2026, 11, 2), False),  # Monday after
            (date(2026, 12, 15), False),
        ],
    )
    def test_transitions(self, day: date, expected: bool) -> None:
        assert is_us_dst(day) is expected


class TestSessionTimes:
    def test_open_is_0900_ist_in_both_regimes(self, calendar: MarketCalendar) -> None:
        for day in (SUMMER_DAY, WINTER_DAY):
            assert to_ist(calendar.session_open(day)).time() == time(9, 0)

    def test_close_follows_us_dst(self, calendar: MarketCalendar) -> None:
        assert to_ist(calendar.session_close(SUMMER_DAY)).time() == time(23, 30)
        assert to_ist(calendar.session_close(WINTER_DAY)).time() == time(23, 55)

    def test_session_length(self, calendar: MarketCalendar) -> None:
        assert calendar.session_minutes(SUMMER_DAY) == 870
        assert calendar.session_minutes(WINTER_DAY) == 895


class TestBarBoundaries:
    """870 minutes divides by 30. 895 does not. Both answers must be right."""

    def test_us_dst_session_is_exactly_29_bars(self, calendar: MarketCalendar) -> None:
        boundaries = calendar.bar_boundaries(SUMMER_DAY, M30)
        assert len(boundaries) == 29
        assert not any(b.is_partial for b in boundaries)
        assert to_ist(boundaries[0].close_ts).time() == time(9, 30)
        assert to_ist(boundaries[-1].close_ts).time() == time(23, 30)

    def test_standard_session_is_29_bars_plus_a_stub(self, calendar: MarketCalendar) -> None:
        boundaries = calendar.bar_boundaries(WINTER_DAY, M30)
        assert len(boundaries) == 30
        assert [b.is_partial for b in boundaries] == [False] * 29 + [True]
        assert to_ist(boundaries[-1].open_ts).time() == time(23, 30)
        assert to_ist(boundaries[-1].close_ts).time() == time(23, 55)

    def test_bar_count_changes_across_the_year(self, calendar: MarketCalendar) -> None:
        """The property a naive UTC resampler gets wrong."""
        summer = len(calendar.bar_boundaries(SUMMER_DAY, M30))
        winter = len(calendar.bar_boundaries(WINTER_DAY, M30))
        assert summer != winter

    def test_boundaries_are_contiguous_and_cover_the_session(
        self, calendar: MarketCalendar
    ) -> None:
        for day in (SUMMER_DAY, WINTER_DAY):
            boundaries = calendar.bar_boundaries(day, M30)
            assert boundaries[0].open_ts == calendar.session_open(day)
            assert boundaries[-1].close_ts == calendar.session_close(day)
            for earlier, later in pairwise(boundaries):
                assert earlier.close_ts == later.open_ts

    def test_fifteen_minute_bars_divide_evenly_in_both_regimes(
        self, calendar: MarketCalendar
    ) -> None:
        """Offered in the plan as the alternative that removes the stub entirely."""
        assert len(calendar.bar_boundaries(SUMMER_DAY, M15)) == 58
        winter = calendar.bar_boundaries(WINTER_DAY, M15)
        assert len(winter) == 60
        assert [b.is_partial for b in winter] == [False] * 59 + [True]


class TestTradingDays:
    def test_weekends_are_closed(self, calendar: MarketCalendar) -> None:
        assert not calendar.is_trading_day(date(2026, 8, 22))  # Saturday
        assert not calendar.is_trading_day(date(2026, 8, 23))  # Sunday
        assert calendar.is_trading_day(date(2026, 8, 21))  # Friday

    def test_holidays_are_closed(self) -> None:
        holiday = date(2026, 8, 20)
        cal = synthetic_calendar(holidays=frozenset({holiday}))
        assert not cal.is_trading_day(holiday)
        assert cal.previous_trading_day(holiday) == date(2026, 8, 19)
        assert cal.next_trading_day(holiday) == date(2026, 8, 21)

    def test_session_on_a_closed_day_raises(self, calendar: MarketCalendar) -> None:
        with pytest.raises(CalendarError, match="not an MCX trading day"):
            calendar.session_open(date(2026, 8, 22))


class TestVerificationGate:
    """An unverified calendar refuses to answer rather than assuming a trading day.

    Silently trading on a holiday because the holiday list was never loaded is
    exactly the failure brief §1 is written against.
    """

    def test_unverified_calendar_refuses(self) -> None:
        cal = MarketCalendar(
            name="MCX",
            session=synthetic_calendar()._session,
            holidays=frozenset(),
            verified_through=None,
        )
        with pytest.raises(CalendarError, match="no verified holiday data"):
            cal.is_trading_day(SUMMER_DAY)

    def test_dates_beyond_the_verified_range_refuse(self) -> None:
        cal = MarketCalendar(
            name="MCX",
            session=synthetic_calendar()._session,
            holidays=frozenset(),
            verified_through=date(2026, 6, 30),
        )
        assert cal.is_trading_day(date(2026, 6, 30))
        with pytest.raises(CalendarError, match="Refusing to guess"):
            cal.is_trading_day(SUMMER_DAY)
