"""`replay_signals`: the same answer as the backtest, on the same bars.

The point of this module existing at all is that a monitor and a study must not
disagree about whether a rule fired, so the test that matters most is the one
that runs both paths over one series and compares them
(`test_it_finds_the_same_entries_run_cfd_backtest_does`). Everything else here
is the bookkeeping that test depends on: the paper position's sign, that the
exit rule can see it, and that the notes are drained rather than accumulated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from algo.backtest.cfd_runner import run_cfd_backtest
from algo.backtest.signal_replay import (
    Fired,
    PaperBook,
    forex_session_of,
    replay_signals,
)
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side, SignalAction
from algo.core.instrument import CfdId
from algo.strategy.pivot_ema_cascade import PivotEmaCascade

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=5)
TEST_PERIODS = (3, 5, 8)

#: Mid-morning UTC, so every bar of a day falls inside that day's forex session
#: and `forex_session_of` returns the calendar date - the sessions have to be
#: distinguishable for the pivots to roll, and this is the least clever way.
FIRST_BAR = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)

#: The path `test_pivot_ema_cascade.py` derives: a partial session, then a full
#: one drawing R3 at 4086.67, then a climb above it and a decline that crosses
#: R3 and the three EMAs in order. Repeated here rather than imported because a
#: test that reaches into another test file couples the two files' setups.
_SESSION_ONE = [4000.0] * 15
_SESSION_TWO = (
    [4000.0]
    + [4000.0 - 26.0 * i for i in range(1, 6)]
    + [3870.0 + 26.0 * i for i in range(1, 6)]
    + [4000.0]
)
_RUN_UP = [4000.0 + 10.0 * i for i in range(1, 10)]
_DECLINE = [4085.667, 4081.831, 4076.354, 4067.446]
#: After the entry, a close back above both fast EMAs - the exit.
_REBOUND = [4085.0]


def _series() -> list[Bar]:
    """One bar per five minutes, each session on its own calendar day."""
    sessions = [_SESSION_ONE, _SESSION_TWO, _RUN_UP + _DECLINE + _REBOUND]
    bars: list[Bar] = []
    for day, closes in enumerate(sessions):
        for index, close in enumerate(closes):
            value = Decimal(str(close))
            bars.append(
                Bar(
                    ts=FIRST_BAR + timedelta(days=day, minutes=5 * index),
                    timeframe=TF,
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=100,
                )
            )
    return bars


def _factory() -> PivotEmaCascade:
    #: A wide stop, so this file measures the rule's own entry and exit rather
    #: than which protective exit reached the bar first.
    return PivotEmaCascade(
        instrument=XAUUSD, ema_periods=TEST_PERIODS, stop_loss_pct=Decimal("5")
    )


def _replay(bars: list[Bar]) -> list[Fired]:
    return replay_signals(
        bars,
        strategy_factory=_factory,
        instrument=XAUUSD,
        timeframe=TF,
        session_of=forex_session_of,
    )


# ------------------------------------------------------------------ the contract
def test_it_reports_the_entry_on_the_bar_that_completed_the_cascade() -> None:
    bars = _series()
    fired = _replay(bars)
    entries = [f for f in fired if f.is_entry]
    assert len(entries) == 1
    assert entries[0].side is Side.SELL
    assert entries[0].bar.close == Decimal(str(_DECLINE[-1]))


def test_the_paper_position_lets_the_exit_rule_fire() -> None:
    """Without a position the strategy has nothing to close, so an exit-only
    replay would silently report entries and never their other end."""
    fired = _replay(_series())
    assert [f.signal.action for f in fired] == [SignalAction.OPEN, SignalAction.CLOSE]
    closing = fired[-1]
    # A closing BUY is the exit of a short - the convention `Fired.side` states.
    assert closing.side is Side.BUY
    assert "back above" in closing.signal.reason


def test_it_finds_the_same_entries_run_cfd_backtest_does() -> None:
    """The anti-drift test this module exists for.

    `run_cfd_backtest` fills and charges costs, so its P&L is its own; what has
    to match is *when it traded*. If a future change to either path moves an
    entry by one bar, the monitor would alert on a rule the study never scored,
    which is the failure mode `signal_replay.py`'s docstring is about.
    """
    bars = _series()
    replayed = [f for f in _replay(bars) if f.is_entry]

    result = run_cfd_backtest(
        bars,
        instrument=XAUUSD,
        timeframe=TF,
        strategy_factory=_factory,
        stop_loss_pct=Decimal("5"),
        trail_activation_pct=Decimal("2"),
        trail_pct=Decimal("0"),
        lots=1,
        starting_equity=Decimal("100000"),
    )

    assert [t.entry_ts for t in result.trades] == [f.bar.ts for f in replayed]
    assert [t.side for t in result.trades] == [f.side for f in replayed]


def test_a_fresh_strategy_per_replay_means_two_runs_agree() -> None:
    """The factory exists so indicator state cannot leak between series."""
    bars = _series()
    first = [(f.bar.ts, f.signal.signal_id) for f in _replay(bars)]
    second = [(f.bar.ts, f.signal.signal_id) for f in _replay(bars)]
    assert first == second


def test_notes_do_not_accumulate_across_the_replay() -> None:
    """A strategy that noted every warmup bar would hold hundreds of strings.

    Checked through the strategy the replay used rather than by counting them:
    what matters is that the instance is left drained, not the exact wording.
    """
    strategies: list[PivotEmaCascade] = []

    def factory() -> PivotEmaCascade:
        strategies.append(_factory())
        return strategies[-1]

    replay_signals(
        _series(),
        strategy_factory=factory,
        instrument=XAUUSD,
        timeframe=TF,
        session_of=forex_session_of,
    )
    assert len(strategies) == 1
    assert strategies[0].drain_notes() == []


# --------------------------------------------------------------------- paper book
def test_the_paper_book_signs_the_quantity_by_side() -> None:
    book = PaperBook(XAUUSD)
    assert book.position() is None

    book.open(Side.SELL, Decimal("4000"))
    short = book.position()
    assert short is not None and short.qty < 0
    assert short.average_price == Decimal("4000")

    book.open(Side.BUY, Decimal("4100"))
    long_position = book.position()
    assert long_position is not None and long_position.qty > 0

    book.close()
    assert book.position() is None


def test_forex_session_of_places_a_mid_session_bar_on_its_own_date() -> None:
    assert forex_session_of(FIRST_BAR) == date(2026, 8, 24)


# ---------------------------------------------------------------------- observer
def test_the_observer_sees_every_bar_and_the_live_strategy() -> None:
    """The hook a diagnostic reads the cascade's stage through.

    It exists so a study can ask "how far did the attempts that never became
    trades get" without writing a second copy of the rule, so what it must
    guarantee is that the strategy it hands over is the live one, mid-replay.
    """
    bars = _series()
    seen: list[tuple[datetime, str]] = []

    def observe(bar: Bar, strategy: PivotEmaCascade) -> None:
        seen.append((bar.ts, strategy.state().get("short_stage", "0")))

    replay_signals(
        bars,
        strategy_factory=_factory,
        instrument=XAUUSD,
        timeframe=TF,
        session_of=forex_session_of,
        observer=observe,  # type: ignore[arg-type]
    )
    assert [ts for ts, _ in seen] == [bar.ts for bar in bars]
    assert any(stage != "0" for _, stage in seen), "the cascade never armed"


def test_a_completed_cascade_is_not_visible_as_a_stage() -> None:
    """Why the funnel's last column is counted from the signals.

    The strategy resets the cascade inside the same `on_bar` that emits the
    entry, so no observer can ever see the final stage. A diagnostic that read
    the last step from `state()` would report zero entries while the trade
    table beside it showed dozens - which is how it was first written, and what
    this test now prevents from coming back.
    """
    bars = _series()
    stages: list[int] = []

    def observe(_bar: Bar, strategy: PivotEmaCascade) -> None:
        stages.append(int(strategy.state().get("short_stage", "0") or 0))

    fired = replay_signals(
        bars,
        strategy_factory=_factory,
        instrument=XAUUSD,
        timeframe=TF,
        session_of=forex_session_of,
        observer=observe,  # type: ignore[arg-type]
    )
    #: Three EMAs in the test set, so a completed cascade is stage 4.
    assert max(stages) < 4
    assert [f for f in fired if f.is_entry], "but the entry did happen"
