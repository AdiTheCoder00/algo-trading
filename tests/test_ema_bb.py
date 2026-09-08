"""EmaBollinger: the two readings, the warmup gate, and the EMA's continuity.

Numeric correctness of the bands themselves is `test_indicators.py`'s job, the
same split `test_macd_crossover.py` states. This file is about what the strategy
*does*: which mode fires on which bar, that neither fires before the EMA has
shed its seed, and that the EMA keeps advancing on a bar where a protective exit
returns early - the one place `MacdCrossover` has a known divergence that this
strategy deliberately does not reproduce.
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
from algo.core.signal import Signal
from algo.exchange.specs import ContractSpecStore
from algo.strategy.context import BarContext, PositionView, SessionInfo
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=30)
START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
#: The strategy needs `ema_period + bb_period + 2` bars before it will act.
WARMUP = EmaBollinger(instrument=XAUUSD).warmup_bars()


def _bar(index: int, close: str, *, high: str | None = None, low: str | None = None) -> Bar:
    c = Decimal(close)
    return Bar(
        ts=START + timedelta(minutes=30 * index),
        timeframe=TF,
        open=c,
        high=Decimal(high) if high is not None else c,
        low=Decimal(low) if low is not None else c,
        close=c,
        volume=100,
    )


def _ctx(bars: list[Bar], *, held: Position | None = None) -> BarContext:
    """A rolling window, not a single bar: the bands are computed from
    `ctx.history(bb_period + 1)`, so a one-bar window cannot exercise them."""
    positions = {} if held is None else {held.instrument.key: held}
    return BarContext(
        window=BarWindow.of(tuple(bars[-80:])),
        session=SessionInfo(
            session_date=bars[-1].ts.date(),
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


def _feed(
    strategy: EmaBollinger, closes: list[str], *, held: Position | None = None
) -> list[list[Signal]]:
    bars: list[Bar] = []
    out: list[list[Signal]] = []
    for i, close in enumerate(closes):
        bars.append(_bar(i, close))
        out.append(strategy.on_bar(_ctx(bars, held=held)))
    return out


def _long(lots: int = 100, cost: str = "4400") -> Position:
    return Position(
        instrument=XAUUSD, lots=lots, qty=Decimal(lots), cost_basis=Decimal(lots) * Decimal(cost)
    )


def _flat_then(tail: list[str], *, level: str = "4400") -> list[str]:
    """`WARMUP` bars of dead-flat price, then whatever the test needs.

    Flat is deliberate: it puts the EMA exactly at `level` and collapses the
    bands onto it, so the first non-flat close is unambiguously outside them
    and the test is about the rule, not about arithmetic luck.
    """
    return [level] * WARMUP + tail


def test_rejects_an_unknown_mode() -> None:
    with pytest.raises(DomainError, match="mode must be one of"):
        EmaBollinger(instrument=XAUUSD, mode="scalp")


def test_warmup_gate_blocks_every_mode() -> None:
    for mode in (PULLBACK, BREAKOUT):
        strategy = EmaBollinger(instrument=XAUUSD, mode=mode)
        # A hard rally, but stopping one bar short of the warmup requirement.
        closes = ["4400"] * (WARMUP - 2) + ["4500"]
        assert all(s == [] for s in _feed(strategy, closes)), mode


def test_breakout_goes_long_on_a_close_above_the_upper_band() -> None:
    strategy = EmaBollinger(instrument=XAUUSD, mode=BREAKOUT)
    signals = _feed(strategy, _flat_then(["4450"]))
    last = signals[-1]
    assert len(last) == 1
    assert last[0].action is SignalAction.OPEN
    assert last[0].legs[0].direction is Side.BUY
    assert "upper band" in last[0].reason


def test_breakout_goes_short_on_a_close_below_the_lower_band() -> None:
    strategy = EmaBollinger(instrument=XAUUSD, mode=BREAKOUT)
    signals = _feed(strategy, _flat_then(["4350"]))
    assert signals[-1][0].legs[0].direction is Side.SELL


def test_pullback_does_not_fire_while_price_is_still_outside_the_band() -> None:
    """The distinction the mode exists for: below the band is not a signal,
    leaving it is. A strategy that entered on the first bar below the band
    would be catching the knife it is supposed to be waiting out."""
    strategy = EmaBollinger(instrument=XAUUSD, mode=PULLBACK)
    # Rally to establish an uptrend, then one sharp dip that pokes below the
    # lower band while price is still above the 50 EMA.
    closes = _flat_then([str(4400 + i) for i in range(1, 40)] + ["4405"])
    signals = _feed(strategy, closes)
    assert signals[-1] == [], "entered while still outside the band"


def test_pullback_fires_when_price_closes_back_inside_the_band() -> None:
    strategy = EmaBollinger(instrument=XAUUSD, mode=PULLBACK)
    closes = _flat_then([str(4400 + i) for i in range(1, 40)] + ["4405", "4432"])
    signals = _feed(strategy, closes)
    last = signals[-1]
    assert len(last) == 1, f"expected one entry, got {[s.reason for s in last]}"
    assert last[0].action is SignalAction.OPEN
    assert last[0].legs[0].direction is Side.BUY
    assert "back above the lower band" in last[0].reason


def test_the_ema_advances_on_a_bar_where_a_protective_exit_fires() -> None:
    """`mt5/README.md` records that `MacdCrossover` returns before updating its
    histogram when an exit fires, so the next bar compares against the value
    from two bars ago. That wart is preserved there because a measured backtest
    depends on it. This strategy has no such history and must not grow one."""
    strategy = EmaBollinger(instrument=XAUUSD, mode=BREAKOUT, stop_loss_pct=Decimal("0.5"))
    _feed(strategy, ["4400"] * WARMUP)
    before = float(strategy.state()["ema"])

    # A long at 4400 with a 0.5% stop is stopped out at 4378; this bar trades
    # well through it, so the exit fires and `on_bar` returns early.
    bars = [_bar(i, "4400") for i in range(WARMUP)]
    bars.append(_bar(WARMUP, "4300", high="4400", low="4290"))
    exits = strategy.on_bar(_ctx(bars, held=_long(cost="4400")))

    assert exits and exits[0].action is SignalAction.CLOSE
    after = float(strategy.state()["ema"])
    assert after < before, "the EMA did not advance on the bar the exit fired"


def test_state_round_trips_the_ema_exactly() -> None:
    strategy = EmaBollinger(instrument=XAUUSD, mode=BREAKOUT)
    _feed(strategy, ["4400"] * 10 + ["4410"] * 10)
    saved = strategy.state()

    resumed = EmaBollinger(instrument=XAUUSD, mode=BREAKOUT)
    resumed.restore(saved)
    assert resumed.state()["ema"] == saved["ema"]
    assert resumed.state()["bars_seen"] == saved["bars_seen"]
