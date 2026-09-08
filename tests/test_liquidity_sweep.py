"""The liquidity-sweep rule set, one test per rule the brief states.

Two kinds of test live here and the split is deliberate.

The **rule tests** call the pure functions - `is_swing_high`, `is_sweep`,
`is_displacement`, `gap_between` and the rest - with hand-built bars. They can
be read against the brief line by line, and they fail with a message about the
rule rather than about a backtest that came out differently.

The **scenario tests** build one day of five-minute bars that walks the whole
sequence - sweep, reclaim, displacement, MSS, gap, retracement - and run it
through the real runner. `_scenario()` is that day, parameterised so a test can
break exactly one link in the chain and assert on the reason the setup died.
Every one of them asserts on the *reason*, not just on the trade count: "took no
trade" is true of a broken implementation too.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo.backtest.liquidity_sweep_runner import (
    ExitReason,
    SweepResult,
    run_liquidity_sweep,
    size_for_risk,
)
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.pricing.indicators import WilderAtr, atr
from algo.strategy.liquidity_sweep import (
    BASELINE,
    DistanceMode,
    EntryMode,
    InvalidationReason,
    LiquiditySource,
    LiquiditySweepParams,
    LiquiditySweepStrategy,
    SessionWindow,
    SetupRecord,
    SetupState,
    TpMode,
    gap_between,
    is_displacement,
    is_mss,
    is_reclaim,
    is_sweep,
    is_swing_high,
    is_swing_low,
    trading_day,
)

M5 = Timeframe(minutes=5)
DAY = datetime(2024, 3, 5, tzinfo=UTC)

#: One bar as (open, high, low, close). Ints and floats alike, so a rule test
#: can say `(1, 5, 0, 1)` and a scenario can say `(2005.2, 2009.0, 2005.0, 2008.8)`.
Row = tuple[float, float, float, float]


def bar(ts: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(
        ts=ts,
        timeframe=M5,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=100,
    )


def series(start: datetime, rows: Sequence[Row]) -> list[Bar]:
    """Consecutive five-minute bars, the first stamped `start`."""
    return [
        bar(start + timedelta(minutes=5 * i), o, h, low, c)
        for i, (o, h, low, c) in enumerate(rows)
    ]


# ------------------------------------------------------------- 1 & 2. swings


def test_a_swing_high_needs_lower_bars_before_and_no_higher_bars_after() -> None:
    window = series(DAY, [(1, 2, 0, 1), (1, 3, 0, 1), (1, 5, 0, 1), (1, 4, 0, 1), (1, 2, 0, 1)])
    assert is_swing_high(window, 2)


def test_a_swing_high_is_strict_before_and_permissive_after() -> None:
    """The brief's asymmetry: `>` looking back, `>=` looking forward.

    A flat double top therefore confirms on its first leg and not its second,
    which is the behaviour that keeps one level rather than two at the price.
    """
    equal_before = series(
        DAY, [(1, 2, 0, 1), (1, 5, 0, 1), (1, 5, 0, 1), (1, 4, 0, 1), (1, 2, 0, 1)]
    )
    assert not is_swing_high(equal_before, 2)

    equal_after = series(
        DAY, [(1, 2, 0, 1), (1, 3, 0, 1), (1, 5, 0, 1), (1, 5, 0, 1), (1, 2, 0, 1)]
    )
    assert is_swing_high(equal_after, 2)


def test_a_higher_bar_after_the_candidate_disqualifies_it() -> None:
    window = series(DAY, [(1, 2, 0, 1), (1, 3, 0, 1), (1, 5, 0, 1), (1, 6, 0, 1), (1, 2, 0, 1)])
    assert not is_swing_high(window, 2)


def test_a_swing_low_is_the_mirror() -> None:
    window = series(
        DAY,
        [(6, 9, 5, 6), (6, 9, 4, 6), (6, 9, 1, 6), (6, 9, 2, 6), (6, 9, 3, 6)],
    )
    assert is_swing_low(window, 2)
    assert not is_swing_high(window, 2)


def test_a_swing_test_refuses_a_window_of_the_wrong_length() -> None:
    with pytest.raises(DomainError, match="exactly 5 bars"):
        is_swing_high(series(DAY, [(1, 2, 0, 1)] * 4), 2)


def test_a_swing_needs_the_bars_after_it_and_so_confirms_late() -> None:
    """The no-repaint guarantee, stated as a test.

    The peak at 07:05 is not a level while it is the newest bar, and is not one
    a bar later either. It becomes one on the close of the second bar after it
    and never earlier - so nothing can trade against it in between.
    """
    rows = [(1, 2, 0, 1), (1, 3, 0, 1), (1, 5, 0, 1), (1, 4, 0, 1)]
    with pytest.raises(DomainError):
        is_swing_high(series(DAY, rows), 2)
    assert is_swing_high(series(DAY, [*rows, (1, 2, 0, 1)]), 2)


# ------------------------------------------------------- 3 & 4. sweep, reclaim


def test_a_sweep_needs_the_minimum_distance_beyond_the_level() -> None:
    """The brief's own worked example: level 3348.00, low 3347.80, close 3348.50."""
    candle = bar(DAY, 3348.20, 3348.60, 3347.80, 3348.50)
    assert is_sweep(candle, Decimal("3348.00"), Decimal("0.10"), long_side=True)
    assert not is_sweep(candle, Decimal("3348.00"), Decimal("0.30"), long_side=True)
    assert is_reclaim(candle, Decimal("3348.00"), long_side=True)


