"""Weekend sessions MCX actually held (D-107).

Evidenced, not assumed: Sunday 2026-02-01 carries 285,223 lots of real GOLDM
option volume in the supplied bhavcopy. The first backtest against real data
crashed on it, which is the only reason it was found.
"""

from __future__ import annotations

from datetime import date

import pytest

from algo.core.errors import CalendarError
from algo.exchange.calendar import MCX_SPECIAL_SESSIONS, synthetic_calendar

BUDGET_SUNDAY = date(2026, 2, 1)


class TestSpecialSessions:
    def test_the_budget_sunday_is_a_trading_day(self) -> None:
        assert BUDGET_SUNDAY.weekday() == 6  # Sunday
        assert synthetic_calendar().is_trading_day(BUDGET_SUNDAY)

    def test_it_is_listed_from_observed_data(self) -> None:
        assert BUDGET_SUNDAY in MCX_SPECIAL_SESSIONS

    def test_an_ordinary_weekend_is_still_closed(self) -> None:
        """The override must be a list of dates, never a relaxed weekend rule."""
        calendar = synthetic_calendar()

        assert not calendar.is_trading_day(date(2026, 2, 7))  # Saturday
        assert not calendar.is_trading_day(date(2026, 2, 8))  # Sunday

    def test_a_holiday_beats_a_special_session(self) -> None:
        """A date in both was scheduled and then cancelled."""
        calendar = synthetic_calendar(holidays=frozenset({BUDGET_SUNDAY}))

        assert not calendar.is_trading_day(BUDGET_SUNDAY)

    def test_session_boundaries_work_on_the_special_day(self) -> None:
        """`session_close` is what the runner actually crashed on."""
        calendar = synthetic_calendar()

        assert calendar.session_close(BUDGET_SUNDAY) is not None
        assert calendar.require_trading_day(BUDGET_SUNDAY) == BUDGET_SUNDAY

    def test_a_calendar_without_it_still_refuses(self) -> None:
        """Opting out stays possible - and must still fail loudly, not silently
        drop the session."""
        calendar = synthetic_calendar(special_sessions=frozenset())

        assert not calendar.is_trading_day(BUDGET_SUNDAY)
        with pytest.raises(CalendarError, match="not an MCX trading day"):
            calendar.require_trading_day(BUDGET_SUNDAY)
