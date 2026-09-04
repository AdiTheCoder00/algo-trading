"""The XAUUSD scalper: the rules, the runner's execution, and the news filter.

The rule tests are deliberately arithmetic - a handful of floats through a pure
function - because that is what `rsi_stoch_reversal` is for. The runner tests
build small synthetic series where the correct answer is known by construction,
which is the only way to assert "it entered at the *next* bar's open" rather
than "it entered at a price that looks plausible".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from algo.backtest.cfd_runner import CfdCosts
from algo.backtest.xauusd_runner import H1_WARMUP, M5_WARMUP, run_scalper
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.errors import DataError
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.data.dukascopy import _bucket_close, bars_from_ticks, decode_path, regrid_bars
from algo.data.econ_calendar import EconomicCalendar, EconomicEvent
from algo.pricing.indicators import rsi, stoch_rsi
from algo.strategy.rsi_stoch_reversal import (
    BASELINE,
    ExitState,
    ScalperParams,
    Trend,
    entry_side,
    rsi_reversal_exit,
    stop_level,
    trend_of,
)

M5 = Timeframe(minutes=5)
H1 = Timeframe(minutes=60)
FREE = CfdCosts(
    half_spread=Decimal("0"),
    swap=SwapModel(
        long_points=Decimal("0"), short_points=Decimal("0"), point_value=Decimal("0.01")
    ),
    commission=CfdChargeModel(),
)


# --------------------------------------------------------------------- indicators


def test_rsi_matches_wilders_worked_example() -> None:
    """The canonical series from Wilder's own book, as every platform seeds it.

    Values from the 14-period example that ships with practically every RSI
    implementation; the first computed point is the SMA seed, which is the half
    that distinguishes TradingView's `ta.rsi` from a plain recursive EMA.
    """
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
        46.03, 46.41, 46.22, 45.64,
    ]
    values = rsi(closes, 14)
    assert values[13] != values[13], "no RSI until 14 changes have been seen"
    # The published table reads 70.53, 66.32 and 57.97; it rounds the closes it
    # prints, so the tolerance is the rounding, not slack in the implementation.
    assert values[14] == pytest.approx(70.53, abs=0.1)
    assert values[15] == pytest.approx(66.32, abs=0.1)
    assert values[19] == pytest.approx(57.97, abs=0.1)


def test_rsi_has_no_value_before_it_has_a_window() -> None:
    values = rsi([1.0, 2.0, 3.0], 14)
    assert all(v != v for v in values), "a warmup RSI must be NaN, never a number"


def test_rsi_is_causal() -> None:
    """Truncating the series must not change any value that survives.

    This is the property the runner relies on when it precomputes indicators
    over the whole history: if `rsi(x[:n])[i] == rsi(x)[i]`, there is no path
    by which a later bar reaches an earlier value.
    """
    closes = [100 + (i * 7 % 13) - 6 for i in range(200)]
    full = rsi(closes, 14)
    partial = rsi(closes[:150], 14)
    for i in range(150):
        assert (full[i] != full[i]) == (partial[i] != partial[i])
        if full[i] == full[i]:
            assert full[i] == pytest.approx(partial[i])


def test_stoch_rsi_is_bounded_and_flat_windows_are_the_midpoint() -> None:
    flat = [2000.0] * 60
    result = stoch_rsi(flat)
    settled = [v for v in result.k if v == v]
    assert settled, "a flat series still has a stochastic once warmed up"
    assert all(v == 50.0 for v in settled), "a zero-width range is the midpoint, not 0 or 100"

    closes = [2000 + (i % 17) * 0.4 for i in range(300)]
    result = stoch_rsi(closes)
    for series in (result.k, result.d):
        for value in series:
            if value == value:
                assert 0.0 <= value <= 100.0


def test_stoch_rsi_smooths_twice() -> None:
    """%D is the average of %K, not of the raw stochastic - the common slip."""
    closes = [2000 + (i * 3 % 29) * 0.7 for i in range(200)]
    result = stoch_rsi(closes)
    for i in range(60, 200):
        window = result.k[i - 2 : i + 1]
        assert result.d[i] == pytest.approx(sum(window) / 3)


# -------------------------------------------------------------------- trend filter


@pytest.mark.parametrize(
    ("fast", "slow", "line", "signal", "expected"),
    [
        (2010.0, 2000.0, 1.5, 1.0, Trend.BULL),
        (1990.0, 2000.0, -1.5, -1.0, Trend.BEAR),
        (2010.0, 2000.0, -1.5, -1.0, Trend.NONE),  # EMAs agree, MACD does not
        (1990.0, 2000.0, 1.5, 1.0, Trend.NONE),
        (2000.0, 2000.0, 1.5, 1.0, Trend.NONE),  # equal EMAs are not a trend
    ],
)
def test_trend_needs_both_conditions(fast, slow, line, signal, expected) -> None:
    assert trend_of(ema_fast=fast, ema_slow=slow, macd_line=line, macd_signal=signal) is expected


# --------------------------------------------------------------------- entry rules


def test_buy_needs_the_cross_the_trend_and_both_stochastic_lines() -> None:
    ok = {"trend": Trend.BULL, "previous_rsi": 58.0, "rsi": 61.0, "stoch_k": 70.0,
          "stoch_d": 65.0}
    assert entry_side(**ok) is Side.BUY

    assert entry_side(**{**ok, "trend": Trend.BEAR}) is None
    assert entry_side(**{**ok, "trend": Trend.NONE}) is None
    assert entry_side(**{**ok, "previous_rsi": 61.0}) is None, "already above 60 is not a cross"
    assert entry_side(**{**ok, "rsi": 60.0}) is None, "60 is not above 60"
    assert entry_side(**{**ok, "stoch_k": 59.0}) is None
    assert entry_side(**{**ok, "stoch_d": 59.0}) is None
    assert entry_side(**{**ok, "previous_rsi": 60.0}) is Side.BUY, "60 satisfies <= 60"


def test_sell_mirrors_the_buy_rule() -> None:
    ok = {"trend": Trend.BEAR, "previous_rsi": 42.0, "rsi": 39.0, "stoch_k": 30.0,
          "stoch_d": 35.0}
    assert entry_side(**ok) is Side.SELL
    assert entry_side(**{**ok, "trend": Trend.BULL}) is None
    assert entry_side(**{**ok, "previous_rsi": 39.5}) is None
    assert entry_side(**{**ok, "rsi": 40.0}) is None, "40 is not below 40"
    assert entry_side(**{**ok, "stoch_k": 41.0}) is None


def test_a_nan_is_never_a_signal() -> None:
    nan = float("nan")
    ok = {"trend": Trend.BULL, "previous_rsi": 58.0, "rsi": 61.0, "stoch_k": 70.0, "stoch_d": 70.0}
    assert entry_side(**{**ok, "previous_rsi": nan}) is None
    assert entry_side(**{**ok, "stoch_k": nan}) is None


def test_direction_switches_disable_one_side_only() -> None:
    from dataclasses import replace

    buy_only = replace(BASELINE, allow_sell=False)
    assert entry_side(
        trend=Trend.BEAR, previous_rsi=42.0, rsi=39.0, stoch_k=30.0, stoch_d=35.0, params=buy_only
    ) is None
    assert entry_side(
        trend=Trend.BULL, previous_rsi=58.0, rsi=61.0, stoch_k=70.0, stoch_d=70.0, params=buy_only
    ) is Side.BUY


# ------------------------------------------------------- the RSI reversal state


def test_rsi_above_75_is_not_by_itself_an_exit() -> None:
    """§7's whole point. Crossing *up* through 75 must hold, not close."""
    state = ExitState(side=Side.BUY)
    state.observe(78.0)
    assert state.extreme_activated
    assert not rsi_reversal_exit(state, previous_rsi=70.0, rsi=78.0)
    assert not rsi_reversal_exit(state, previous_rsi=78.0, rsi=80.0)