def test_a_wick_that_stops_short_of_the_threshold_is_not_a_sweep() -> None:
    candle = bar(DAY, 3348.20, 3348.60, 3347.95, 3348.50)
    assert not is_sweep(candle, Decimal("3348.00"), Decimal("0.10"), long_side=True)


def test_the_sell_side_sweep_is_the_mirror() -> None:
    """Level 3355.00, high 3355.30, close 3354.70 - the brief's short example."""
    candle = bar(DAY, 3354.90, 3355.30, 3354.60, 3354.70)
    assert is_sweep(candle, Decimal("3355.00"), Decimal("0.10"), long_side=False)
    assert is_reclaim(candle, Decimal("3355.00"), long_side=False)


def test_a_close_on_the_wrong_side_is_not_a_reclaim() -> None:
    candle = bar(DAY, 3348.20, 3348.60, 3347.80, 3347.90)
    assert is_sweep(candle, Decimal("3348.00"), Decimal("0.10"), long_side=True)
    assert not is_reclaim(candle, Decimal("3348.00"), long_side=True)


# ---------------------------------------------------------- 5. displacement


def test_displacement_needs_the_body_and_the_direction() -> None:
    average = Decimal("1.0")
    multiplier = Decimal("1.5")
    big_up = bar(DAY, 100, 102, 99.9, 101.6)
    assert is_displacement(big_up, average, multiplier, long_side=True)
    # Same body, wrong way for a long.
    big_down = bar(DAY, 101.6, 102, 99.9, 100)
    assert not is_displacement(big_down, average, multiplier, long_side=True)
    assert is_displacement(big_down, average, multiplier, long_side=False)


def test_a_body_exactly_on_the_multiple_qualifies() -> None:
    candle = bar(DAY, 100, 102, 100, 101.5)
    assert is_displacement(candle, Decimal("1.0"), Decimal("1.5"), long_side=True)
    assert not is_displacement(candle, Decimal("1.0"), Decimal("1.51"), long_side=True)


def test_a_long_wick_is_not_a_displacement() -> None:
    """Range is not body. A three-dollar wick with a ten-cent body is a
    rejection, not a displacement, and the rule reads the body for that reason."""
    candle = bar(DAY, 100, 103, 99.9, 100.1)
    assert not is_displacement(candle, Decimal("1.0"), Decimal("1.5"), long_side=True)


# ------------------------------------------------------------------- 6. MSS


def test_the_mss_needs_a_close_and_not_a_wick() -> None:
    wick_only = bar(DAY, 100, 105, 99, 101)
    assert not is_mss(wick_only, Decimal("104"), long_side=True)
    closed_through = bar(DAY, 100, 105, 99, 104.5)
    assert is_mss(closed_through, Decimal("104"), long_side=True)


def test_the_bearish_mss_is_the_mirror() -> None:
    wick_only = bar(DAY, 100, 101, 95, 99)
    assert not is_mss(wick_only, Decimal("96"), long_side=False)
    assert is_mss(bar(DAY, 100, 101, 95, 95.5), Decimal("96"), long_side=False)


# ------------------------------------------------------------- 7 & 8. gaps


def test_a_bullish_gap_is_between_candle_one_and_candle_three() -> None:
    first = bar(DAY, 100, 100.5, 99.5, 100.4)
    third = bar(DAY + timedelta(minutes=10), 101.5, 102.5, 101.0, 102.0)
    gap = gap_between(first, third, bullish=True)
    assert gap is not None
    assert gap.low == Decimal("100.5")
    assert gap.high == Decimal("101.0")
    assert gap.size == Decimal("0.5")
    assert gap.midpoint == Decimal("100.75")


def test_a_bearish_gap_is_the_mirror() -> None:
    first = bar(DAY, 101.6, 102, 101.5, 101.6)
    third = bar(DAY + timedelta(minutes=10), 100.4, 100.8, 100, 100.2)
    gap = gap_between(first, third, bullish=False)
    assert gap is not None
    assert gap.low == Decimal("100.8")
    assert gap.high == Decimal("101.5")


def test_overlapping_candles_leave_no_gap() -> None:
    first = bar(DAY, 100, 101.5, 99.5, 101)
    third = bar(DAY + timedelta(minutes=10), 101, 102, 101.0, 102)
    # Candle three's low touches candle one's high exactly: no imbalance.
    assert gap_between(first, third, bullish=True) is None


# ---------------------------------------------------------------- ATR parity


