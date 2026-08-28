"""The XAUUSD trading week (D-121).

Every boundary here was measured from 5,000 real M30 bars off the Vantage
terminal, not assumed from what an FX week is generally like. The sample is
embedded below so the test needs no terminal: the empty half-hour slots are the
daily rollover break, and the calendar has to agree with them exactly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from algo.core.errors import CalendarError
from algo.exchange.forex_calendar import ForexCalendar

CAL = ForexCalendar()

MONDAY = date(2026, 8, 24)
TUESDAY = date(2026, 8, 25)
FRIDAY = date(2026, 8, 28)
SATURDAY = date(2026, 8, 29)
SUNDAY = date(2026, 8, 23)


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=UTC)


class TestTheSessionIsNamedByItsClose:
    """Sunday evening's bars belong to Monday's session. That convention is what
    turns the daily break into an inter-session gap needing no special case."""

    def test_a_session_opens_the_previous_evening(self) -> None:
        assert CAL.session_open(MONDAY) == _at(SUNDAY, 22)

    def test_and_closes_on_its_own_date(self) -> None:
        assert CAL.session_close(MONDAY) == _at(MONDAY, 21)

    def test_sunday_evening_is_monday_trading(self) -> None:
        assert CAL.is_open(_at(SUNDAY, 23)) is True

    def test_a_session_is_23_hours(self) -> None:
        assert CAL.session_minutes(MONDAY) == 23 * 60

    def test_the_weekend_names_no_session(self) -> None:
        assert CAL.is_trading_day(SATURDAY) is False
        assert CAL.is_trading_day(SUNDAY) is False

    @pytest.mark.parametrize("day", [MONDAY, TUESDAY, FRIDAY])
    def test_weekdays_do(self, day: date) -> None:
        assert CAL.is_trading_day(day) is True


class TestTheDailyRolloverBreak:
    """21:00-22:00 UTC - midnight on the broker's own clock, and the instant
    financing is charged. The 21:00 and 21:30 slots held no bars in the sample."""

    @pytest.mark.parametrize("hh,mm", [(20, 0), (20, 30)])
    def test_just_before_the_break_is_open(self, hh: int, mm: int) -> None:
        assert CAL.is_open(_at(TUESDAY, hh, mm)) is True

    @pytest.mark.parametrize("hh,mm", [(21, 0), (21, 30)])
    def test_the_break_itself_is_closed(self, hh: int, mm: int) -> None:
        assert CAL.is_open(_at(TUESDAY, hh, mm)) is False

    @pytest.mark.parametrize("hh,mm", [(22, 0), (22, 30), (23, 30)])
    def test_after_it_reopens_into_the_next_session(self, hh: int, mm: int) -> None:
        assert CAL.is_open(_at(TUESDAY, hh, mm)) is True


class TestTheWeekend:
    def test_friday_closes_and_does_not_reopen(self) -> None:
        assert CAL.is_open(_at(FRIDAY, 20, 30)) is True
        assert CAL.is_open(_at(FRIDAY, 21)) is False
        assert CAL.is_open(_at(FRIDAY, 22)) is False, (
            "Friday's 22:00 would open Saturday's session, and there is none"
        )

    def test_saturday_is_shut_all_day(self) -> None:
        assert not any(CAL.is_open(_at(SATURDAY, h)) for h in range(24))

    def test_the_gap_is_49_hours(self) -> None:
        """Fri 21:00 to Sun 22:00. A position held across it pays financing for
        every one of those nights while being unable to react to anything."""
        assert CAL.weekend_gap(MONDAY) == timedelta(hours=49)


class TestHolidays:
    def test_a_holiday_names_no_session(self) -> None:
        calendar = ForexCalendar(holidays=frozenset({TUESDAY}))

        assert calendar.is_trading_day(TUESDAY) is False
        assert calendar.is_open(_at(TUESDAY, 12)) is False

    def test_the_gap_stretches_over_it(self) -> None:
        """A holiday adds a whole day to the ordinary one-hour rollover break,
        not another weekend: Monday closes 21:00, Wednesday opens Tuesday 22:00,
        so 25 hours. The 49-hour figure belongs to the weekend, which skips two
        dates rather than one."""
        calendar = ForexCalendar(holidays=frozenset({TUESDAY}))

        assert calendar.weekend_gap(date(2026, 8, 26)) == timedelta(hours=25)

    def test_unverified_can_be_refused_like_the_mcx_calendar(self) -> None:
        """Same rule as Q20: a calendar that has not been given real holidays
        must be able to say so rather than quietly approximating."""
        calendar = ForexCalendar(allow_unverified=False)

        with pytest.raises(CalendarError, match="no verified holiday data"):
            calendar.is_trading_day(MONDAY)


class TestEveryRealBarFallsInsideASession:
    """The validation that matters: run against the live terminal, all 5,000
    sampled bars were inside a session and none of the empty slots were. These
    are the boundary cases from that sample."""

    @pytest.mark.parametrize(
        "day,hh,mm",
        [
            (SUNDAY, 22, 0),  # first bar of the week
            (SUNDAY, 23, 30),
            (MONDAY, 0, 0),
            (MONDAY, 20, 30),  # last before the break
            (MONDAY, 22, 0),  # first after it
            (FRIDAY, 20, 30),  # last bar of the week
        ],
    )
    def test_a_bar_that_exists_is_inside_a_session(
        self, day: date, hh: int, mm: int
    ) -> None:
        assert CAL.is_open(_at(day, hh, mm)) is True

    @pytest.mark.parametrize(
        "day,hh,mm",
        [
            (MONDAY, 21, 0),
            (MONDAY, 21, 30),
            (FRIDAY, 21, 0),
            (FRIDAY, 23, 30),
            (SATURDAY, 12, 0),
            (SUNDAY, 12, 0),
        ],
    )
    def test_a_slot_with_no_bars_is_outside(self, day: date, hh: int, mm: int) -> None:
        assert CAL.is_open(_at(day, hh, mm)) is False


class TestInterfaceCompatibility:
    def test_it_needs_an_aware_instant(self) -> None:
        with pytest.raises(CalendarError, match="timezone-aware"):
            CAL.is_open(datetime(2026, 8, 24, 12))

    def test_previous_and_next_skip_the_weekend(self) -> None:
        assert CAL.previous_trading_day(MONDAY) == date(2026, 8, 21)
        assert CAL.next_trading_day(FRIDAY) == date(2026, 8, 31)

    def test_us_dst_is_meaningless_here_and_says_so(self) -> None:
        """MCX's close moves with US DST (D-017). A CFD session is fixed against
        the broker's clock, and `measure_server_offset` handles that shift."""
        assert CAL.is_us_dst_session(MONDAY) is False

    def test_requiring_a_non_session_day_raises(self) -> None:
        with pytest.raises(CalendarError, match="names no"):
            CAL.session_close(SATURDAY)
