"""`algo/strategy/trailing_profit_stop.py`: the shared trail both CFD
strategies can opt into.

Every scenario is checked once here, at the level of the primitives
(`start_trail`, `advance_trail`, `is_armed`, `trail_level`, `trail_touched`,
`trail_fill_price`) rather than only through a strategy, so a bug in the
shared logic cannot hide behind either strategy's own test suite happening
not to exercise it - the same discipline `test_price_stop.py` applies to the
flat stop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.strategy.trailing_profit_stop import (
    TrailState,
    advance_trail,
    is_armed,
    start_trail,
    trail_fill_price,
    trail_level,
    trail_touched,
)

TF = Timeframe(minutes=5)
TS = datetime(2026, 8, 24, tzinfo=UTC)
ENTRY = Decimal("4400.00")


def _bar(*, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        ts=TS, timeframe=TF, open=Decimal(open_), high=Decimal(high),
        low=Decimal(low), close=Decimal(close), volume=100,
    )


class TestStartAndAdvance:
    def test_a_fresh_trail_peaks_at_entry(self) -> None:
        state = start_trail(ENTRY, Side.BUY)

        assert state.peak == ENTRY

    def test_advancing_a_long_extends_the_peak_to_the_bars_high(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        bar = _bar(open_="4400", high="4450.00", low="4395", close="4440")

        state = advance_trail(state, bar)

        assert state.peak == Decimal("4450.00")

    def test_advancing_a_short_extends_the_peak_to_the_bars_low(self) -> None:
        state = start_trail(ENTRY, Side.SELL)
        bar = _bar(open_="4400", high="4405", low="4350.00", close="4360")

        state = advance_trail(state, bar)

        assert state.peak == Decimal("4350.00")

    def test_a_long_peak_never_retreats_on_a_pullback_bar(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4460.00", low="4395", close="4440"))

        state = advance_trail(state, _bar(open_="4440", high="4450", low="4410", close="4420"))

        assert state.peak == Decimal("4460.00")

    def test_a_short_peak_never_retreats_on_a_rally_bar(self) -> None:
        state = start_trail(ENTRY, Side.SELL)
        state = advance_trail(state, _bar(open_="4400", high="4405", low="4340.00", close="4360"))

        state = advance_trail(state, _bar(open_="4360", high="4390", low="4350", close="4380"))

        assert state.peak == Decimal("4340.00")


class TestIsArmed:
    def test_not_armed_before_the_activation_move(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4430", low="4395", close="4420"))

        assert is_armed(state, Decimal("2")) is False

    def test_armed_once_the_peak_reaches_the_activation_move(self) -> None:
        """2% above a 4400 entry is 4488.00."""
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4488.00", low="4395", close="4480"))

        assert is_armed(state, Decimal("2")) is True

    def test_a_short_arms_on_a_2pct_favourable_drop(self) -> None:
        """2% below a 4400 entry is 4312.00."""
        state = start_trail(ENTRY, Side.SELL)
        state = advance_trail(state, _bar(open_="4400", high="4405", low="4312.00", close="4320"))

        assert is_armed(state, Decimal("2")) is True

    def test_zero_activation_is_armed_immediately(self) -> None:
        """Not a "disabled" sentinel here (that is `trail_pct <= 0` in
        `trail_touched`) - a zero activation just means trail from entry."""
        state = start_trail(ENTRY, Side.BUY)

        assert is_armed(state, Decimal("0")) is True


class TestTrailLevel:
    def test_a_longs_trail_sits_below_the_peak(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4488.00", low="4395", close="4480"))

        assert trail_level(state, Decimal("0.5")) == Decimal("4465.560")

    def test_a_shorts_trail_sits_above_the_peak(self) -> None:
        state = start_trail(ENTRY, Side.SELL)
        state = advance_trail(state, _bar(open_="4400", high="4405", low="4312.00", close="4320"))

        assert trail_level(state, Decimal("0.5")) == Decimal("4333.560")


class TestCostToCostFloor:
    """A trail level must never sit worse than entry - "move the stop to
    cost" - independent of which `activation_pct`/`trail_pct` combination is
    in play. Chosen here so the un-clamped level *would* dip below entry
    without the floor, to actually exercise the clamp rather than rely on it
    never mattering for these particular numbers."""

    def test_a_wide_trail_relative_to_a_narrow_activation_is_floored_at_entry(
        self,
    ) -> None:
        """Armed at just +1%, peak 4444.00. An un-clamped 5% trail would sit
        at 4444.00 * 0.95 = 4221.80 - well below the 4400 entry. The floor
        must hold it at entry instead."""
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4444.00", low="4395", close="4440"))

        assert is_armed(state, Decimal("1")) is True
        assert trail_level(state, Decimal("5")) == ENTRY

    def test_the_short_side_floors_the_same_way(self) -> None:
        """1% below 4400 is 4356.00; an un-clamped 5% trail would sit at
        4356.00 * 1.05 = 4573.80 - above entry. Floored (capped) at entry."""
        state = start_trail(ENTRY, Side.SELL)
        state = advance_trail(state, _bar(open_="4400", high="4405", low="4356.00", close="4360"))

        assert is_armed(state, Decimal("1")) is True
        assert trail_level(state, Decimal("5")) == ENTRY

    def test_a_floored_trail_still_touches_at_exactly_entry(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4444.00", low="4395", close="4440"))
        bar = _bar(open_="4420", high="4425", low="4400.00", close="4410")

        assert trail_touched(state, bar, Decimal("1"), Decimal("5")) is True

    def test_a_floored_trail_fills_at_entry_not_the_uncapped_level(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4444.00", low="4395", close="4440"))
        bar = _bar(open_="4420", high="4425", low="4400.00", close="4410")

        assert trail_fill_price(state, bar, Decimal("5")) == ENTRY

    def test_a_normal_narrow_trail_never_reaches_the_floor(self) -> None:
        """Sanity check the other direction: D-127's own 2%/0.5% combination
        never needed this clamp - confirming the floor only bites when it is
        supposed to, not on every armed trail regardless of distance."""
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4488.00", low="4395", close="4480"))

        assert trail_level(state, Decimal("0.5")) == Decimal("4465.560")


class TestTrailTouched:
    def _armed_long(self) -> TrailState:
        state = start_trail(ENTRY, Side.BUY)
        return advance_trail(state, _bar(open_="4400", high="4488.00", low="4395", close="4480"))

    def test_zero_trail_pct_never_touches(self) -> None:
        state = self._armed_long()
        bar = _bar(open_="4480", high="4485", low="4000", close="4400")

        assert trail_touched(state, bar, Decimal("2"), Decimal("0")) is False

    def test_not_armed_never_touches_even_on_a_huge_pullback(self) -> None:
        """A pullback all the way back to entry, before the activation move was
        ever reached, must not trigger this exit - that is what the flat stop
        in `price_stop.py` is for, not this one."""
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4410", low="4395", close="4405"))
        bar = _bar(open_="4405", high="4406", low="4300", close="4310")

        assert trail_touched(state, bar, Decimal("2"), Decimal("0.5")) is False

    def test_armed_and_the_low_touches_the_trail_level(self) -> None:
        state = self._armed_long()
        # Trail level is 4465.56 (0.5% behind the 4488.00 peak).
        bar = _bar(open_="4480", high="4485", low="4465.56", close="4470")

        assert trail_touched(state, bar, Decimal("2"), Decimal("0.5")) is True

    def test_armed_but_staying_above_the_trail_level_does_not_touch(self) -> None:
        state = self._armed_long()
        bar = _bar(open_="4480", high="4485", low="4465.57", close="4470")

        assert trail_touched(state, bar, Decimal("2"), Decimal("0.5")) is False

    def test_a_short_touches_on_the_highs_rally_back_up(self) -> None:
        state = start_trail(ENTRY, Side.SELL)
        state = advance_trail(state, _bar(open_="4400", high="4405", low="4312.00", close="4320"))
        # Trail level is 4333.56 (0.5% above the 4312.00 peak).
        bar = _bar(open_="4320", high="4333.56", low="4318", close="4330")

        assert trail_touched(state, bar, Decimal("2"), Decimal("0.5")) is True

    def test_arming_and_touching_can_happen_within_the_same_bar(self) -> None:
        """The documented convention: the peak advances to this bar's best
        price first, then the (now-armed) trail is checked against this same
        bar's worst price - a single volatile bar can both arm and trigger."""
        state = start_trail(ENTRY, Side.BUY)
        bar = _bar(open_="4470", high="4488.00", low="4465.56", close="4470")

        state = advance_trail(state, bar)

        assert trail_touched(state, bar, Decimal("2"), Decimal("0.5")) is True