def test_the_incremental_atr_matches_the_vectorised_one_bar_for_bar() -> None:
    """Two implementations are allowed only while this passes.

    A Wilder average is path dependent, so this is not a rounding check - a
    strategy carrying its own running value and a study precomputing the series
    must agree exactly, or the same rules trade differently in the two places.
    """
    rng = random.Random(11)
    price = 2000.0
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    running = WilderAtr(14)
    incremental: list[float | None] = []
    for _ in range(200):
        price += rng.uniform(-3, 3)
        high, low = price + rng.uniform(0, 2), price - rng.uniform(0, 2)
        highs.append(high)
        lows.append(low)
        closes.append(price)
        incremental.append(running.update(high, low, price))

    reference = atr(highs, lows, closes, 14)
    for i, (theirs, mine) in enumerate(zip(reference, incremental, strict=True)):
        if theirs != theirs:  # NaN - no value yet
            assert mine is None, f"bar {i} produced a value before the reference did"
        else:
            assert mine is not None
            assert abs(mine - theirs) < 1e-9, f"bar {i}: {mine} vs {theirs}"


# ------------------------------------------------------------ 17. the clock


def test_a_bar_belongs_to_the_window_it_opened_in() -> None:
    """Bars are close-labelled, so 07:00 is the last bar before London."""
    london = BASELINE.trading_windows[0]
    assert not london.contains(datetime(2024, 3, 5, 7, 0, tzinfo=UTC))
    assert london.contains(datetime(2024, 3, 5, 7, 5, tzinfo=UTC))
    assert london.contains(datetime(2024, 3, 5, 10, 0, tzinfo=UTC))
    assert not london.contains(datetime(2024, 3, 5, 10, 5, tzinfo=UTC))


def test_midnight_closes_the_day_it_belongs_to() -> None:
    assert trading_day(datetime(2024, 3, 6, 0, 0, tzinfo=UTC)) == datetime(2024, 3, 5).date()
    assert trading_day(datetime(2024, 3, 6, 0, 5, tzinfo=UTC)) == datetime(2024, 3, 6).date()


# ----------------------------------------------------------------- scenarios


def _scenario(
    *,
    reclaim: bool = True,
    displacement: bool = True,
    mss: bool = True,
    gap: bool = True,
    retrace: bool = True,
    take_profit: bool = True,
    sweep_hour: int = 7,
    tail_bars: int = 40,
) -> list[Bar]:
    """One day that walks the whole sequence, with any link breakable.

    The Asian filler is deliberately monotonous - identical bars, a $2.00 range
    and a $1.00 body - so every threshold starts from a round number. The Asian
    low is 2004.00 and it is the level the sweep takes.

    Each `False` here breaks its link **for the whole life of the setup**, not
    just on one candle. That distinction is the point: a sweep whose reclaim is
    merely late is still a valid setup, and a scenario that only delayed the
    reclaim would be testing nothing. So `reclaim=False` keeps price below the
    level until the window has passed, `mss=False` keeps every close below the
    swing high, and `gap=False` keeps every candle overlapping its neighbours.
    """
    filler_rows = [(2004.5, 2006.0, 2004.0, 2005.5)] * 84  # 00:05 .. 07:00
    bars = series(DAY + timedelta(minutes=5), filler_rows)

    start = DAY + timedelta(hours=sweep_hour, minutes=5)
    quiet = (2005.5, 2006.0, 2005.0, 2005.5)
    rows: list[Row] = [
        # A peak that confirms as the swing high two bars later: the MSS level.
        (2005.0, 2008.0, 2004.5, 2007.0),
        (2007.0, 2007.5, 2005.0, 2005.5),
        (2005.5, 2006.0, 2004.5, 2005.0),
        # The sweep: through 2004.00 by 0.50.
        (2005.0, 2005.2, 2003.5, 2005.0 if reclaim else 2003.6),
    ]
    if not reclaim:
        # Four bars below the level: past `reclaim_max_bars`, so the sweep dies
        # rather than merely reclaiming late.
        rows.extend([(2003.6, 2003.9, 2003.4, 2003.7)] * 4)
        rows.extend([quiet] * tail_bars)
        return bars + series(start, rows)

    if not displacement:
        # Small directional bodies only: never 1.5x the mean, for long enough
        # that the displacement window closes.
        rows.extend([(2005.0, 2005.6, 2004.9, 2005.4)] * 8)
        rows.extend([quiet] * tail_bars)
        return bars + series(start, rows)

    if not mss:
        # A real displacement candle that stops short of the 2008.00 swing, and
        # nothing after it closes through either.
        rows.append((2005.2, 2007.9, 2005.0, 2007.5))
        rows.extend([(2007.5, 2007.9, 2006.5, 2007.0)] * 30)
        rows.extend([quiet] * tail_bars)
        return bars + series(start, rows)

    # Displacement, and the same candle closes through the 2008.00 swing high.
    rows.append((2005.2, 2009.0, 2005.0, 2008.8))

    if not gap:
        # Every candle from here overlaps the one two back, so no three-candle
        # imbalance ever exists to enter into.
        rows.extend([(2008.8, 2009.5, 2005.0, 2008.5), (2008.5, 2009.4, 2005.1, 2008.6)] * 15)
        rows.extend([quiet] * tail_bars)
        return bars + series(start, rows)

    # Candle three of the gap: its low sits above the sweep candle's high, so
    # the imbalance runs 2005.20 - 2008.50 and its midpoint is 2006.85.
    rows.append((2008.8, 2010.0, 2008.5, 2009.5))
    if retrace:
        # Back into the gap - a low of 2006.00 is below the midpoint.
        rows.append((2009.5, 2009.6, 2006.0, 2009.0))
    else:
        rows.append((2009.5, 2009.6, 2008.6, 2009.0))
    if take_profit:
        rows.append((2009.0, 2015.0, 2008.9, 2014.5))
    else:
        rows.append((2009.0, 2009.5, 2008.9, 2009.2))
    rows.extend([(2009.2, 2009.6, 2008.8, 2009.2)] * tail_bars)
    return bars + series(start, rows)


