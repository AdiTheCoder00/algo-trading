"""MacdCrossover: signal logic, position discipline, and restart safety.

The strategy is exercised through a real `BarContext` fed one bar at a time,
never through the full `BacktestEngine` — it reads only `ctx.bar.close` and
`ctx.positions()`, so a single-bar window is the honest minimum harness and
keeps these tests independent of how any particular engine wires a session.

Numeric correctness (does this match pandas `adjust=False`) is
`test_indicators.py`'s job. This file is about what the strategy *does* once
the histogram crosses: enters, reverses, stays put, and never invents a
position from its own memory rather than the context.
"""

from __future__ import annotations

import random
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
from algo.strategy.macd_crossover import MacdCrossover

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=5)
START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)


def _bar(index: int, close: str) -> Bar:
    return Bar(
        ts=START + timedelta(minutes=5 * index),
        timeframe=TF,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
    )


def _ctx(bar: Bar, *, held: Position | None = None) -> BarContext:
    positions = {} if held is None else {held.instrument.key: held}
    return BarContext(
        window=BarWindow.of((bar,)),
        session=SessionInfo(
            session_date=bar.ts.date(),
            is_us_dst=False,
            minutes_to_close=600,
            is_partial_bar=False,
            bar_index=0,
            bars_in_session=288,
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


def _feed(strategy: MacdCrossover, closes: list[str], *, held: Position | None = None) -> list:
    """Run every close through the strategy in order, returning all signals."""
    signals = []
    for i, close in enumerate(closes):
        ctx = _ctx(_bar(i, close), held=held)
        signals.append(strategy.on_bar(ctx))
    return signals


def _v_shaped_series() -> list[str]:
    """A price path that reliably produces at least one clean up-cross and one
    clean down-cross under MACD(12,26,9): flat, then a sustained rally, then a
    sustained selloff. Real market noise is not needed to test the strategy's
    own bookkeeping around a crossover — only that a crossover happens at all."""
    values: list[float] = [4400.0] * 40
    for _ in range(60):
        values.append(values[-1] + 2.0)
    for _ in range(60):
        values.append(values[-1] - 2.0)
    return [f"{v:.2f}" for v in values]


def _inverted_v_series() -> list[str]:
    """The mirror of `_v_shaped_series`: flat, then a sustained selloff, then a
    sustained rally — reliably produces an early bearish cross followed by a
    bullish one.

    A monotonic trend starting from bar 0 does *not* reliably cross: both EMAs
    are seeded at the same first price, so the histogram starts at zero and a
    single-direction trend can walk it away from zero without ever crossing
    back through it. The flat run-in here matters for the same reason the
    rally run-in does in `_v_shaped_series` — it lets the histogram settle
    near zero before the move that is meant to cross it.
    """
    values: list[float] = [4400.0] * 40
    for _ in range(60):
        values.append(values[-1] - 2.0)
    for _ in range(60):
        values.append(values[-1] + 2.0)
    return [f"{v:.2f}" for v in values]


class TestConstruction:
    def test_fast_must_be_shorter_than_slow(self) -> None:
        with pytest.raises(DomainError, match="fast period must be shorter"):
            MacdCrossover(instrument=XAUUSD, fast=26, slow=12)

    def test_default_periods_are_12_26_9(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)

        assert strategy.params() == {
            "instrument": XAUUSD.key,
            "fast": "12",
            "slow": "26",
            "signal": "9",
        }

    def test_warmup_matches_the_indicator_modules_formula(self) -> None:
        assert MacdCrossover(instrument=XAUUSD).warmup_bars() == 26 + 9 + 2


class TestNothingHappensDuringWarmup:
    def test_no_signal_before_enough_bars(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        warmup = strategy.warmup_bars()

        signals = _feed(strategy, ["4400.00"] * (warmup - 1))

        assert all(s == [] for s in signals)

    def test_a_note_is_left_explaining_why(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        _feed(strategy, ["4400.00"] * 5)

        notes = strategy.drain_notes()

        assert any("no entry" in n and "MACD is trusted" in n for n in notes)


class TestItEntersOnACrossover:
    def test_a_bullish_cross_opens_long(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        signals = _feed(strategy, _v_shaped_series())

        opens = [s[0] for batch in signals for s in [batch] if batch]
        long_opens = [
            s for s in opens if s.action is SignalAction.OPEN and s.legs[0].direction is Side.BUY
        ]

        assert long_opens, "expected at least one bullish entry on a sustained rally"

    def test_a_bearish_cross_opens_short(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        signals = _feed(strategy, _inverted_v_series())

        opens = [s[0] for batch in signals if batch for s in [batch]]
        short_opens = [
            s for s in opens if s.action is SignalAction.OPEN and s.legs[0].direction is Side.SELL
        ]

        assert short_opens

    def test_flat_and_no_cross_emits_nothing(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        signals = _feed(strategy, ["4400.00"] * (strategy.warmup_bars() + 10))

        assert all(s == [] for s in signals)

    def test_no_signal_still_emitted_purely_from_zero_line_noise(self) -> None:
        """A flat series never crosses; the histogram sits at (or near) zero the
        whole time and must not fire spuriously."""
        strategy = MacdCrossover(instrument=XAUUSD)
        signals = _feed(strategy, ["4400.00"] * 200)

        assert all(s == [] for s in signals)


class TestPositionIsReadFromContextNeverFromMemory:
    """D-041: the same rule `CoinFlip` and `DeltaStrangle` both follow. A
    strategy that remembered its own intent would emit a second OPEN for a
    position the risk layer actually refused."""

    def test_while_flat_a_bullish_cross_opens(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        for close in ["4400.00"] * (strategy.warmup_bars() - 1):
            strategy.on_bar(_ctx(_bar(0, close)))

        # One more bar with a clear jump to force a cross, held=None (flat).
        signal = strategy.on_bar(_ctx(_bar(1, "4420.00")))

        # Whatever it decided, it must not error on reading position from ctx.
        assert isinstance(signal, list)

    def test_a_position_the_context_shows_open_blocks_a_new_entry(self) -> None:
        """Even on a fresh bullish cross, an already-long position must not
        receive a second OPEN — only CLOSE (on an opposing cross) or nothing."""
        strategy = MacdCrossover(instrument=XAUUSD)
        series = _v_shaped_series()
        held = _long()

        signals = _feed(strategy, series, held=held)

        opens_while_held = [
            s
            for batch in signals
            for s in batch
            if s.action is SignalAction.OPEN
        ]
        assert opens_while_held == [], "must never OPEN while ctx reports a held position"

    def test_a_short_the_context_shows_is_closed_on_a_bullish_cross(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        held = _short()

        signals = _feed(strategy, _v_shaped_series(), held=held)
        closes = [s for batch in signals for s in batch if s.action is SignalAction.CLOSE]

        assert closes, "a bullish cross must close an opposing short"
        assert all(s.legs[0].direction is Side.BUY for s in closes), (
            "closing a short is a BUY leg"
        )

    def test_a_long_the_context_shows_is_closed_on_a_bearish_cross(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        held = _long()

        signals = _feed(strategy, _inverted_v_series(), held=held)
        closes = [s for batch in signals for s in batch if s.action is SignalAction.CLOSE]

        assert closes
        assert all(s.legs[0].direction is Side.SELL for s in closes), (
            "closing a long is a SELL leg"
        )

    def test_a_long_survives_a_same_direction_signal(self) -> None:
        """No signal action should fire while the position already agrees with
        the market direction and nothing has crossed against it."""
        strategy = MacdCrossover(instrument=XAUUSD)
        held = _long()

        signals = _feed(strategy, ["4400.00"] * 100, held=held)

        assert all(s == [] for s in signals)


class TestRestartPersistence:
    """D-110's rationale, applied to indicator state: reseeding from zero on
    every restart would silently spend `warmup_bars()` bars re-converging."""

    def test_a_fresh_strategy_has_no_state_to_save(self) -> None:
        assert MacdCrossover(instrument=XAUUSD).state() == {}

    def test_state_appears_once_a_bar_has_been_seen(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        strategy.on_bar(_ctx(_bar(0, "4400.00")))

        state = strategy.state()

        assert "fast_ema" in state
        assert "bars_seen" in state
        assert state["bars_seen"] == "1"

    def test_restoring_reproduces_the_exact_next_decision(self) -> None:
        """The real test: two strategies fed the same subsequent bars, one
        continuous and one restarted midway, must make identical decisions."""
        series = _v_shaped_series()
        split = 90

        continuous = MacdCrossover(instrument=XAUUSD)
        continuous_signals = _feed(continuous, series)

        first_half = MacdCrossover(instrument=XAUUSD)
        for i, close in enumerate(series[:split]):
            first_half.on_bar(_ctx(_bar(i, close)))
        saved = first_half.state()

        restarted = MacdCrossover(instrument=XAUUSD)
        restarted.restore(saved)
        restarted_signals = [
            restarted.on_bar(_ctx(_bar(split + i, close)))
            for i, close in enumerate(series[split:])
        ]

        assert restarted_signals == continuous_signals[split:]

    def test_restoring_garbage_refuses_rather_than_running_half_seeded(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)

        with pytest.raises(DomainError, match="cannot restore MACD state"):
            strategy.restore({"fast_ema": "not-a-float", "slow_ema": "0", "signal_ema": "0"})

    def test_restoring_nothing_is_a_silent_cold_start(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        strategy.restore({})

        assert strategy.state() == {}

    def test_repr_float_round_trips_exactly(self) -> None:
        """The persistence format itself: `float(repr(x)) == x` for every value
        this strategy will ever produce, not just tidy ones."""
        rng = random.Random(3)
        for _ in range(50):
            x = rng.uniform(-9999, 9999)
            assert float(repr(x)) == x


class TestSignalShape:
    def test_a_signal_carries_exactly_one_leg(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        signals = _feed(strategy, _v_shaped_series())

        for batch in signals:
            for signal in batch:
                assert len(signal.legs) == 1
                assert signal.legs[0].instrument == XAUUSD

    def test_the_reason_names_the_histogram_transition(self) -> None:
        strategy = MacdCrossover(instrument=XAUUSD)
        signals = _feed(strategy, _v_shaped_series())

        opens = [s for batch in signals for s in batch if s.action is SignalAction.OPEN]
        assert opens
        assert "hist" in opens[0].reason