def test_rsi_reversal_fires_on_the_way_back_down() -> None:
    state = ExitState(side=Side.BUY)
    state.observe(76.0)
    assert rsi_reversal_exit(state, previous_rsi=76.0, rsi=74.0)


def test_rsi_reversal_needs_the_activation_first() -> None:
    """A trade whose RSI never reached 75 is never eligible, even at 74.9."""
    state = ExitState(side=Side.BUY)
    state.observe(70.0)
    assert not state.extreme_activated
    assert not rsi_reversal_exit(state, previous_rsi=74.9, rsi=60.0)


def test_sell_reversal_is_the_mirror() -> None:
    state = ExitState(side=Side.SELL)
    state.observe(22.0)
    assert state.extreme_activated
    assert not rsi_reversal_exit(state, previous_rsi=22.0, rsi=20.0)
    assert rsi_reversal_exit(state, previous_rsi=24.0, rsi=26.0)


def test_touching_the_extreme_exactly_arms_it() -> None:
    state = ExitState(side=Side.BUY)
    state.observe(75.0)
    assert state.extreme_activated


def test_the_reversal_exit_can_be_switched_off() -> None:
    from dataclasses import replace

    off = replace(BASELINE, rsi_reversal_exit=False)
    state = ExitState(side=Side.BUY)
    state.observe(80.0, off)
    assert not rsi_reversal_exit(state, previous_rsi=80.0, rsi=70.0, params=off)