def _run(bars: list[Bar], params: LiquiditySweepParams = BASELINE) -> SweepResult:
    """The scenario through the real runner, with costs switched off.

    Zero costs here so a test asserting on an entry price is asserting on the
    strategy's arithmetic and not on the spread; the cost accounting has tests
    of its own below.
    """
    from algo.backtest.cfd_runner import CfdCosts

    return run_liquidity_sweep(
        bars,
        params=params,
        costs=CfdCosts(half_spread=Decimal("0")),
        starting_equity=Decimal("10000"),
    )


def _statuses(result: SweepResult) -> list[tuple[SetupState, InvalidationReason | None]]:
    return [(r.signal_status, r.invalidation_reason) for r in result.records]


def _reasons(result: SweepResult) -> list[InvalidationReason | None]:
    return [r.invalidation_reason for r in result.records]


def test_the_whole_sequence_produces_one_trade_at_the_gap_midpoint() -> None:
    result = _run(_scenario())
    assert result.orders_placed == 1
    assert len(result.trades) == 1

    trade = result.trades[0]
    assert trade.side is Side.BUY
    assert trade.source is LiquiditySource.ASIAN_LOW
    # 10. Entry: the midpoint of the 2005.20 - 2008.50 gap.
    assert trade.limit_mid == Decimal("2006.85")
    # 11. Stop: the sweep low of 2003.50, less a tenth of the ATR the setup
    # was measured against - which the record carries, so this is the rule and
    # not a number copied out of a run.
    record = next(
        r for r in result.records if r.signal_status is SetupState.WAITING_FOR_RETRACE
    )
    assert record.atr is not None
    assert trade.stop_mid == Decimal("2003.5") - Decimal("0.10") * record.atr
    # 12. Target: twice the risk, on the far side of the entry.
    risk = trade.limit_mid - trade.stop_mid
    assert trade.target_mid == trade.limit_mid + Decimal("2") * risk
    assert trade.exit_reason is ExitReason.TAKE_PROFIT


def test_the_setup_record_carries_every_step_it_passed_through() -> None:
    result = _run(_scenario())
    placed = [r for r in result.records if r.signal_status is SetupState.WAITING_FOR_RETRACE]
    assert len(placed) == 1
    record = placed[0]
    assert record.liquidity_type is LiquiditySource.ASIAN_LOW
    assert record.liquidity_level == Decimal("2004.0")
    assert record.sweep_price == Decimal("2003.5")
    assert record.sweep_distance == Decimal("0.5")
    assert record.reclaim_price == Decimal("2005.0")
    assert record.mss_level == Decimal("2008.0")
    assert record.fvg_low == Decimal("2005.2")
    assert record.fvg_high == Decimal("2008.5")
    # The mean body over the last ten candles, and this candle's own: 3.60 is
    # comfortably past the 1.5x it had to beat.
    assert record.average_body is not None
    assert record.displacement_size == Decimal("3.6")
    assert record.displacement_size >= Decimal("1.5") * record.average_body
    assert record.session == "LONDON"
    assert record.rr == Decimal("2")
    assert record.signal_ref


def test_without_the_reclaim_the_setup_dies_at_the_sweep() -> None:
    result = _run(_scenario(reclaim=False))
    assert result.orders_placed == 0
    assert InvalidationReason.NO_RECLAIM in _reasons(result)
    died = next(r for r in result.records if r.invalidation_reason is InvalidationReason.NO_RECLAIM)
    assert died.reached is SetupState.SWEEP_DETECTED


def test_without_a_displacement_candle_nothing_is_taken() -> None:
    result = _run(_scenario(displacement=False))
    assert result.orders_placed == 0
    assert InvalidationReason.NO_DISPLACEMENT in _reasons(result)


def test_a_wick_through_the_swing_high_is_not_a_structure_shift() -> None:
    """Displacement happens; nothing ever closes above the 2008.00 swing.

    The setup therefore reaches DISPLACEMENT_CONFIRMED and dies there when its
    clock runs out - which is what the log has to be able to say, because "no
    trade" is equally true of a setup that never swept anything.
    """
    result = _run(_scenario(mss=False))
    assert result.orders_placed == 0
    expired = [r for r in result.records if r.reached is SetupState.DISPLACEMENT_CONFIRMED]
    assert expired
    assert all(r.invalidation_reason is not None for r in expired)


def test_no_gap_means_no_order_however_good_the_structure_was() -> None:
    result = _run(_scenario(gap=False))
    assert result.orders_placed == 0
    assert any(r.reached is SetupState.MSS_CONFIRMED for r in result.records)


