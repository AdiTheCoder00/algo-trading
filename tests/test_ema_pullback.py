"""The 4-EMA pullback strategy: rules, state machine, sizing and execution.

The rule tests are arithmetic - a handful of floats through a pure function -
because that is what `ema_pullback` is for. The execution tests build small
series where the right answer is known by construction, which is the only way
to assert "it entered at the *next* candle's open" rather than "it entered at a
price that looks plausible".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo.backtest.cfd_runner import CfdCosts
from algo.backtest.xauusd_runner import EntryIntent, run_scalper, size_for_risk
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Exchange, Side
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.exchange.specs import ContractSpecStore
from algo.pricing.indicators import ema
from algo.strategy.ema_pullback import (
    BASELINE,
    Emas,
    Phase,
    Setup,
    advance,
    holds_trend_ema,
    is_confirmation,
    is_pullback,
    trend_side,
)
from algo.strategy.rsi_stoch_reversal import ScalperParams

M5 = Timeframe(minutes=5)
H1 = Timeframe(minutes=60)
BASE = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)

FREE = CfdCosts(
    half_spread=Decimal("0"),
    swap=SwapModel(
        long_points=Decimal("0"), short_points=Decimal("0"), point_value=Decimal("0.01")
    ),
    commission=CfdChargeModel(),
)

#: Only the specified exits: the stop, and the four-hour cap.
EXECUTION = ScalperParams(
    rsi_reversal_exit=False,
    news_filter=False,
    daily_loss_limit=Decimal("-1000000"),
    max_hold=timedelta(hours=4),
    stop_atr_multiple=None,
)


def bar(
    index: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
) -> Bar:
    return Bar(
        ts=BASE + timedelta(minutes=5 * (index + 1)),
        timeframe=M5,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1,
    )


BULL = Emas(fast=2010.0, pullback=2008.0, trend=2005.0, major=2000.0)
BEAR = Emas(fast=1990.0, pullback=1992.0, trend=1995.0, major=2000.0)


# ------------------------------------------------------------------ indicators


def test_ema_is_the_recursive_form_seeded_on_the_first_value() -> None:
    """The convention `indicators.ema` documents, and MT5's own.

    Asserted here because the whole strategy is four of these: if the seeding
    changed, every trend, every pullback zone and every stop would move.
    """
    values = [10.0, 11.0, 12.0, 13.0]
    out = ema(values, 3)
    alpha = 2.0 / 4.0
    assert out[0] == 10.0
    for i in range(1, 4):
        assert out[i] == pytest.approx(alpha * values[i] + (1 - alpha) * out[i - 1])


def test_ema_warmup_is_a_settling_curve_not_a_cliff() -> None:
    """A recursive EMA seeded on its first value never becomes exact - its error
    decays geometrically. This measures the decay rather than asserting a
    threshold, because the threshold depends on how wrong the seed was.

    The specification asks only that EMA200 be "properly initialised", which has
    no exact meaning for a recursive average. `WARMUP = 1000` in the study script
    is justified by the numbers below: whatever error the seed carries is down by
    a factor of ~22,000 by then, so a seeding error the size of a plausible gold
    move - tens of dollars - is far below a $0.01 tick. At the 200 candles the
    specification's wording might suggest, it is only down by a factor of 7.
    """
    steady = [2000.0] * 1200
    disturbed = [1000.0, *steady[1:]]
    a = ema(steady, 200)
    b = ema(disturbed, 200)
    shock = abs(a[0] - b[0])

    at_200 = abs(a[200] - b[200]) / shock
    at_1000 = abs(a[1000] - b[1000]) / shock
    assert at_200 > 0.1, "at 200 candles most of the seeding error is still there"
    assert at_1000 < 1e-4, "by 1,000 candles it is four orders of magnitude down"

    # What that means for a realistic seed: the first close differs from the
    # settled average by tens of dollars, not by a thousand.
    assert 50.0 * at_1000 < 0.01, "a $50 seeding error is below a tick by then"


# ----------------------------------------------------------------- trend filter


def test_a_long_trend_needs_the_full_stack_and_price_above_the_200() -> None:
    assert trend_side(BULL, 2011.0) is Side.BUY
    assert trend_side(BULL, 1999.0) is None, "close below the 200 is not a long trend"
    assert trend_side(Emas(2008.0, 2010.0, 2005.0, 2000.0), 2011.0) is None
    assert trend_side(Emas(2010.0, 2008.0, 2005.0, 2011.0), 2012.0) is None


def test_a_short_trend_is_the_mirror() -> None:
    assert trend_side(BEAR, 1989.0) is Side.SELL
    assert trend_side(BEAR, 2001.0) is None
    assert trend_side(Emas(1992.0, 1990.0, 1995.0, 2000.0), 1989.0) is None


def test_equal_emas_are_not_a_trend() -> None:
    """Strict inequalities. Two averages at the same value is a flat market."""
    assert trend_side(Emas(2010.0, 2010.0, 2005.0, 2000.0), 2011.0) is None


def test_a_warming_up_ema_is_never_a_trend() -> None:
    nan = float("nan")
    assert trend_side(Emas(nan, 2008.0, 2005.0, 2000.0), 2011.0) is None


def test_direction_switches_disable_one_side_only() -> None:
    from dataclasses import replace

    long_only = replace(BASELINE, allow_short=False)
    assert trend_side(BEAR, 1989.0, long_only) is None
    assert trend_side(BULL, 2011.0, long_only) is Side.BUY


# -------------------------------------------------------------------- pullback


def test_a_long_pullback_is_a_candle_whose_range_reaches_the_zone() -> None:
    """Low at or below EMA9 and high at or above EMA20 - the zone is [2008, 2010]."""
    inside = bar(0, open_="2011", high="2012", low="2009", close="2011")
    assert is_pullback(inside, BULL, Side.BUY)

    above = bar(0, open_="2013", high="2014", low="2011", close="2013")
    assert not is_pullback(above, BULL, Side.BUY), "never reached down to EMA9"

    below = bar(0, open_="2006", high="2007", low="2005", close="2006")
    assert not is_pullback(below, BULL, Side.BUY), "high never reached EMA20"


def test_a_short_pullback_is_the_mirror() -> None:
    inside = bar(0, open_="1989", high="1991", low="1988", close="1989")
    assert is_pullback(inside, BEAR, Side.SELL)
    below = bar(0, open_="1987", high="1988", low="1986", close="1987")
    assert not is_pullback(below, BEAR, Side.SELL)


def test_the_50_ema_test_uses_the_close_not_the_wick() -> None:
    """"Decisively break" made objective: a candle may trade through the 50 and
    close back above it without invalidating the setup."""
    wick = bar(0, open_="2009", high="2010", low="2001", close="2009")
    assert holds_trend_ema(wick, BULL, Side.BUY)
    closed_through = bar(0, open_="2009", high="2010", low="2001", close="2003")
    assert not holds_trend_ema(closed_through, BULL, Side.BUY)


# ---------------------------------------------------------------- confirmation


def test_a_long_confirmation_is_bullish_and_closes_above_the_fast_ema() -> None:
    good = bar(0, open_="2009", high="2012", low="2008", close="2011")
    assert is_confirmation(good, BULL, Side.BUY)

    bearish = bar(0, open_="2012", high="2013", low="2008", close="2011")
    assert not is_confirmation(bearish, BULL, Side.BUY), "close below open"

    under = bar(0, open_="2008", high="2010", low="2007", close="2009")
    assert not is_confirmation(under, BULL, Side.BUY), "closed below EMA9"


def test_a_short_confirmation_is_the_mirror() -> None:
    good = bar(0, open_="1991", high="1992", low="1988", close="1989")
    assert is_confirmation(good, BEAR, Side.SELL)
    bullish = bar(0, open_="1988", high="1992", low="1987", close="1989")
    assert not is_confirmation(bullish, BEAR, Side.SELL)


# ------------------------------------------------------------ the state machine


def test_a_pullback_and_a_confirmation_produce_one_entry() -> None:
    setup = Setup(phase=Phase.NO_SETUP)
    pullback = bar(0, open_="2011", high="2012", low="2009", close="2011")
    step = advance(setup, pullback, BULL)
    assert step.setup.phase is Phase.PULLBACK
    assert step.entry is None, "the pullback candle alone is not an entry"

    confirm = bar(1, open_="2010", high="2013", low="2009.5", close="2012")
    step = advance(step.setup, confirm, BULL)
    assert step.entry is Side.BUY
    assert step.stop_price == Decimal("2009"), "the lowest low of the whole setup"


def test_one_pullback_produces_at_most_one_entry() -> None:
    """§10: a fresh pullback candle is required before another entry."""
    step = advance(
        Setup(phase=Phase.NO_SETUP),
        bar(0, open_="2011", high="2012", low="2009", close="2011"),
        BULL,
    )
    step = advance(step.setup, bar(1, open_="2010", high="2013", low="2010", close="2012"), BULL)
    assert step.entry is Side.BUY
    assert step.setup.phase is Phase.TREND

    # Another perfectly good confirmation candle, with no new pullback: no entry.
    again = advance(step.setup, bar(2, open_="2011", high="2014", low="2011", close="2013"), BULL)
    assert again.entry is None


def test_the_stop_is_the_lowest_low_across_the_whole_pullback() -> None:
    step = advance(
        Setup(phase=Phase.NO_SETUP),
        bar(0, open_="2011", high="2012", low="2009", close="2011"),
        BULL,
    )
    deeper = bar(1, open_="2009", high="2010", low="2006", close="2009")
    step = advance(step.setup, deeper, BULL)
    assert step.entry is None
    confirm = bar(2, open_="2009", high="2013", low="2009", close="2012")
    step = advance(step.setup, confirm, BULL)
    assert step.stop_price == Decimal("2006")


def test_a_setup_dies_when_the_trend_does() -> None:
    step = advance(
        Setup(phase=Phase.NO_SETUP),
        bar(0, open_="2011", high="2012", low="2009", close="2011"),
        BULL,
    )
    assert step.setup.phase is Phase.PULLBACK
    flat = Emas(2008.0, 2010.0, 2005.0, 2000.0)  # stack broken
    step = advance(step.setup, bar(1, open_="2010", high="2011", low="2009", close="2010"), flat)
    assert step.setup.phase is not Phase.PULLBACK
    assert step.invalidated == "trend no longer valid"


def test_a_setup_dies_when_the_50_ema_is_closed_through() -> None:
    step = advance(
        Setup(phase=Phase.NO_SETUP),
        bar(0, open_="2011", high="2012", low="2009", close="2011"),
        BULL,
    )
    broken = bar(1, open_="2009", high="2010", low="2001", close="2003")
    step = advance(step.setup, broken, BULL)
    assert step.setup.phase is not Phase.PULLBACK
    assert step.invalidated == "closed through the 50 EMA"


def test_a_setup_expires_when_it_goes_stale() -> None:
    from dataclasses import replace

    params = replace(BASELINE, stale_after=3)
    step = advance(
        Setup(phase=Phase.NO_SETUP),
        bar(0, open_="2011", high="2012", low="2009", close="2011"),
        BULL,
        params,
    )
    waiting = bar(1, open_="2010", high="2010.5", low="2009", close="2009.5")
    for _ in range(3):
        step = advance(step.setup, waiting, BULL, params)
        assert step.entry is None
    step = advance(step.setup, waiting, BULL, params)
    assert step.invalidated == "setup went stale"


def test_same_candle_confirmation_is_off_by_default_and_switchable() -> None:
    from dataclasses import replace

    both = bar(0, open_="2009", high="2012", low="2009", close="2011")
    assert is_pullback(both, BULL, Side.BUY)
    assert is_confirmation(both, BULL, Side.BUY)

    step = advance(Setup(phase=Phase.NO_SETUP), both, BULL)
    assert step.entry is None, "the default requires a later confirmation candle"

    allowed = replace(BASELINE, allow_same_candle_confirmation=True)
    step = advance(Setup(phase=Phase.NO_SETUP), both, BULL, allowed)
    assert step.entry is Side.BUY


# -------------------------------------------------------------------- sizing


def test_the_engine_spec_still_says_one_ounce_minimum() -> None:
    """The whole sizing story rests on this. If a spec update ever changes the
    minimum order, the risk arithmetic and its limitation change with it."""
    from datetime import date

    spec = ContractSpecStore.default().spec_for("XAUUSD", Exchange.OTC, date(2026, 1, 1))
    assert spec.lot_size == Decimal("1"), "one engine lot is one troy ounce"
    assert spec.multiplier == Decimal("1"), "$1 per $1 of gold, per ounce"
    assert spec.min_lots == 1


@pytest.mark.parametrize(
    ("entry", "stop", "expected_lots"),
    [
        ("2000", "1998", 5),  # $2 away, $10 budget -> exactly 5 oz
        ("2000", "1990", 1),  # $10 away -> exactly one ounce
        ("2000", "1996.70", 3),  # $3.30 -> 3.03 oz, rounded DOWN to 3
        ("2000", "1987", 0),  # $13 away -> below one ounce, refused
        ("2000", "2002", 5),  # a short: distance is what matters, not sign
    ],
)
def test_risk_sizing_rounds_down_and_refuses_what_it_cannot_afford(
    entry: str, stop: str, expected_lots: int
) -> None:
    lots, distance = size_for_risk(
        risk=Decimal("10"), entry_price=Decimal(entry), stop_price=Decimal(stop)
    )
    assert lots == expected_lots
    assert distance == abs(Decimal(entry) - Decimal(stop))
    if lots:
        assert lots * distance <= Decimal("10"), "never more than the budget"


def test_a_zero_width_stop_is_refused_rather_than_sized_infinitely() -> None:
    lots, _ = size_for_risk(
        risk=Decimal("10"), entry_price=Decimal("2000"), stop_price=Decimal("2000")
    )
    assert lots == 0


# ------------------------------------------------------------------- execution


def _rising(count: int, *, start: Decimal = Decimal("2000")) -> list[Bar]:
    """A steady climb, so the EMA stack orders itself and stays ordered."""
    bars: list[Bar] = []
    price = start
    for i in range(count):
        nxt = price + Decimal("0.05")
        bars.append(
            Bar(
                ts=BASE + timedelta(minutes=5 * (i + 1)),
                timeframe=M5,
                open=price,
                high=max(price, nxt) + Decimal("0.10"),
                low=min(price, nxt) - Decimal("0.10"),
                close=nxt,
                volume=1,
            )
        )
        price = nxt
    return bars


def test_entry_fills_at_the_next_candles_open_and_sizes_from_the_stop() -> None:
    m5 = _rising(300)
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(30)
    ]
    fired = {"at": 100}

    def entry(i: int):
        if i != fired["at"]:
            return None
        return EntryIntent(
            side=Side.BUY,
            stop_price=m5[i].close - Decimal("2"),
            risk=Decimal("10"),
        )

    result = run_scalper(
        m5, h1, params=EXECUTION, costs=FREE, calendar=None, entry=entry, warmup_bars=50
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == m5[fired["at"] + 1].open, "the NEXT candle's open"
    assert trade.entry_ts == m5[fired["at"]].ts
    assert trade.lots >= 1
    assert trade.lots * trade.stop_distance <= Decimal("10")


def test_a_stop_too_wide_for_the_budget_is_skipped_and_counted() -> None:
    m5 = _rising(300)
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(30)
    ]

    def entry(i: int):
        if i != 100:
            return None
        return EntryIntent(
            side=Side.BUY, stop_price=m5[i].close - Decimal("50"), risk=Decimal("10")
        )

    result = run_scalper(
        m5, h1, params=EXECUTION, costs=FREE, calendar=None, entry=entry, warmup_bars=50
    )
    assert not result.trades
    assert result.blocked_by_sizing == 1
    # The declined bar still records its equity point - no hole in the curve.
    assert len(result.equity_curve) == len(m5)


def test_the_four_hour_cap_is_measured_from_the_fill_and_never_exceeded() -> None:
    m5 = _rising(400)
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(40)
    ]

    def entry(i: int):
        if i != 100:
            return None
        # A stop far below anything this series reaches, so only the clock exits.
        return EntryIntent(
            side=Side.BUY, stop_price=m5[i].close - Decimal("9.5"), risk=Decimal("10")
        )

    result = run_scalper(
        m5, h1, params=EXECUTION, costs=FREE, calendar=None, entry=entry, warmup_bars=50
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason.value == "max hold"
    assert trade.holding_time == timedelta(hours=4), "exactly the cap, on a continuous grid"
    assert trade.exit_ts == trade.entry_ts + timedelta(hours=4)


def test_only_one_position_at_a_time() -> None:
    m5 = _rising(600)
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(60)
    ]

    def entry(i: int):
        # Ask for a position on every single bar.
        if i < 60:
            return None
        return EntryIntent(
            side=Side.BUY, stop_price=m5[i].close - Decimal("2"), risk=Decimal("10")
        )

    result = run_scalper(
        m5, h1, params=EXECUTION, costs=FREE, calendar=None, entry=entry, warmup_bars=50
    )
    assert result.trades
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_ts is not None
        assert earlier.exit_ts <= later.entry_ts
    assert result.blocked_while_in_position > 0


def test_a_stopped_trade_loses_about_the_risk_budget() -> None:
    """With no costs, a stop is the intended risk to the rounding, never worse
    unless the candle gapped through the level."""
    falling: list[Bar] = []
    price = Decimal("2000")
    for i in range(300):
        nxt = price - Decimal("0.20")
        falling.append(
            Bar(
                ts=BASE + timedelta(minutes=5 * (i + 1)),
                timeframe=M5,
                open=price,
                high=price + Decimal("0.05"),
                low=nxt - Decimal("0.05"),
                close=nxt,
                volume=1,
            )
        )
        price = nxt
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(30)
    ]

    def entry(i: int):
        if i != 100:
            return None
        return EntryIntent(
            side=Side.BUY, stop_price=falling[i].close - Decimal("2"), risk=Decimal("10")
        )

    result = run_scalper(
        falling,
        h1,
        params=EXECUTION,
        costs=FREE,
        calendar=None,
        entry=entry,
        warmup_bars=50,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason.value == "stop loss"
    assert trade.net_pnl <= Decimal("-9")
    assert trade.net_pnl >= Decimal("-11"), "one rounding step, not a different number"


def test_the_signals_do_not_move_when_only_the_risk_budget_changes() -> None:
    """§30: risk is a sizing input, not a signal input.

    Restricted to setups both budgets can afford, because a larger budget can
    take a wider stop - that is a difference in what is AFFORDABLE, not in what
    was signalled, and the study script says so in its own report.
    """
    import sys
    from dataclasses import replace
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backtest_xauusd_ema_pullback import build_signals

    m5 = _rising(2000)
    a, _ = build_signals(m5, BASELINE)
    b, _ = build_signals(m5, replace(BASELINE, risk_per_trade=Decimal("50")))
    assert [d.entry for d in a] == [d.entry for d in b]
    assert [d.stop_price for d in a] == [d.stop_price for d in b]


def test_the_strategy_produces_no_signal_before_its_emas_exist() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backtest_xauusd_ema_pullback import build_signals

    m5 = _rising(600)
    decisions, _ = build_signals(m5, BASELINE)
    for i in range(BASELINE.ema_major):
        assert decisions[i].entry is None, f"a signal at bar {i}, before EMA200 exists"


def test_signals_are_causal() -> None:
    """Truncating the series must not change a decision that survives - the
    property the precomputed signal array depends on."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backtest_xauusd_ema_pullback import build_signals

    m5 = _rising(1200)
    full, _ = build_signals(m5, BASELINE)
    partial, _ = build_signals(m5[:900], BASELINE)
    for i in range(900):
        assert full[i].entry == partial[i].entry
        assert full[i].stop_price == partial[i].stop_price