def test_stop_level_is_ten_dollars_away_at_one_ounce() -> None:
    assert stop_level(Side.BUY, Decimal("2000")) == Decimal("1990")
    assert stop_level(Side.SELL, Decimal("2000")) == Decimal("2010")


# ------------------------------------------------------------- the news calendar


def _calendar() -> EconomicCalendar:
    return EconomicCalendar(
        [
            EconomicEvent(
                ts=datetime(2024, 3, 12, 12, 30, tzinfo=UTC),
                name="Consumer Price Index",
                category="cpi",
                impact="high",
                source="test",
            )
        ]
    )


@pytest.mark.parametrize(
    ("when", "blocked"),
    [
        (datetime(2024, 3, 12, 11, 59, tzinfo=UTC), False),
        (datetime(2024, 3, 12, 12, 0, tzinfo=UTC), True),  # exactly 30 minutes before
        (datetime(2024, 3, 12, 12, 30, tzinfo=UTC), True),
        (datetime(2024, 3, 12, 12, 45, tzinfo=UTC), True),  # exactly 15 after
        (datetime(2024, 3, 12, 12, 46, tzinfo=UTC), False),
    ],
)
def test_the_news_window_is_thirty_before_and_fifteen_after(when, blocked) -> None:
    assert _calendar().blocks(when) is blocked


def test_the_shipped_calendar_loads_and_is_sourced() -> None:
    calendar = EconomicCalendar.load()
    assert len(calendar) > 100
    categories = {event.category for event in calendar.events}
    assert {"nfp", "cpi", "fomc", "fomc_press"} <= categories
    assert all(event.source for event in calendar.events), "every row states where it came from"
    # The March 2024 CPI print, 08:30 ET on a date inside daylight saving.
    assert calendar.blocks(datetime(2024, 3, 12, 12, 30, tzinfo=UTC))
    # ... and 08:30 ET in January is an hour later in UTC. If the loader had
    # assumed a fixed offset, one of these two would be wrong.
    assert calendar.blocks(datetime(2024, 1, 11, 13, 30, tzinfo=UTC))


def test_running_with_the_filter_on_and_no_calendar_is_refused() -> None:
    """Silently reporting "0 blocked by news" with no calendar is the failure
    this guards - it reads as "the filter found nothing", not "there was no filter"."""
    bars = _series(count=10, start=Decimal("2000"))
    with pytest.raises(DataError, match="no economic calendar"):
        run_scalper(bars, bars, calendar=None)