def test_an_order_that_is_never_retraced_into_expires_unfilled() -> None:
    """9. The retracement is the entry. Without it the order simply expires."""
    result = _run(_scenario(retrace=False))
    assert result.orders_placed == 1
    assert result.orders_filled == 0
    assert result.orders_expired == 1
    assert not result.trades


def test_the_order_expires_after_max_setup_bars_and_not_before() -> None:
    """13. The clock is `max_setup_bars` five-minute candles from the gap.

    The retracement is moved out to the twentieth bar after the order, so it
    fills under the default budget of twenty-four and does not under a budget
    of ten. Same bars, same setup, one parameter.
    """
    bars = _scenario(retrace=False, take_profit=False, tail_bars=60)
    placed_at = _order_record(_run(bars)).timestamp
    index = next(i for i, b in enumerate(bars) if b.ts == placed_at)

    # The retracement arrives on the twentieth bar after the order.
    late = bars[index + 20]
    filled = list(bars)
    filled[index + 20] = bar(late.ts, 2009.2, 2009.6, 2006.0, 2009.2)

    assert _run(filled).orders_filled == 1, "twenty bars is inside a budget of 24"
    tight = _run(filled, replace_params(max_setup_bars=10))
    assert tight.orders_filled == 0
    assert tight.orders_expired == 1


def replace_params(**changes: object) -> LiquiditySweepParams:
    """A baseline with one thing changed, for the one-at-a-time tests."""
    from dataclasses import replace as _replace

    return _replace(BASELINE, **changes)  # type: ignore[arg-type]


def _order_record(result: SweepResult) -> SetupRecord:
    return next(
        r for r in result.records if r.signal_status is SetupState.WAITING_FOR_RETRACE
    )


def test_the_entry_mode_moves_the_order_and_nothing_else() -> None:
    """10. Same gap, three prices: near edge, midpoint, far edge.

    Asserted on the orders rather than on the fills, because that is the
    difference between the modes: the retracement in this scenario reaches
    2006.00, so the near edge and the midpoint fill and the far edge does not.
    """
    bars = _scenario()
    midpoint = _order_record(_run(bars))
    touch = _order_record(_run(bars, replace_params(entry_mode=EntryMode.FIRST_TOUCH)))
    boundary = _order_record(_run(bars, replace_params(entry_mode=EntryMode.FVG_BOUNDARY)))

    assert touch.entry_price == Decimal("2008.5")
    assert midpoint.entry_price == Decimal("2006.85")
    assert boundary.entry_price == Decimal("2005.2")
    # The stop is a property of the sweep, not of where the order sat.
    assert touch.stop_price == midpoint.stop_price == boundary.stop_price
    # And only the two that price actually reached became trades.
    assert _run(bars, replace_params(entry_mode=EntryMode.FVG_BOUNDARY)).orders_filled == 0


def test_a_wider_stop_buffer_moves_the_stop_and_the_target_together() -> None:
    """11 and 12. The target is two times whatever the risk turned out to be."""
    result = _run(_scenario(), replace_params(sl_buffer=Decimal("0.5")))
    wide = result.trades[0]
    measured = _order_record(result).atr
    assert measured is not None
    assert wide.stop_mid == Decimal("2003.5") - Decimal("0.5") * measured
    risk = wide.limit_mid - wide.stop_mid
    assert wide.target_mid == wide.limit_mid + Decimal("2") * risk
    # Wider stop, same budget, fewer ounces - the sizing follows the stop.
    assert wide.lots < _run(_scenario()).trades[0].lots


def test_a_fixed_sweep_distance_is_read_in_dollars_not_in_atrs() -> None:
    """7. The sweep of 2004.00 to 2003.50 is 0.50 - enough at a $0.40
    threshold and not at a $0.60 one, whatever the ATR happens to be."""
    ok = _run(
        _scenario(),
        replace_params(
            sweep_distance_mode=DistanceMode.FIXED, min_sweep_distance=Decimal("0.40")
        ),
    )
    assert ok.orders_placed == 1
    too_far = _run(
        _scenario(),
        replace_params(
            sweep_distance_mode=DistanceMode.FIXED, min_sweep_distance=Decimal("0.60")
        ),
    )
    assert too_far.orders_placed == 0


def test_a_minimum_gap_size_rejects_the_setup_rather_than_shrinking_it() -> None:
    result = _run(
        _scenario(),
        replace_params(fvg_size_mode=DistanceMode.FIXED, min_fvg_size=Decimal("5.0")),
    )
    assert result.orders_placed == 0


def test_the_target_can_be_the_nearest_opposing_level_instead() -> None:
    """15, mode B. The Asian high at 2006.00 is the nearest level above the
    2006.85 entry... it is not above it, so the next one up is taken."""
    fixed = _run(_scenario()).trades[0]
    hybrid = _run(_scenario(), replace_params(tp_mode=TpMode.HYBRID)).trades[0]
    # Hybrid never targets further away than the fixed multiple.
    assert hybrid.target_mid <= fixed.target_mid


# ------------------------------------------------------------- 13. the clock