# --------------------------------------------------------------- the EMA exit


def test_the_baseline_has_no_ema_exit_and_keeps_its_stop() -> None:
    """§15: the EMA exit is a variant, not part of the hypothesis."""
    assert BASELINE.ema_exit is None
    assert BASELINE.use_stop is True


def test_the_ema_exit_uses_the_close_not_the_wick() -> None:
    from dataclasses import replace

    from algo.strategy.ema_pullback import ExitEma, ema_exit_hit

    fast = replace(BASELINE, ema_exit=ExitEma.FAST)
    # EMA9 is 2010. A long is closed when a candle CLOSES below it.
    assert ema_exit_hit(BULL, 2009.0, Side.BUY, fast)
    assert not ema_exit_hit(BULL, 2011.0, Side.BUY, fast)
    # The mirror for a short: EMA9 is 1990.
    assert ema_exit_hit(BEAR, 1991.0, Side.SELL, fast)
    assert not ema_exit_hit(BEAR, 1989.0, Side.SELL, fast)


def test_the_pullback_ema_exit_gives_more_room_than_the_fast_one() -> None:
    from dataclasses import replace

    from algo.strategy.ema_pullback import ExitEma, ema_exit_hit

    fast = replace(BASELINE, ema_exit=ExitEma.FAST)
    slow = replace(BASELINE, ema_exit=ExitEma.PULLBACK)
    # 2009 is below EMA9 (2010) but above EMA20 (2008).
    assert ema_exit_hit(BULL, 2009.0, Side.BUY, fast)
    assert not ema_exit_hit(BULL, 2009.0, Side.BUY, slow)