# ------------------------------------------------------------------- the runner


def _series(
    *, count: int, start: Decimal, step: Decimal = Decimal("0"), tf: Timeframe = M5
) -> list[Bar]:
    """A flat or linearly drifting series, so the arithmetic is knowable."""
    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    bars = []
    price = start
    for i in range(count):
        nxt = price + step
        bars.append(
            Bar(
                ts=base + timedelta(minutes=tf.minutes * (i + 1)),
                timeframe=tf,
                open=price,
                high=max(price, nxt),
                low=min(price, nxt),
                close=nxt,
                volume=1,
            )
        )
        price = nxt
    return bars


def _forced_run(**kwargs):
    """A run over a deterministic random walk long enough to clear both warmups.

    A shaped series does not work here, and the reason is itself a check on the
    rules: on a repeating saw-tooth, RSI crosses up through 60 exactly when it
    is at the bottom of its own recent range, so the stochastic confirmation is
    never satisfied and the strategy takes no trades at all. It needs a series
    with genuine variety of shape, so this is a linear congruential walk - fixed
    seed, so the fixture is the same on every run and on every machine.
    """
    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    seed = 12345

    def draw() -> Decimal:
        nonlocal seed
        seed = (1103515245 * seed + 12345) % 2147483648
        return Decimal(seed % 401 - 195) / Decimal(100)

    m5: list[Bar] = []
    price = Decimal("2000")
    for i in range(M5_WARMUP + H1_WARMUP * 12 + 3000):
        nxt = price + draw()
        m5.append(
            Bar(
                ts=base + timedelta(minutes=5 * (i + 1)),
                timeframe=M5,
                open=price,
                high=max(price, nxt) + Decimal("0.3"),
                low=min(price, nxt) - Decimal("0.3"),
                close=nxt,
                volume=1,
            )
        )
        price = nxt
    h1 = regrid_bars(m5, H1)
    return m5, h1, run_scalper(m5, h1, costs=FREE, calendar=_calendar(), **kwargs)


def test_entry_fills_at_the_next_bars_open_never_the_signal_bars_close() -> None:
    m5, _h1, result = _forced_run()
    assert result.trades, "the fixture must produce trades or it tests nothing"
    by_ts = {bar.ts: bar for bar in m5}
    step = timedelta(minutes=5)
    for trade in result.trades:
        following = by_ts[trade.signal_ts + step]
        signal_bar = by_ts[trade.signal_ts]
        assert trade.entry_mid == following.open
        assert trade.entry_mid != signal_bar.close or following.open == signal_bar.close


def test_only_one_position_is_ever_open() -> None:
    _m5, _h1, result = _forced_run()
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_ts is not None
        assert earlier.exit_ts <= later.entry_ts


def test_no_trade_is_held_past_the_maximum_when_the_market_is_open() -> None:
    """The fixture has no weekend gap, so every hold is exactly bounded.

    Against real data a hold can exceed four hours - the market closes and there
    is no executable price - which is why this asserts against a continuous
    series rather than against the archive.
    """
    _m5, _h1, result = _forced_run()
    for trade in result.trades:
        assert trade.holding_time <= BASELINE.max_hold


def test_a_stopped_trade_loses_exactly_the_stop_before_other_costs() -> None:
    """With no spread, no commission and no swap, a stop is -$10 to the cent
    unless the bar gapped through it."""
    _m5, _h1, result = _forced_run()
    stopped = [t for t in result.trades if t.exit_reason.value == "stop loss"]
    for trade in stopped:
        move = trade.exit_price - trade.entry_price
        signed = move if trade.side is Side.BUY else -move
        assert signed <= Decimal("-10"), "a stop can fill worse on a gap, never better"