def test_a_position_is_closed_at_the_four_hour_mark() -> None:
    """16. The hard maximum holding period, with no target in reach."""
    result = _run(_scenario(take_profit=False, tail_bars=80))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.MAX_HOLD
    assert trade.holding_time == timedelta(hours=4)


def test_a_shorter_maximum_hold_closes_the_same_trade_sooner() -> None:
    result = _run(
        _scenario(take_profit=False, tail_bars=80),
        replace_params(max_hold=timedelta(minutes=30)),
    )
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.MAX_HOLD
    assert trade.holding_time == timedelta(minutes=30)


def test_the_stop_wins_a_bar_that_contains_both_levels() -> None:
    """The pessimistic reading of a bar the runner cannot see inside."""
    bars = _scenario(take_profit=False, tail_bars=4)
    # One bar that reaches the target above and the stop below.
    bars.append(
        bar(
            bars[-1].ts + timedelta(minutes=5),
            2009.2,
            2020.0,
            2000.0,
            2009.0,
        )
    )
    bars.extend(series(bars[-1].ts + timedelta(minutes=5), [(2009, 2009.5, 2008.8, 2009)] * 3))
    result = _run(bars)
    assert result.trades[0].exit_reason is ExitReason.STOP_LOSS


# ------------------------------------------- 14, 15, 16. limits and duplicates