class TestTrailFillPrice:
    def test_a_normal_touch_fills_at_the_trail_level(self) -> None:
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4488.00", low="4395", close="4480"))
        bar = _bar(open_="4480", high="4485", low="4465.56", close="4470")

        assert trail_fill_price(state, bar, Decimal("0.5")) == Decimal("4465.560")

    def test_a_gap_through_the_trail_level_fills_at_the_open(self) -> None:
        """Same `GAPPED_STOP` doctrine as `price_stop.stop_fill_price`: the
        level itself was never tradeable, so the fill is the worse, honest
        price."""
        state = start_trail(ENTRY, Side.BUY)
        state = advance_trail(state, _bar(open_="4400", high="4488.00", low="4395", close="4480"))
        bar = _bar(open_="4450.00", high="4455", low="4440", close="4445")

        assert trail_fill_price(state, bar, Decimal("0.5")) == Decimal("4450.00")

    def test_a_short_gap_fills_at_the_open_too(self) -> None:
        state = start_trail(ENTRY, Side.SELL)
        state = advance_trail(state, _bar(open_="4400", high="4405", low="4312.00", close="4320"))
        bar = _bar(open_="4340.00", high="4345", low="4330", close="4335")

        assert trail_fill_price(state, bar, Decimal("0.5")) == Decimal("4340.00")