def test_costs_reconcile_with_the_executed_prices() -> None:
    """net = gross - costs, and net also = the executed round trip less
    commission and swap. Both must hold, or the spread is counted twice."""
    m5 = _series(count=800, start=Decimal("2000"), step=Decimal("0.05"))
    h1 = regrid_bars(m5, H1)
    costs = CfdCosts(half_spread=Decimal("0.2"))
    result = run_scalper(m5, h1, costs=costs, calendar=_calendar())
    for trade in result.trades:
        move = trade.exit_price - trade.entry_price
        executed = (move if trade.side is Side.BUY else -move) * trade.lots
        assert trade.net_pnl == executed - trade.commission_paid - trade.swap_paid


def test_the_daily_limit_latches_for_the_rest_of_the_day() -> None:
    from dataclasses import replace

    tight = replace(BASELINE, daily_loss_limit=Decimal("-1"))
    _m5, _h1, result = _forced_run(params=tight)
    assert result.limit_days, "a $1 daily limit must be reached by a $10 stop"
    assert result.blocked_by_daily_limit > 0
    for day, total in result.limit_days.items():
        after = [t for t in result.trades if t.entry_ts.date() == day]
        # Nothing may open on a latched day once the limit is reached.
        breached = [t for t in after if t.daily_realised_before <= tight.daily_loss_limit]
        assert not breached, f"{len(breached)} entries on {day} after {total}"


def test_indicators_are_not_recomputed_into_a_different_answer() -> None:
    """Passing precomputed indicators must give the identical run - that is the
    only thing that makes the variant sweep's reuse safe."""
    from algo.backtest.xauusd_runner import compute_indicators

    m5 = _series(count=900, start=Decimal("2000"), step=Decimal("0.05"))
    h1 = regrid_bars(m5, H1)
    shared = compute_indicators(m5, h1)
    a = run_scalper(m5, h1, costs=FREE, calendar=_calendar())
    b = run_scalper(m5, h1, costs=FREE, calendar=_calendar(), indicators=shared)
    assert [(t.entry_ts, t.exit_ts, t.net_pnl) for t in a.trades] == [
        (t.entry_ts, t.exit_ts, t.net_pnl) for t in b.trades
    ]


# ------------------------------------------------------------------ the tick data


def test_dukascopy_paths_are_zero_indexed_by_month() -> None:
    assert decode_path(Path("XAUUSD/2024/00/02/13h_ticks.bi5")) == datetime(
        2024, 1, 2, 13, tzinfo=UTC
    )
    assert decode_path(Path("XAUUSD/2024/11/31/23h_ticks.bi5")) == datetime(
        2024, 12, 31, 23, tzinfo=UTC
    )


def test_a_tick_on_the_boundary_closes_the_bar_it_ends() -> None:
    """`(open, close]` - a tick at 10:05:00.000 belongs to the bar stamped
    10:05, not to the one stamped 10:10."""
    assert _bucket_close(datetime(2024, 1, 2, 10, 5, tzinfo=UTC), timedelta(minutes=5)) == datetime(
        2024, 1, 2, 10, 5, tzinfo=UTC
    )
    assert _bucket_close(
        datetime(2024, 1, 2, 10, 5, 0, 1000, tzinfo=UTC), timedelta(minutes=5)
    ) == datetime(2024, 1, 2, 10, 10, tzinfo=UTC)


def test_regridding_preserves_the_extremes() -> None:
    m5 = _series(count=240, start=Decimal("2000"), step=Decimal("0.3"))
    h1 = regrid_bars(m5, H1)
    assert len(h1) == 20
    assert h1[0].open == m5[0].open
    assert h1[0].close == m5[11].close
    assert h1[0].high == max(b.high for b in m5[:12])
    assert h1[0].low == min(b.low for b in m5[:12])