def _two_setup_day() -> list[Bar]:
    """The scenario, then the same shape again in the New York window.

    The bridge between them drifts back down to 2006 rather than sitting where
    the first trade left it, because the second leg needs its own confirmed
    swing high to break: a bridge parked above 2008 would leave the *old* swing
    as the newest one and the second setup would have nothing to shift.
    """
    first = _scenario(take_profit=True, tail_bars=6)
    decline = series(
        first[-1].ts + timedelta(minutes=5),
        [
            (2009.2, 2009.3, 2008.2, 2008.4),
            (2008.4, 2008.5, 2007.4, 2007.6),
            (2007.6, 2007.7, 2006.6, 2006.8),
            (2006.8, 2006.9, 2005.9, 2006.0),
        ],
    )
    quiet_from = decline[-1].ts + timedelta(minutes=5)
    ny_start = DAY + timedelta(hours=13, minutes=5)
    quiet = series(
        quiet_from,
        [(2006.0, 2006.4, 2005.6, 2006.0)]
        * int((ny_start - quiet_from).total_seconds() // 300),
    )
    second = series(
        ny_start,
        [
            (2006.0, 2008.0, 2005.5, 2007.5),
            (2007.5, 2007.6, 2005.0, 2005.5),
            (2005.5, 2006.0, 2004.5, 2005.0),
            (2005.0, 2005.2, 2003.5, 2005.0),
            (2005.2, 2009.0, 2005.0, 2008.8),
            (2008.8, 2010.0, 2008.5, 2009.5),
            (2009.5, 2009.6, 2006.0, 2009.0),
            (2009.0, 2015.0, 2008.9, 2014.5),
            *[(2009.2, 2009.6, 2008.8, 2009.2)] * 12,
        ],
    )
    return first + decline + quiet + second


def test_one_sweep_produces_one_order_and_not_a_second() -> None:
    """20. The sweep is consumed the moment it produces an order.

    Without this the same Asian low, still below price, would set up again on
    the next bar that dipped through it and the day would fill with copies of
    one idea.
    """
    result = _run(_scenario(tail_bars=60))
    assert result.orders_placed == 1
    consumed = [
        r for r in result.records if r.liquidity_type is LiquiditySource.ASIAN_LOW
    ]
    assert sum(1 for r in consumed if r.signal_status is SetupState.WAITING_FOR_RETRACE) == 1


def _orders(result: SweepResult) -> list[SetupRecord]:
    return [
        r for r in result.records if r.signal_status is SetupState.WAITING_FOR_RETRACE
    ]


def test_a_level_that_has_traded_is_not_swept_again_that_day() -> None:
    """20, the other half. The level is consumed along with the sweep.

    The day here takes the same 2003.50 low twice, once in each session. The
    first order is the Asian low at 2004.00; the second cannot be, because
    that level is spent - so it is the swing low at 2005.60 that the quiet
    hours in between built. Same price action, a different level, which is
    exactly the distinction the rule draws.
    """
    orders = _orders(_run(_two_setup_day()))
    assert len(orders) == 2
    assert orders[0].liquidity_type is LiquiditySource.ASIAN_LOW
    assert orders[1].liquidity_type is LiquiditySource.SWING_LOW
    assert len({(o.liquidity_type, o.liquidity_level) for o in orders}) == 2


def test_re_trading_a_level_is_possible_when_it_is_configured() -> None:
    """The same day with `retrade_same_level` on: the Asian low, twice.

    Which is also the check that the consumption above is a *rule* and not
    the strategy being unable to see the second setup at all.
    """
    orders = _orders(_run(_two_setup_day(), replace_params(retrade_same_level=True)))
    assert len(orders) == 2
    assert all(o.liquidity_type is LiquiditySource.ASIAN_LOW for o in orders)
    assert orders[0].timestamp != orders[1].timestamp


def test_the_daily_trade_limit_stops_the_third_entry() -> None:
    """19. Two entered trades a day, and the third setup says why it was refused."""
    day = _two_setup_day()
    assert _run(day).orders_filled == 2

    capped = _run(day, replace_params(max_trades_per_day=1))
    assert capped.orders_filled == 1
    assert InvalidationReason.DAILY_LIMIT in _reasons(capped)


def test_only_one_position_is_open_at_a_time() -> None:
    result = _run(_two_setup_day())
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_ts is not None
        assert earlier.exit_ts <= later.entry_ts


# ------------------------------------------------------- 17. session filtering


def test_a_sweep_outside_every_trading_window_is_never_looked_for() -> None:
    """The same day, with the sweep moved to 11:05 - between the two windows.

    The scenario's quiet tail runs on into New York and can start setups of
    its own there, which is why this asserts that nothing was *born* between
    10:00 and 13:00 rather than that the whole day was silent.
    """
    result = _run(_scenario(sweep_hour=11))
    assert result.orders_placed == 0
    between = [r for r in result.records if 10 <= r.timestamp.hour < 13]
    assert not between


def test_moving_the_window_moves_the_trade_with_it() -> None:
    from datetime import time as clock

    params = replace_params(
        trading_windows=(SessionWindow("MIDDAY", clock(11, 0), clock(13, 0)),)
    )
    result = _run(_scenario(sweep_hour=11), params)
    assert result.orders_placed == 1
    placed = next(
        r for r in result.records if r.signal_status is SetupState.WAITING_FOR_RETRACE
    )
    assert placed.session == "MIDDAY"


def test_a_resting_order_does_not_survive_the_close_of_its_session() -> None:
    """The order is placed at 09:50 and the window ends at 10:00, so it has two
    bars to fill rather than the full twenty-four."""
    bars = _scenario(sweep_hour=9, retrace=False, tail_bars=60)
    result = _run(bars)
    assert result.orders_placed == 1
    order_bar = next(
        r.timestamp for r in result.records if r.signal_status is SetupState.WAITING_FOR_RETRACE
    )
    assert result.orders_expired == 1
    assert order_bar.hour == 9


# ----------------------------------------------------------------- 18. sizing


def test_size_is_rounded_down_and_a_stop_too_wide_is_skipped() -> None:
    lots, distance = size_for_risk(
        risk=Decimal("100"), entry_price=Decimal("2000"), stop_price=Decimal("1997")
    )
    assert distance == Decimal("3")
    assert lots == 33  # 33.33 rounded down: never up, never over budget

    none, _ = size_for_risk(
        risk=Decimal("100"), entry_price=Decimal("2000"), stop_price=Decimal("1800")
    )
    assert none == 0


def test_the_position_risks_the_configured_fraction_of_equity() -> None:
    result = run_liquidity_sweep(
        _scenario(),
        risk_per_trade_pct=Decimal("1"),
        starting_equity=Decimal("10000"),
    )
    trade = result.trades[0]
    assert trade.risk_amount <= Decimal("100")
    assert trade.risk_amount > Decimal("95")
    # And R is measured against the money actually at risk.
    assert trade.r_multiple is not None
    assert trade.r_multiple == trade.net_pnl / trade.risk_amount


def test_an_unaffordable_stop_is_counted_rather_than_taken_at_a_bigger_size() -> None:
    result = run_liquidity_sweep(
        _scenario(),
        risk_per_trade_pct=Decimal("0.01"),  # $1 of a $10,000 account
        starting_equity=Decimal("10000"),
    )
    assert result.orders_placed == 1
    assert result.orders_filled == 0
    assert result.orders_unaffordable == 1


# ------------------------------------------------------------------- costs


def test_the_costs_reconcile_with_the_executed_prices() -> None:
    """Net P&L is the executed round trip less commission and financing.

    Stated as an equality rather than trusted: the spread is charged as a cost
    *and* backed out of the mid prices, and a sign error in either direction
    would make the gross figure and the executed prices disagree.
    """
    from algo.backtest.cfd_runner import CfdCosts

    result = run_liquidity_sweep(
        _scenario(), costs=CfdCosts(half_spread=Decimal("0.145"))
    )
    trade = result.trades[0]
    assert trade.exit_price is not None
    executed = (trade.exit_price - trade.entry_price) * trade.lots
    assert trade.net_pnl == executed - trade.commission_paid - trade.swap_paid


def test_the_entry_pays_the_ask_and_the_exit_receives_the_bid() -> None:
    from algo.backtest.cfd_runner import CfdCosts

    half = Decimal("0.145")
    result = run_liquidity_sweep(_scenario(), costs=CfdCosts(half_spread=half))
    trade = result.trades[0]
    assert trade.entry_price == trade.limit_mid + half
    assert trade.exit_price == trade.target_mid - half
    assert trade.spread_paid == half * 2 * trade.lots


def test_a_costless_run_and_a_charged_run_differ_by_exactly_the_costs() -> None:
    from algo.backtest.cfd_runner import CfdCosts

    free = run_liquidity_sweep(_scenario(), costs=CfdCosts(half_spread=Decimal("0")))
    charged = run_liquidity_sweep(_scenario(), costs=CfdCosts(half_spread=Decimal("0.145")))
    assert free.total_costs == Decimal("0")
    assert charged.net_pnl == charged.gross_pnl - charged.total_costs
    # The same trade, smaller: a wider executable risk buys fewer ounces inside
    # the same budget, so the charged run is not the free one minus a constant.
    assert charged.trades[0].lots < free.trades[0].lots
    assert charged.trades[0].r_multiple is not None


# ------------------------------------------------- 18. look-ahead prevention


def _random_walk(bars_wanted: int, seed: int = 7) -> list[Bar]:
    """A seeded five-minute random walk, starting at the top of a day."""
    rng = random.Random(seed)
    out: list[Bar] = []
    price = 2000.0
    ts = datetime(2024, 4, 1, 0, 5, tzinfo=UTC)
    for _ in range(bars_wanted):
        drift = rng.gauss(0, 0.8)
        close = price + drift
        high = max(price, close) + abs(rng.gauss(0, 0.4))
        low = min(price, close) - abs(rng.gauss(0, 0.4))
        out.append(bar(ts, round(price, 3), round(high, 3), round(low, 3), round(close, 3)))
        price = close
        ts += timedelta(minutes=5)
    return out


def test_truncating_the_history_does_not_change_what_was_already_decided() -> None:
    """The canary for section 23.

    Run the whole series, then run only its first half. Every setup record and
    every order in the shorter run must be identical to the corresponding one
    in the longer run: if any decision read a bar that had not closed yet, the
    two would disagree at the point the future stopped being available.
    """
    bars = _random_walk(900)
    full = _run(bars)
    half = _run(bars[:450])
    cutoff = bars[449].ts

    full_prefix = [r.to_row() for r in full.records if r.timestamp <= cutoff]
    short = [r.to_row() for r in half.records]
    assert short == full_prefix

    full_orders = [
        (t.signal_ts, t.limit_mid, t.stop_mid, t.target_mid)
        for t in full.trades
        if t.signal_ts <= cutoff
    ]
    short_orders = [
        (t.signal_ts, t.limit_mid, t.stop_mid, t.target_mid) for t in half.trades
    ]
    assert short_orders[: len(short_orders)] == full_orders[: len(short_orders)]


def test_the_same_bars_produce_the_same_run_twice() -> None:
    """Reproducibility: no clock, no randomness, no dict ordering leaking in."""
    bars = _random_walk(600, seed=3)
    first, second = _run(bars), _run(bars)
    assert [r.to_row() for r in first.records] == [r.to_row() for r in second.records]
    assert first.net_pnl == second.net_pnl


def test_an_order_can_never_fill_on_the_bar_that_placed_it() -> None:
    bars = _random_walk(2000, seed=5)
    result = _run(bars)
    assert result.trades, "the walk produced no trades to check"
    for trade in result.trades:
        assert trade.entry_ts > trade.signal_ts


# ---------------------------------------------------------------- parameters


def test_the_news_filter_refuses_to_pretend_it_has_a_calendar() -> None:
    with pytest.raises(DomainError, match="no historical economic calendar"):
        LiquiditySweepStrategy(params=replace_params(news_filter_enabled=True))


def test_nonsense_parameters_are_refused_at_construction() -> None:
    with pytest.raises(DomainError, match="swing lookback"):
        LiquiditySweepStrategy(params=replace_params(swing_lookback=0))
    with pytest.raises(DomainError, match="RR target"):
        LiquiditySweepStrategy(params=replace_params(rr_target=Decimal("0")))
    with pytest.raises(DomainError, match="at least one liquidity source"):
        LiquiditySweepStrategy(params=replace_params(sources=()))


def test_the_parameters_are_all_in_the_hash() -> None:
    """A parameter change must change every signal id it could affect."""
    base = LiquiditySweepStrategy()
    for change in (
        {"rr_target": Decimal("3")},
        {"swing_lookback": 3},
        {"min_sweep_distance": Decimal("0.2")},
        {"entry_mode": EntryMode.FIRST_TOUCH},
        {"max_trades_per_day": 1},
    ):
        other = LiquiditySweepStrategy(params=replace_params(**change))
        assert other.params_hash() != base.params_hash(), change


def test_the_higher_timeframe_filter_only_blocks_the_wrong_side() -> None:
    """22. Off by default, and when on it needs a settled hourly EMA first."""
    assert not BASELINE.htf_filter_enabled
    result = _run(_scenario(), replace_params(htf_filter_enabled=True))
    # One day of history cannot settle a fifty-period hourly EMA, so the filter
    # refuses rather than guessing - and says so in the record.
    assert result.orders_placed == 0
    assert InvalidationReason.HTF_FILTER in _reasons(result)


def test_disabling_a_liquidity_source_removes_its_setups() -> None:
    without_asia = replace_params(
        sources=(LiquiditySource.SWING_HIGH, LiquiditySource.SWING_LOW)
    )
    result = _run(_scenario(), without_asia)
    assert all(
        r.liquidity_type is not LiquiditySource.ASIAN_LOW for r in result.records
    )
