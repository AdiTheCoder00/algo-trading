"""PivotEmaCascade: the ordering of the cascade, what breaks it, and the exit.

The arithmetic of the pivot levels themselves is `test_indicators.py`'s job -
the same split `test_ema_bb.py` and `test_macd_crossover.py` already state.
This file is about what the strategy *does*: that the pivot line must go first,
that a bar may take several steps at once, that a close back through the last
level restarts the count, and that a position leaves on the first close back
above both fast EMAs.

## Why the tests run on (3, 5, 8) rather than (10, 20, 50, 100, 200)

The default set needs 286 bars of warmup before it will act and, more to the
point, needs a price path on which each of five EMAs is crossed in a stated
order - which is a path found by search, not written by hand, and a test whose
setup has to be searched for is a test nobody can later read. Three short
periods have the identical structure (a pivot, then every EMA in order, entry on
the slowest) and a path that fits on the screen.

`test_the_defaults_are_the_five_emas_the_rule_names` pins the real periods, so
the default is not left untested; it is just not what the mechanism is proved
on.

## Where the numbers in `_DECLINE` come from

They were computed, once, by stepping the same EMA recursion this strategy uses
over `_RUN_UP` and then choosing each close to sit one point below the next
level in the sequence and above every level after it. That is why they have
three decimal places and why they are pinned as literals: the point of the test
is that the strategy crosses those levels in that order, and recomputing the
path inside the test would just be the strategy's own arithmetic marking its own
homework.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
from algo.strategy.pivot_ema_cascade import EMA_PERIODS, PivotEmaCascade, seed_shed_bars

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=5)
START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)

#: Short periods, same structure. See the module docstring.
TEST_PERIODS = (3, 5, 8)

DAY_ONE = date(2026, 8, 24)
DAY_TWO = date(2026, 8, 25)
DAY_THREE = date(2026, 8, 26)

#: Joined part-way through by construction, so no pivots are drawn from it.
_SESSION_ONE = [4000.0] * 15

#: A full session: high 4000, low 3870, close 4000. Fibonacci pivots from those
#: put R3 at 4086.67, which is the line the cascade below breaks.
_SESSION_TWO = (
    [4000.0]
    + [4000.0 - 26.0 * i for i in range(1, 6)]
    + [3870.0 + 26.0 * i for i in range(1, 6)]
    + [4000.0]
)

#: A steady climb up through every pivot line, ending above R3 with the three
#: EMAs stacked below at 4079.996 / 4069.840 / 4054.873.
_RUN_UP = [4000.0 + 10.0 * i for i in range(1, 10)]

#: One level per bar, in order: R3 (4086.67), then the 3, 5 and 8 EMAs. The
#: last close is the entry.
_DECLINE = [4085.667, 4081.831, 4076.354, 4067.446]

#: One bar that goes through all four at once - the "ya saath mein hi" case.
_COLLAPSE = [3900.0]


def _bar(index: int, close: float, *, high: float | None = None, low: float | None = None) -> Bar:
    c = Decimal(str(close))
    return Bar(
        ts=START + timedelta(minutes=5 * index),
        timeframe=TF,
        open=c,
        high=Decimal(str(high)) if high is not None else c,
        low=Decimal(str(low)) if low is not None else c,
        close=c,
        volume=100,
    )


def _ctx(bars: list[Bar], session_day: date, *, held: Position | None = None) -> BarContext:
    positions = {} if held is None else {held.instrument.key: held}
    return BarContext(
        window=BarWindow.of(tuple(bars[-40:])),
        session=SessionInfo(
            session_date=session_day,
            is_us_dst=False,
            minutes_to_close=600,
            is_partial_bar=False,
            bar_index=0,
            bars_in_session=276,
        ),
        specs=ContractSpecStore.default(),
        positions=PositionView(positions),
        timeframe=TF,
        exchange=Exchange.OTC,
    )


def _feed(
    strategy: PivotEmaCascade,
    closes: list[float],
    days: list[date],
    *,
    held: Position | None = None,
) -> list[list[Signal]]:
    bars: list[Bar] = []
    out: list[list[Signal]] = []
    for i, (close, day) in enumerate(zip(closes, days, strict=True)):
        bars.append(_bar(i, close))
        out.append(strategy.on_bar(_ctx(bars, day, held=held)))
    return out


def _days(*counts: tuple[date, int]) -> list[date]:
    return [day for day, count in counts for _ in range(count)]


def _strategy(**kwargs: object) -> PivotEmaCascade:
    return PivotEmaCascade(instrument=XAUUSD, ema_periods=TEST_PERIODS, **kwargs)  # type: ignore[arg-type]


def _mirror(closes: list[float]) -> list[float]:
    """Reflect a path through 4000.

    Every level this strategy reads is an affine function of the closes - the
    EMAs by linearity, the Fibonacci pivots because reflecting swaps the
    session's high and low and leaves the range unchanged, mapping each Rn onto
    the corresponding Sn. So the reflected path crosses the reflected levels in
    the same order, upwards, and is an exact mirror rather than an approximate
    one.
    """
    return [8000.0 - c for c in closes]


def _short(lots: int = 100, cost: str = "4067") -> Position:
    return Position(
        instrument=XAUUSD, lots=-lots, qty=Decimal(-lots), cost_basis=Decimal(lots) * Decimal(cost)
    )


def _long(lots: int = 100, cost: str = "3933") -> Position:
    return Position(
        instrument=XAUUSD, lots=lots, qty=Decimal(lots), cost_basis=Decimal(lots) * Decimal(cost)
    )


# ------------------------------------------------------------------ the contract
def test_seed_shed_bars_reproduces_the_hand_calculation_in_ema_bb() -> None:
    """72 for a 50-period EMA is the number `ema_bb.py` derived in prose.

    If this ever stops matching, one of the two is wrong about what "warm"
    means, and the strategies would disagree about when they may trade.
    """
    assert seed_shed_bars(50) == 72
    assert seed_shed_bars(200) == 285


def test_the_defaults_are_the_five_emas_the_rule_names() -> None:
    strategy = PivotEmaCascade(instrument=XAUUSD)
    assert EMA_PERIODS == (10, 20, 50, 100, 200)
    assert strategy.params()["ema_periods"] == "10,20,50,100,200"
    assert strategy.params()["pivots"] == "fibonacci"
    #: The 200 EMA's seed-shedding length, plus the bar a crossing compares to.
    assert strategy.warmup_bars() == 286


@pytest.mark.parametrize(
    "periods",
    [(10, 20), (20, 10, 50), (10, 10, 20), (1, 10, 20)],
    ids=["too-few", "unordered", "duplicated", "degenerate"],
)
def test_rejects_period_sets_the_cascade_cannot_walk(periods: tuple[int, ...]) -> None:
    with pytest.raises(DomainError):
        PivotEmaCascade(instrument=XAUUSD, ema_periods=periods)


def test_no_entry_before_the_warmup_gate() -> None:
    strategy = _strategy()
    bars = strategy.warmup_bars() - 1
    closes = [4000.0] * (bars - 1) + [3000.0]
    signals = _feed(strategy, closes, _days((DAY_ONE, bars)))
    assert all(s == [] for s in signals)
    assert any("shed its seed" in note for note in strategy.drain_notes())


def test_no_entry_until_a_complete_session_has_drawn_pivots() -> None:
    """The first session is joined part-way through, so nothing is drawn from it.

    The collapse below would complete a cascade instantly if any pivot line
    existed. None does, because the only session that has ended was one this
    instance did not see the start of.
    """
    closes = _SESSION_ONE + _SESSION_TWO + _COLLAPSE
    days = _days((DAY_ONE, len(_SESSION_ONE)), (DAY_TWO, len(_SESSION_TWO) + 1))
    strategy = _strategy()
    signals = _feed(strategy, closes, days)
    assert all(s == [] for s in signals)
    assert any("no pivot lines to break" in note for note in strategy.drain_notes())


# ------------------------------------------------------------------- the cascade
def _cascade_run(
    tail: list[float], **kwargs: object
) -> tuple[PivotEmaCascade, list[list[Signal]]]:
    """Sessions one and two, then `tail` on session three."""
    closes = _SESSION_ONE + _SESSION_TWO + tail
    days = _days(
        (DAY_ONE, len(_SESSION_ONE)),
        (DAY_TWO, len(_SESSION_TWO)),
        (DAY_THREE, len(tail)),
    )
    strategy = _strategy(**kwargs)
    return strategy, _feed(strategy, closes, days)


def test_short_fires_on_the_bar_that_closes_below_the_slowest_ema() -> None:
    _, signals = _cascade_run(_RUN_UP + _DECLINE)
    fired = [(i, s) for i, s in enumerate(signals) if s]
    assert len(fired) == 1, [s[0].reason for _, s in fired]
    index, batch = fired[0]
    assert index == len(signals) - 1, "entered before the last EMA was crossed"
    assert batch[0].action is SignalAction.OPEN
    assert batch[0].legs[0].direction is Side.SELL
    assert "8 EMA" in batch[0].reason
    assert "R3 pivot" in batch[0].reason


def test_the_whole_cascade_may_complete_on_one_bar() -> None:
    """"Uske baad ya saath mein hi" - after, or on the same candle."""
    _, signals = _cascade_run(_RUN_UP + _COLLAPSE)
    assert signals[-1], "one bar through every level produced nothing"
    assert signals[-1][0].legs[0].direction is Side.SELL


def test_the_pivot_line_has_to_go_first() -> None:
    """Price already below every pivot line cannot arm, however it moves.

    Session two closes at 4000, below R1 at 4006.33; this path never trades
    back above a pivot line, so the EMAs are crossed with nothing armed.
    """
    _, signals = _cascade_run([4000.0 - 3.0 * i for i in range(1, 12)])
    assert all(s == [] for s in signals)


def test_a_close_back_above_the_last_level_restarts_the_count() -> None:
    """One bar back above the 3 EMA between the second and third step.

    Nothing else changes: the remaining decline still crosses the 5 and the 8.
    It produces no entry because the cascade is counting from zero again, and
    the interrupting close (4086.0) stops just under R3 at 4086.67, so nothing
    on the way back down closes through a pivot line to re-arm it.
    """
    interrupted = [*_DECLINE[:2], 4086.0, *_DECLINE[2:]]
    _, signals = _cascade_run(_RUN_UP + interrupted)
    assert all(s == [] for s in signals), "a broken cascade still entered"


def test_a_new_session_restarts_the_count() -> None:
    """The pivots are redrawn, so a cascade armed against the old ones is void."""
    closes = _SESSION_ONE + _SESSION_TWO + _RUN_UP + _DECLINE
    # Identical bars; only the last one is labelled as a new session.
    days = _days(
        (DAY_ONE, len(_SESSION_ONE)),
        (DAY_TWO, len(_SESSION_TWO)),
        (DAY_THREE, len(_RUN_UP) + len(_DECLINE) - 1),
        (date(2026, 8, 27), 1),
    )
    strategy = _strategy()
    signals = _feed(strategy, closes, days)
    assert all(s == [] for s in signals), "entered on a cascade the session boundary voided"


def test_long_is_the_exact_mirror_of_short() -> None:
    closes = _mirror(_SESSION_ONE + _SESSION_TWO + _RUN_UP + _DECLINE)
    days = _days(
        (DAY_ONE, len(_SESSION_ONE)),
        (DAY_TWO, len(_SESSION_TWO)),
        (DAY_THREE, len(_RUN_UP) + len(_DECLINE)),
    )
    strategy = _strategy()
    signals = _feed(strategy, closes, days)
    fired = [(i, s) for i, s in enumerate(signals) if s]
    assert len(fired) == 1
    assert fired[0][0] == len(signals) - 1
    assert fired[0][1][0].legs[0].direction is Side.BUY
    assert "S3 pivot" in fired[0][1][0].reason


# ----------------------------------------------------------------------- the exit
def test_a_short_leaves_on_the_first_close_back_above_both_fast_emas() -> None:
    # A wide flat stop, so this test is about the rule's own exit rather than
    # about which protective exit got there first.
    strategy, _ = _cascade_run(_RUN_UP + _DECLINE, stop_loss_pct=Decimal("5"))
    # One bar, closing above the 3 EMA (4082.17 after this bar) and the 5.
    signals = strategy.on_bar(_ctx([_bar(0, 4085.0)], DAY_THREE, held=_short()))
    assert len(signals) == 1
    assert signals[0].action is SignalAction.CLOSE
    assert signals[0].legs[0].direction is Side.BUY
    assert "back above" in signals[0].reason


def test_a_short_stays_on_while_only_the_fastest_ema_is_regained() -> None:
    """The rule names two lines. Regaining one of them is not the exit.

    The decline leaves the 3 EMA at 4073.394 and the 5 at 4073.829 - the fast
    one below the slow one, which is what a fall does to a stack. A close of
    4073.6 steps them to 4073.497 and 4073.753, so it ends above the 3 and
    below the 5: the one bar that distinguishes "closed above both" from
    "closed above either".
    """
    strategy, _ = _cascade_run(_RUN_UP + _DECLINE, stop_loss_pct=Decimal("5"))
    assert strategy.on_bar(_ctx([_bar(0, 4073.6)], DAY_THREE, held=_short())) == []


def test_a_long_leaves_on_the_first_close_back_below_both_fast_emas() -> None:
    closes = _mirror(_SESSION_ONE + _SESSION_TWO + _RUN_UP + _DECLINE)
    days = _days(
        (DAY_ONE, len(_SESSION_ONE)),
        (DAY_TWO, len(_SESSION_TWO)),
        (DAY_THREE, len(_RUN_UP) + len(_DECLINE)),
    )
    strategy = _strategy(stop_loss_pct=Decimal("5"))
    _feed(strategy, closes, days)
    signals = strategy.on_bar(_ctx([_bar(0, 8000.0 - 4085.0)], DAY_THREE, held=_long()))
    assert len(signals) == 1
    assert signals[0].action is SignalAction.CLOSE
    assert signals[0].legs[0].direction is Side.SELL


# ------------------------------------------------------------- exits and continuity
def test_the_emas_advance_on_a_bar_where_the_stop_fires() -> None:
    """`ema_bb.py` states why: an indicator that skips bars is not that indicator.

    `MacdCrossover` has the opposite wart and keeps it to match a measured
    backtest; this strategy has no such history to preserve.
    """
    strategy = _strategy(stop_loss_pct=Decimal("0.5"))
    _feed(strategy, [4000.0] * 20, _days((DAY_ONE, 20)))
    before = dict(strategy.state())

    # A held short, 2% against it: the flat stop fires on this bar.
    bars = [_bar(0, 4000.0), _bar(1, 4080.0, high=4090.0, low=4000.0)]
    signals = strategy.on_bar(_ctx(bars, DAY_ONE, held=_short(cost="4000")))
    assert signals and signals[0].action is SignalAction.CLOSE
    assert signals[0].context["exit"] == "stop"

    after = dict(strategy.state())
    for period in TEST_PERIODS:
        key = f"ema_{period}"
        assert float(after[key]) > float(before[key]), f"{key} did not advance"


def test_state_survives_a_restart_mid_cascade() -> None:
    """Stopped between the pivot break and the final EMA, and resumed.

    This is the case the persisted cascade exists for: the EMAs and the pivots
    are both reconstructible in principle, but a half-matched pattern is not,
    and a restart that dropped it would silently decline to take the trade.
    """
    closes = _SESSION_ONE + _SESSION_TWO + _RUN_UP + _DECLINE
    days = _days(
        (DAY_ONE, len(_SESSION_ONE)),
        (DAY_TWO, len(_SESSION_TWO)),
        (DAY_THREE, len(_RUN_UP) + len(_DECLINE)),
    )
    stopped = _strategy()
    _feed(stopped, closes[:-1], days[:-1])
    assert any(key.startswith("short_") for key in stopped.state()), "nothing to restore"

    resumed = _strategy()
    resumed.restore(stopped.state())

    bars = [_bar(0, closes[-2]), _bar(1, closes[-1])]
    ctx = _ctx(bars, days[-1])
    from_restart = resumed.on_bar(ctx)
    from_scratch = stopped.on_bar(ctx)
    assert [s.signal_id for s in from_restart] == [s.signal_id for s in from_scratch]
    assert from_restart and from_restart[0].legs[0].direction is Side.SELL