def test_bars_from_ticks_and_regridding_agree() -> None:
    from algo.data.dukascopy import Tick

    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    ticks = [
        Tick(
            ts=base + timedelta(seconds=30 * i),
            bid=Decimal(2000 + (i % 11)),
            ask=Decimal(2000 + (i % 11)) + Decimal("0.4"),
        )
        for i in range(1, 2000)
    ]
    direct = bars_from_ticks(ticks, H1)
    stepped = regrid_bars(bars_from_ticks(ticks, M5), H1)
    assert [(b.ts, b.open, b.high, b.low, b.close) for b in direct] == [
        (b.ts, b.open, b.high, b.low, b.close) for b in stepped
    ]


# ------------------------------------------------------------------- the report


def test_the_exported_trade_agrees_with_the_runners_own_record() -> None:
    """`to_trades` must not change any P&L on the way out.

    The exported record puts the spread and slippage in the executed prices and
    keeps commission and swap as `Charges`; the runner's record keeps a mid
    gross and charges everything separately. Two bases, one net - and if they
    ever disagreed, the tearsheet and the run report would print different
    numbers for the same trade.
    """
    from algo.reporting.scalper_report import to_trades

    _m5, _h1, result = _forced_run()
    exported = to_trades(result, BASELINE)
    assert len(exported) == len(result.trades)
    for original, record in zip(result.trades, exported, strict=True):
        assert record.net_pnl == original.net_pnl
        assert record.opened_at == original.entry_ts
        assert record.closed_at == original.exit_ts
        assert record.exit_reason == original.exit_reason.value


def test_the_summary_totals_reconcile() -> None:
    from algo.reporting.scalper_report import summarise

    _m5, _h1, result = _forced_run()
    summary = summarise(result, BASELINE, label="test")
    costs = (
        summary.spread_cost
        + summary.slippage_cost
        + summary.commission_cost
        + summary.swap_cost
    )
    assert summary.net_pnl == summary.gross_pnl_before_costs - costs
    assert summary.gross_profit - summary.gross_loss == summary.net_pnl
    assert summary.wins + summary.losses + summary.scratches == summary.trades
    assert sum(summary.exits.values()) == summary.trades


def test_the_extended_log_carries_every_field_the_brief_asks_for() -> None:
    import csv
    import io

    from algo.reporting.export import TRADE_COLUMNS, write_extended_trade_log
    from algo.reporting.scalper_report import CONTEXT_COLUMNS, to_trades

    _m5, _h1, result = _forced_run()
    exported = to_trades(result, BASELINE)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = write_extended_trade_log(
            exported, Path(tmp) / "log.csv", CONTEXT_COLUMNS
        )
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))

    assert len(rows) == len(exported)
    for name in ("m5_rsi", "stoch_k", "stoch_d", "h1_ema20", "h1_ema50", "h1_macd",
                 "mae", "mfe", "news_status", "daily_realised_before", "holding"):
        assert name in rows[0], f"{name} is missing from the trade log"
    # The standard columns are untouched - the golden log's shape is not widened.
    assert all(column in rows[0] for column in TRADE_COLUMNS)


def test_the_session_bands_cover_every_hour_exactly_once() -> None:
    from algo.reporting.scalper_report import SESSIONS, session_of

    seen = [session_of(datetime(2024, 1, 2, hour, tzinfo=UTC)) for hour in range(24)]
    assert len(set(seen)) == len(SESSIONS)
    assert all(name in {s[0] for s in SESSIONS} for name in seen)


# ---------------------------------------------------------- the volatility stop


def test_atr_seeds_on_a_simple_average_then_smooths() -> None:
    from algo.pricing.indicators import atr, true_range

    highs = [10.0 + i * 0.5 for i in range(30)]
    lows = [h - 2.0 for h in highs]
    closes = [h - 0.5 for h in highs]

    ranges = true_range(highs, lows, closes)
    assert ranges[0] == pytest.approx(2.0), "the first bar has no gap to measure"

    values = atr(highs, lows, closes, 14)
    assert all(v != v for v in values[:13]), "no ATR before 14 bars"
    assert values[13] == pytest.approx(sum(ranges[:14]) / 14)
    for i in range(14, 30):
        assert values[i] == pytest.approx((values[i - 1] * 13 + ranges[i]) / 14)


