"""Expiry resolution. Decisions C-004 and D-023.

The rule under test is the one confirmed from the live terminal: GOLDM options
expire on the **last Friday of the contract month**. I had disputed that from
secondary sources and was wrong, which is precisely why the instrument master is
the source of truth here and the rule is only a cross-check.
"""

from __future__ import annotations

from datetime import date

import pytest

from algo.core.errors import CalendarError
from algo.exchange.calendar import MarketCalendar, synthetic_calendar
from algo.exchange.expiries import (
    ExpiryCalendar,
    ExpirySet,
    InstrumentMasterExpiries,
    LastFridayRule,
    last_friday,
)


class TestLastFriday:
    def test_the_confirmed_contract(self) -> None:
        """28 Aug 2026 — read off the live GOLDM chain."""
        expiry = last_friday(2026, 8)
        assert expiry == date(2026, 8, 28)
        assert expiry.weekday() == 4  # Friday

    @pytest.mark.parametrize(
        ("year", "month", "expected"),
        [
            (2026, 1, date(2026, 1, 30)),
            (2026, 2, date(2026, 2, 27)),
            (2026, 5, date(2026, 5, 29)),
            (2026, 8, date(2026, 8, 28)),
            (2026, 12, date(2026, 12, 25)),
        ],
    )
    def test_across_the_year(self, year: int, month: int, expected: date) -> None:
        assert last_friday(year, month) == expected

    def test_month_ending_on_a_friday(self) -> None:
        """31 Jul 2026 is a Friday, so it is its own last Friday."""
        assert last_friday(2026, 7) == date(2026, 7, 31)
        assert date(2026, 7, 31).weekday() == 4

    def test_rolls_back_over_a_holiday(self) -> None:
        cal = synthetic_calendar(holidays=frozenset({date(2026, 8, 28)}))
        rule = LastFridayRule(cal)
        assert rule.option_expiry("GOLDM", 2026, 8) == date(2026, 8, 27)


class TestInstrumentMasterIsAuthoritative:
    def test_an_unknown_contract_refuses_rather_than_deriving_one(self) -> None:
        master = InstrumentMasterExpiries({})
        with pytest.raises(CalendarError, match="refusing to derive"):
            master.expiry_set("GOLDM", 2026, 8)

    def test_agreement_passes(self, calendar: MarketCalendar) -> None:
        master = InstrumentMasterExpiries(
            {("GOLDM", 2026, 8): ExpirySet(option_expiry=date(2026, 8, 28))}
        )
        cal = ExpiryCalendar(authority=master, rule=LastFridayRule(calendar))
        assert cal.option_expiry("GOLDM", 2026, 8) == date(2026, 8, 28)

    def test_disagreement_halts_rather_than_choosing(self, calendar: MarketCalendar) -> None:
        """If the two sources differ, the safe move is to stop and have a human look."""
        master = InstrumentMasterExpiries(
            {("GOLDM", 2026, 8): ExpirySet(option_expiry=date(2026, 8, 26))}
        )
        cal = ExpiryCalendar(authority=master, rule=LastFridayRule(calendar))
        with pytest.raises(CalendarError, match="expiry mismatch"):
            cal.option_expiry("GOLDM", 2026, 8)


class TestExpirySelection:
    @pytest.fixture
    def cycle_calendar(self, calendar: MarketCalendar) -> ExpiryCalendar:
        master = InstrumentMasterExpiries(
            {
                ("GOLDM", 2026, 8): ExpirySet(
                    option_expiry=date(2026, 8, 28),
                    futures_expiry=date(2026, 9, 4),
                    tender_period_start=date(2026, 9, 1),
                ),
                ("GOLDM", 2026, 9): ExpirySet(
                    option_expiry=date(2026, 9, 25),
                    futures_expiry=date(2026, 10, 5),
                    tender_period_start=date(2026, 9, 30),
                ),
            }
        )
        return ExpiryCalendar(authority=master, rule=LastFridayRule(calendar))

    def test_picks_the_current_cycle_before_expiry(self, cycle_calendar: ExpiryCalendar) -> None:
        chosen = cycle_calendar.nearest_expiry_on_or_after("GOLDM", date(2026, 8, 19))
        assert chosen.option_expiry == date(2026, 8, 28)

    def test_rolls_to_the_next_cycle_after_expiry(self, cycle_calendar: ExpiryCalendar) -> None:
        chosen = cycle_calendar.nearest_expiry_on_or_after("GOLDM", date(2026, 8, 29))
        assert chosen.option_expiry == date(2026, 9, 25)

    def test_a_starting_month_with_no_listed_contract_is_skipped_not_fatal(
        self, cycle_calendar: ExpiryCalendar
    ) -> None:
        """A table built from one bhavcopy archive's worth of history may have no
        entry at all for the calendar month a session falls in — the archive's
        earliest sessions predate the month of its only listed expiry. The nearest
        listed contract still has to be found within the horizon, the same way
        `BarContext.option_expiries` already tolerates a month with nothing
        recorded (context.py) rather than raising on the very first miss."""
        chosen = cycle_calendar.nearest_expiry_on_or_after("GOLDM", date(2026, 7, 27))
        assert chosen.option_expiry == date(2026, 8, 28)

    def test_expiry_day_itself_still_counts_as_current(
        self, cycle_calendar: ExpiryCalendar
    ) -> None:
        chosen = cycle_calendar.nearest_expiry_on_or_after("GOLDM", date(2026, 8, 28))
        assert chosen.option_expiry == date(2026, 8, 28)

    def test_dte_from_the_capture_date(self, cycle_calendar: ExpiryCalendar) -> None:
        """19 Aug to 28 Aug is 9 days — the DTE used throughout the analysis."""
        chosen = cycle_calendar.expiry_set("GOLDM", 2026, 8)
        assert chosen.days_to_option_expiry(date(2026, 8, 19)) == 9

    def test_option_expires_before_the_futures_it_settles_into(
        self, cycle_calendar: ExpiryCalendar
    ) -> None:
        """The gap the devolvement guard exists to protect."""
        cycle = cycle_calendar.expiry_set("GOLDM", 2026, 8)
        assert cycle.futures_expiry is not None
        assert cycle.tender_period_start is not None
        assert cycle.option_expiry < cycle.tender_period_start < cycle.futures_expiry