def test_the_cross_exit_ignores_price_and_watches_the_stack() -> None:
    from dataclasses import replace

    from algo.strategy.ema_pullback import ExitEma, ema_exit_hit

    cross = replace(BASELINE, ema_exit=ExitEma.CROSS)
    assert not ema_exit_hit(BULL, 1000.0, Side.BUY, cross), "price is not the test"
    unstacked = Emas(fast=2007.0, pullback=2008.0, trend=2005.0, major=2000.0)
    assert ema_exit_hit(unstacked, 2011.0, Side.BUY, cross)


def test_no_ema_exit_fires_while_the_emas_are_warming_up() -> None:
    from dataclasses import replace

    from algo.strategy.ema_pullback import ExitEma, ema_exit_hit

    nan = float("nan")
    for mode in (ExitEma.FAST, ExitEma.PULLBACK, ExitEma.CROSS):
        params = replace(BASELINE, ema_exit=mode)
        assert not ema_exit_hit(Emas(nan, nan, nan, nan), 2000.0, Side.BUY, params)


def test_the_exit_hook_closes_the_position_and_is_reported_as_a_signal_exit() -> None:
    m5 = _rising(300)
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(30)
    ]

    def entry(i: int):
        if i != 100:
            return None
        return EntryIntent(side=Side.BUY, lots=1)

    def should_exit(i: int, side) -> bool:
        return i >= 110

    result = run_scalper(
        m5,
        h1,
        params=EXECUTION,
        costs=FREE,
        calendar=None,
        entry=entry,
        exit_signal=should_exit,
        warmup_bars=50,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason.value == "signal exit"
    assert trade.exit_ts == m5[110].ts, "closed on the first candle that asked"


def test_the_stop_still_wins_a_candle_that_contains_both() -> None:
    """A signal exit is decided at the close; the stop can fire at any point
    inside the candle. The engine's pessimistic convention must survive the new
    hook, or a losing trade could be booked as a tidy signal exit."""
    falling: list[Bar] = []
    price = Decimal("2000")
    for i in range(300):
        nxt = price - Decimal("0.50")
        falling.append(
            Bar(
                ts=BASE + timedelta(minutes=5 * (i + 1)),
                timeframe=M5,
                open=price,
                high=price + Decimal("0.05"),
                low=nxt - Decimal("0.05"),
                close=nxt,
                volume=1,
            )
        )
        price = nxt
    h1 = [
        Bar(
            ts=BASE + timedelta(hours=i + 1),
            timeframe=H1,
            open=Decimal("2000"),
            high=Decimal("2001"),
            low=Decimal("1999"),
            close=Decimal("2000"),
            volume=1,
        )
        for i in range(30)
    ]

    def entry(i: int):
        if i != 100:
            return None
        # Close enough that the very first candle the position exists on
        # trades through it - so the stop and the signal are both true on the
        # same candle, which is the case being tested.
        return EntryIntent(
            side=Side.BUY, stop_price=falling[i].close - Decimal("0.2"), risk=Decimal("10")
        )

    result = run_scalper(
        falling,
        h1,
        params=EXECUTION,
        costs=FREE,
        calendar=None,
        entry=entry,
        exit_signal=lambda i, side: True,  # asks to exit on every candle
        warmup_bars=50,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_ts == falling[100].ts
    # The entry candle's own low is more than the stop distance below entry, so
    # the stop was reachable on the same candle the signal fired on.
    assert falling[101].low < trade.entry_price - trade.stop_distance
    assert trade.exit_reason.value == "stop loss"


def test_turning_the_stop_off_trades_a_flat_size_and_has_no_stop_exit() -> None:
    """The property that makes the no-stop rows readable: one ounce every time,
    so the comparison is about the rule and not about the size."""
    import sys
    from dataclasses import replace
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backtest_xauusd_ema_pullback import entry_hook

    from algo.strategy.ema_pullback import Decision, Phase, Setup

    params = replace(BASELINE, use_stop=False)
    decisions = [
        Decision(
            setup=Setup(phase=Phase.TREND, side=Side.BUY),
            entry=Side.BUY,
            stop_price=Decimal("1990"),
        )
    ]
    intent = entry_hook(decisions, params)(0)
    assert intent is not None
    assert intent.stop_price is None, "no stop means no stop price travels with the order"
    assert intent.risk is None, "and nothing to size against"
    assert intent.lots == params.fixed_lots == 1