def test_true_range_uses_the_gap_when_it_is_wider_than_the_bar() -> None:
    from algo.pricing.indicators import true_range

    # A bar that opens far below the previous close: its own span is 1, but the
    # distance from the last close (99.5) down to its low (89) is 10.5, and that
    # is the range actually travelled.
    ranges = true_range([100.0, 90.0], [99.0, 89.0], [99.5, 89.5])
    assert ranges[1] == pytest.approx(10.5)


def test_an_atr_stop_scales_with_volatility_and_a_dollar_stop_does_not() -> None:
    """The finding the stop study rests on, as an assertion.

    The same rules over the same shape at two different volatilities: a fixed
    dollar stop fires far more often in the noisier one, an ATR stop does not.
    """
    from dataclasses import replace

    from algo.reporting.scalper_report import summarise

    def run_at(scale: Decimal, params: ScalperParams):
        base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        seed = 12345

        def draw() -> Decimal:
            nonlocal seed
            seed = (1103515245 * seed + 12345) % 2147483648
            return Decimal(seed % 401 - 195) / Decimal(100) * scale

        m5: list[Bar] = []
        price = Decimal("2000")
        for i in range(M5_WARMUP + H1_WARMUP * 12 + 3000):
            nxt = price + draw()
            m5.append(
                Bar(
                    ts=base + timedelta(minutes=5 * (i + 1)),
                    timeframe=M5,
                    open=price,
                    high=max(price, nxt) + Decimal("0.3") * scale,
                    low=min(price, nxt) - Decimal("0.3") * scale,
                    close=nxt,
                    volume=1,
                )
            )
            price = nxt
        h1 = regrid_bars(m5, H1)
        result = run_scalper(m5, h1, params=params, costs=FREE, calendar=_calendar())
        return summarise(result, params, label="t")

    def stop_share(summary) -> float:
        share = summary.exit_share.get("stop loss")
        return float(share) if share is not None else 0.0

    quiet_dollar = stop_share(run_at(Decimal("1"), BASELINE))
    loud_dollar = stop_share(run_at(Decimal("3"), BASELINE))
    assert loud_dollar - quiet_dollar > 15, (
        "a fixed dollar stop must fire far more often when the range triples - "
        f"got {quiet_dollar:.1f}% and {loud_dollar:.1f}%"
    )

    scaled = replace(BASELINE, stop_atr_multiple=Decimal("6"))
    quiet_atr = stop_share(run_at(Decimal("1"), scaled))
    loud_atr = stop_share(run_at(Decimal("3"), scaled))
    assert abs(loud_atr - quiet_atr) < 10, (
        "an ATR stop must mean roughly the same thing at both volatilities - "
        f"got {quiet_atr:.1f}% and {loud_atr:.1f}%"
    )


def test_the_atr_stop_falls_back_rather_than_leaving_a_position_unbounded() -> None:
    """A missing volatility estimate must not silently become "no stop"."""
    from dataclasses import replace

    from algo.strategy.rsi_stoch_reversal import stop_distance

    scaled = replace(BASELINE, stop_atr_multiple=Decimal("6"))
    assert stop_distance(scaled, atr_at_entry=2.0) == Decimal("12")
    assert stop_distance(scaled, atr_at_entry=float("nan")) == BASELINE.stop_loss
    assert stop_distance(scaled, atr_at_entry=None) == BASELINE.stop_loss
    assert stop_distance(scaled, atr_at_entry=0.0) == BASELINE.stop_loss


def test_the_baseline_still_uses_a_flat_ten_dollar_stop() -> None:
    """The variant must not have moved the baseline. Cheap, and the whole point."""
    assert BASELINE.stop_atr_multiple is None
    assert BASELINE.stop_loss == Decimal("10")
    assert stop_level(Side.BUY, Decimal("2000"), BASELINE) == Decimal("1990")
