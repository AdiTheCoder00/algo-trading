"""TrendlineBreakout: the Donchian channel, and the position discipline around it.

Unlike `MacdCrossover` there is no numeric-matching concern here — a rolling
max/min has one unambiguous value, not a family of pandas-compatible
conventions — so these tests are entirely about the strategy's own logic: the
channel excludes the bar being tested, position comes from the context, and a
breakout in the held direction is not a second entry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.enums import Exchange, Side, SignalAction
from algo.core.errors import DomainError
from algo.core.instrument import CfdId
from algo.core.position import Position
from algo.exchange.specs import ContractSpecStore
from algo.strategy.context import BarContext, PositionView, SessionInfo
from algo.strategy.trendline_breakout import TrendlineBreakout

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=30)
START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)


def _bar(index: int, *, high: str, low: str, close: str) -> Bar:
    return Bar(
        ts=START + timedelta(minutes=30 * index),
        timeframe=TF,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=100,
    )


def _flat_bar(index: int, price: str) -> Bar:
    return _bar(index, high=price, low=price, close=price)


def _ctx(bar: Bar, window: BarWindow, *, held: Position | None = None) -> BarContext:
    positions = {} if held is None else {held.instrument.key: held}
    return BarContext(
        window=window,
        session=SessionInfo(
            session_date=bar.ts.date(),
            is_us_dst=False,
            minutes_to_close=600,
            is_partial_bar=False,
            bar_index=0,
            bars_in_session=48,
        ),
        specs=ContractSpecStore.default(),
        positions=PositionView(positions),
        timeframe=TF,
        exchange=Exchange.OTC,
    )


def _long(lots: int = 100) -> Position:
    return Position(
        instrument=XAUUSD, lots=lots, qty=Decimal(lots), cost_basis=Decimal(lots) * 4400
    )


def _short(lots: int = 100) -> Position:
    return Position(
        instrument=XAUUSD, lots=-lots, qty=Decimal(-lots), cost_basis=Decimal(lots) * 4400
    )


def _feed(strategy: TrendlineBreakout, bars: list[Bar], *, held: Position | None = None) -> list:
    """Run every bar through the strategy in order, growing the window as a
    real engine would, returning every batch of signals."""
    signals = []
    for i in range(len(bars)):
        window = BarWindow.of(tuple(bars[: i + 1]))
        signals.append(strategy.on_bar(_ctx(bars[i], window, held=held)))
    return signals


def _flat_then_breakout(lookback: int, *, direction: str) -> list[Bar]:
    """`lookback` flat bars at 4400, then one bar that clears the channel."""
    bars = [_flat_bar(i, "4400.00") for i in range(lookback)]
    if direction == "up":
        bars.append(_bar(lookback, high="4420.00", low="4400.00", close="4420.00"))
    else:
        bars.append(_bar(lookback, high="4400.00", low="4380.00", close="4380.00"))
    return bars


class TestConstruction:
    def test_lookback_below_two_is_refused(self) -> None:
        with pytest.raises(DomainError, match="at least 2"):
            TrendlineBreakout(instrument=XAUUSD, lookback=1)

    def test_default_lookback_is_20(self) -> None:
        assert TrendlineBreakout(instrument=XAUUSD).params()["lookback"] == "20"

    def test_warmup_is_lookback_plus_one(self) -> None:
        """One extra bar: the channel needs `lookback` PRIOR bars plus the one
        being tested against it."""
        assert TrendlineBreakout(instrument=XAUUSD, lookback=20).warmup_bars() == 21


class TestNothingHappensDuringWarmup:
    def test_no_signal_before_enough_bars(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        bars = [_flat_bar(i, "4400.00") for i in range(5)]  # warmup needs 6

        signals = _feed(strategy, bars)

        assert all(s == [] for s in signals)

    def test_a_note_is_left_explaining_why(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        _feed(strategy, [_flat_bar(i, "4400.00") for i in range(3)])

        notes = strategy.drain_notes()

        assert any("no entry" in n and "5-bar channel" in n for n in notes)


class TestTheChannelExcludesTheCurrentBar:
    """A breakout is checked against the *prior* lookback bars. Including
    today's own high/low in the channel would make a breakout impossible by
    construction."""

    def test_a_bar_that_only_matches_its_own_high_does_not_break_out(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        bars = [_flat_bar(i, "4400.00") for i in range(5)]
        # This bar's own high is 4420, but its close equals the prior channel's
        # ceiling exactly - not above it.
        bars.append(_bar(5, high="4420.00", low="4400.00", close="4400.00"))

        signals = _feed(strategy, bars)

        assert signals[-1] == []

    def test_exceeding_the_prior_channel_does_break_out(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        bars = _flat_then_breakout(5, direction="up")

        signals = _feed(strategy, bars)

        assert signals[-1] != []
        assert signals[-1][0].action is SignalAction.OPEN


class TestItEntersOnABreakout:
    def test_an_upside_breakout_opens_long(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        signals = _feed(strategy, _flat_then_breakout(10, direction="up"))

        opens = [s for batch in signals for s in batch if s.action is SignalAction.OPEN]
        assert opens
        assert opens[0].legs[0].direction is Side.BUY

    def test_a_downside_breakout_opens_short(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        signals = _feed(strategy, _flat_then_breakout(10, direction="down"))

        opens = [s for batch in signals for s in batch if s.action is SignalAction.OPEN]
        assert opens
        assert opens[0].legs[0].direction is Side.SELL

    def test_staying_inside_the_channel_emits_nothing(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        bars = [_flat_bar(i, "4400.00") for i in range(30)]

        signals = _feed(strategy, bars)

        assert all(s == [] for s in signals)


class TestPositionIsReadFromContextNeverFromMemory:
    """D-041, same rule as `MacdCrossover` and `CoinFlip`."""

    def test_an_already_long_position_is_never_given_a_second_open(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        held = _long()

        # Repeated upside breakouts, all while the context reports a long held.
        bars = [_flat_bar(i, "4400.00") for i in range(10)]
        for i in range(10, 40):
            bars.append(_bar(i, high=f"{4420 + i}.00", low="4400.00", close=f"{4420 + i}.00"))

        signals = _feed(strategy, bars, held=held)

        opens = [s for batch in signals for s in batch if s.action is SignalAction.OPEN]
        assert opens == []

    def test_a_short_is_closed_by_an_upside_breakout(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        held = _short()

        signals = _feed(strategy, _flat_then_breakout(10, direction="up"), held=held)
        closes = [s for batch in signals for s in batch if s.action is SignalAction.CLOSE]

        assert closes
        assert closes[0].legs[0].direction is Side.BUY

    def test_a_long_is_closed_by_a_downside_breakout(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        held = _long()

        signals = _feed(strategy, _flat_then_breakout(10, direction="down"), held=held)
        closes = [s for batch in signals for s in batch if s.action is SignalAction.CLOSE]

        assert closes
        assert closes[0].legs[0].direction is Side.SELL

    def test_a_long_survives_a_breakout_in_its_own_direction(self) -> None:
        """The held direction agreeing with the market is not itself an event
        that should fire anything."""
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        held = _long()
        bars = [_flat_bar(i, "4400.00") for i in range(10)]
        bars.append(_bar(10, high="4420.00", low="4400.00", close="4420.00"))

        signals = _feed(strategy, bars, held=held)

        assert all(s == [] for s in signals)


class TestNoIncrementalStateIsNeeded:
    """Unlike `MacdCrossover`'s EMAs, a rolling max/min depends only on the
    bars currently in view - a fresh instance facing the same history must
    decide identically to one that has been running the whole time."""

    def test_state_is_always_empty(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        _feed(strategy, _flat_then_breakout(5, direction="up"))

        assert strategy.state() == {}

    def test_a_fresh_instance_matches_a_long_running_one(self) -> None:
        bars = [
            *_flat_then_breakout(10, direction="up"),
            _bar(11, high="4425.00", low="4405.00", close="4425.00"),
        ]

        continuous = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        continuous_signals = _feed(continuous, bars)

        fresh = TrendlineBreakout(instrument=XAUUSD, lookback=10)
        window = BarWindow.of(tuple(bars))
        fresh_last = fresh.on_bar(_ctx(bars[-1], window))

        assert fresh_last == continuous_signals[-1]


class TestSignalShape:
    def test_a_signal_carries_exactly_one_leg(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        signals = _feed(strategy, _flat_then_breakout(5, direction="up"))

        for batch in signals:
            for signal in batch:
                assert len(signal.legs) == 1
                assert signal.legs[0].instrument == XAUUSD

    def test_the_reason_names_the_channel_bounds(self) -> None:
        strategy = TrendlineBreakout(instrument=XAUUSD, lookback=5)
        signals = _feed(strategy, _flat_then_breakout(5, direction="up"))

        opens = [s for batch in signals for s in batch if s.action is SignalAction.OPEN]
        assert opens
        assert "high" in opens[0].reason
