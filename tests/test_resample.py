"""Resampling, checked against hand-computed answers.

Brief §9 Milestone 1: prove correctness on synthetic data. A constant-price
session makes the expected open, high, low and close obvious by inspection, so a
failure points at the aggregation rather than at the fixture.
"""

from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal

import pytest

from algo.core.bar import M1, M15, M30, Bar
from algo.core.errors import DataError
from algo.core.timeutil import to_ist
from algo.data.resample import check_source, expected_bar_count, resample
from algo.data.synthetic import flat_session, one_minute_session
from algo.exchange.calendar import MarketCalendar
from tests.conftest import SUMMER_DAY, WINTER_DAY


class TestBarCounts:
    def test_us_dst_session_yields_29_bars(self, calendar: MarketCalendar) -> None:
        out = resample(
            one_minute_session(calendar, SUMMER_DAY, seed=1), calendar=calendar, timeframe=M30
        )
        assert len(out) == 29
        assert not any(b.is_partial for b in out)

    def test_standard_session_yields_29_plus_a_flagged_stub(
        self, calendar: MarketCalendar
    ) -> None:
        out = resample(
            one_minute_session(calendar, WINTER_DAY, seed=1), calendar=calendar, timeframe=M30
        )
        assert len(out) == 30
        assert out[-1].is_partial
        assert to_ist(out[-1].ts).time() == time(23, 55)
        assert not any(b.is_partial for b in out[:-1])

    def test_dropping_the_stub_is_available_but_not_the_default(
        self, calendar: MarketCalendar
    ) -> None:
        source = one_minute_session(calendar, WINTER_DAY, seed=1)
        assert len(resample(source, calendar=calendar, timeframe=M30, keep_partial=False)) == 29

    def test_expected_bar_count_matches_what_is_produced(
        self, calendar: MarketCalendar
    ) -> None:
        for day in (SUMMER_DAY, WINTER_DAY):
            source = one_minute_session(calendar, day, seed=2)
            assert len(resample(source, calendar=calendar, timeframe=M30)) == expected_bar_count(
                calendar, day, M30
            )


class TestAggregationArithmetic:
    def test_constant_session_aggregates_to_the_constant(
        self, calendar: MarketCalendar
    ) -> None:
        price = Decimal("156640.50")
        out = resample(
            flat_session(calendar, SUMMER_DAY, price=price), calendar=calendar, timeframe=M30
        )
        assert len(out) == 29
        for bar in out:
            assert bar.open == bar.high == bar.low == bar.close == price

    def test_ohlcv_taken_from_the_right_members(self, calendar: MarketCalendar) -> None:
        """First open, max high, min low, last close, summed volume."""
        opened = calendar.session_open(SUMMER_DAY)
        source = [
            Bar(
                ts=opened + timedelta(minutes=i),
                timeframe=M1,
                open=Decimal(100 + i),
                high=Decimal(100 + i) + Decimal("2"),
                low=Decimal(100 + i) - Decimal("1"),
                close=Decimal(100 + i) + Decimal("1"),
                volume=i,
            )
            for i in range(1, 31)
        ]
        out = resample(source, calendar=calendar, timeframe=M30)
        assert len(out) == 1
        bar = out[0]
        assert bar.open == Decimal("101")  # first member's open
        assert bar.close == Decimal("131")  # last member's close (130 + 1)
        assert bar.high == Decimal("132")  # 130 + 2
        assert bar.low == Decimal("100")  # 101 - 1
        assert bar.volume == sum(range(1, 31))
        assert bar.ts == opened + timedelta(minutes=30)

    def test_close_labelled_intervals_are_half_open(self, calendar: MarketCalendar) -> None:
        """A bar stamped 09:30 covers (09:00, 09:30] — the 09:30 tick is inside it."""
        opened = calendar.session_open(SUMMER_DAY)
        source = [
            Bar(
                ts=opened + timedelta(minutes=i),
                timeframe=M1,
                open=Decimal("100"),
                high=Decimal("999") if i == 30 else Decimal("100"),
                low=Decimal("100"),
                close=Decimal("999") if i == 30 else Decimal("100"),
                volume=1,
            )
            for i in range(1, 61)
        ]
        out = resample(source, calendar=calendar, timeframe=M30)
        assert out[0].close == Decimal("999"), "the 09:30 bar belongs to the 09:30 slot"
        assert out[0].high == Decimal("999")

    def test_missing_minutes_do_not_invent_a_bar(self, calendar: MarketCalendar) -> None:
        source = one_minute_session(calendar, SUMMER_DAY, seed=4)
        # Remove everything in the second 30-minute slot.
        gapped = [b for b in source if not (30 < _minute_of(b, calendar) <= 60)]
        out = resample(gapped, calendar=calendar, timeframe=M30)
        assert len(out) == 28, "the empty slot is skipped, not filled with a phantom bar"


class TestSourceValidation:
    def test_duplicate_timestamps_are_rejected(self, calendar: MarketCalendar) -> None:
        source = one_minute_session(calendar, SUMMER_DAY, seed=5)
        with pytest.raises(DataError, match="duplicate"):
            check_source([*source[:10], source[9]])

    def test_out_of_order_bars_are_rejected(self, calendar: MarketCalendar) -> None:
        source = one_minute_session(calendar, SUMMER_DAY, seed=5)
        with pytest.raises(DataError, match="out of order"):
            resample([source[3], source[1]], calendar=calendar, timeframe=M30)

    def test_bars_on_a_non_trading_day_are_rejected(self, calendar: MarketCalendar) -> None:
        source = one_minute_session(calendar, SUMMER_DAY, seed=5)
        shifted = [b.model_copy(update={"ts": b.ts + timedelta(days=3)}) for b in source[:5]]
        with pytest.raises(DataError, match="non-trading days"):
            resample(shifted, calendar=calendar, timeframe=M30)

    def test_cannot_resample_to_a_finer_timeframe(self, calendar: MarketCalendar) -> None:
        source = resample(
            one_minute_session(calendar, SUMMER_DAY, seed=6), calendar=calendar, timeframe=M30
        )
        with pytest.raises(DataError, match="finer than the source"):
            resample(source, calendar=calendar, timeframe=M15)

    def test_empty_input_is_empty_output(self, calendar: MarketCalendar) -> None:
        assert resample([], calendar=calendar, timeframe=M30) == []


def _minute_of(bar: Bar, calendar: MarketCalendar) -> int:
    opened = calendar.session_open(SUMMER_DAY)
    return int((bar.ts - opened).total_seconds() // 60)
