"""The look-ahead canaries. Brief §7.3 and §7.4.

    "Canary test: write a deliberately cheating strategy that tries to read the
     next bar's close. Assert it raises. This test must exist and pass."

It exists. It does more than the brief asks, because a single accessor check only
proves one door is locked. The four cheats below cover the ways a strategy could
plausibly reach forward, and the poisoning test proves the property directly:
replace the future with garbage and the output must not move by one bit.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from algo.core.bar import M1, M30, Bar, BarWindow
from algo.core.errors import DomainError, LookAheadError
from algo.core.signal import Signal
from algo.data.resample import resample
from algo.data.synthetic import one_minute_session
from algo.exchange.calendar import MarketCalendar
from algo.exchange.specs import ContractSpecStore
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext, contexts_from_bars
from tests.conftest import SUMMER_DAY

# ---------------------------------------------------------------- the cheats


class NextBarCheat(Strategy):
    """Tries to read one bar past the end of the window."""

    strategy_id = "cheat_next_bar"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        ctx.bars[len(ctx.bars)]  # the next bar, if there were one
        raise AssertionError("reading past the window should have raised")

    def warmup_bars(self) -> int:
        return 0


class DataFrameCheat(Strategy):
    """Goes looking for the underlying frame the context was built from."""

    strategy_id = "cheat_dataframe"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        ctx.dataframe  # type: ignore[attr-defined]  # noqa: B018 - the access IS the cheat
        raise AssertionError("there should be no dataframe to reach")

    def warmup_bars(self) -> int:
        return 0


class FeedCheat(Strategy):
    """Goes looking for the feed, which could be re-read from the start."""

    strategy_id = "cheat_feed"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        ctx.feed  # type: ignore[attr-defined]  # noqa: B018 - the access IS the cheat
        raise AssertionError("there should be no feed to reach")

    def warmup_bars(self) -> int:
        return 0


class StashCheat(Strategy):
    """Tries to bolt an attribute onto the context to smuggle state forward."""

    strategy_id = "cheat_stash"

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        ctx.my_secret_future = "hello"  # type: ignore[attr-defined]
        raise AssertionError("the context should not accept new attributes")

    def warmup_bars(self) -> int:
        return 0


class RecordingStrategy(Strategy):
    """An honest strategy. Records exactly what it was shown."""

    strategy_id = "recorder"

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int, int, bool]] = []

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        self.seen.append(
            (
                ctx.now.isoformat(),
                str(ctx.bar.close),
                len(ctx.bars),
                ctx.session.bar_index,
                ctx.session.is_partial_bar,
            )
        )
        return []

    def warmup_bars(self) -> int:
        return 0


# ------------------------------------------------------------------ helpers


def _thirty_minute_bars(calendar: MarketCalendar) -> list[Bar]:
    minute_bars = one_minute_session(calendar, SUMMER_DAY, seed=7)
    return resample(minute_bars, calendar=calendar, timeframe=M30)


def _run(
    strategy: Strategy, bars: list[Bar], calendar: MarketCalendar, specs: ContractSpecStore
) -> None:
    for ctx in contexts_from_bars(bars, calendar=calendar, specs=specs, timeframe=M30):
        strategy.on_bar(ctx)


# -------------------------------------------------------------------- tests


class TestCheatingStrategiesAreStopped:
    def test_reading_the_next_bar_raises(
        self, calendar: MarketCalendar, specs: ContractSpecStore
    ) -> None:
        bars = _thirty_minute_bars(calendar)
        with pytest.raises(LookAheadError, match="outside the visible window"):
            _run(NextBarCheat(), bars, calendar, specs)

    def test_there_is_no_dataframe_to_reach(
        self, calendar: MarketCalendar, specs: ContractSpecStore
    ) -> None:
        bars = _thirty_minute_bars(calendar)
        with pytest.raises(AttributeError):
            _run(DataFrameCheat(), bars, calendar, specs)

    def test_there_is_no_feed_to_reach(
        self, calendar: MarketCalendar, specs: ContractSpecStore
    ) -> None:
        bars = _thirty_minute_bars(calendar)
        with pytest.raises(AttributeError):
            _run(FeedCheat(), bars, calendar, specs)

    def test_the_context_refuses_new_attributes(
        self, calendar: MarketCalendar, specs: ContractSpecStore
    ) -> None:
        """`__slots__` turns "someone adds a back door later" into a hard error."""
        bars = _thirty_minute_bars(calendar)
        with pytest.raises(AttributeError):
            _run(StashCheat(), bars, calendar, specs)


class TestBarWindowBounds:
    def test_last_index_is_the_current_bar(self, calendar: MarketCalendar) -> None:
        bars = _thirty_minute_bars(calendar)
        window = BarWindow.of(bars[:5])
        assert window[4] is window.current
        assert window[-1] is window.current

    @pytest.mark.parametrize("index", [5, 6, 99, -6, -100])
    def test_out_of_range_raises_lookahead(self, calendar: MarketCalendar, index: int) -> None:
        window = BarWindow.of(_thirty_minute_bars(calendar)[:5])
        with pytest.raises(LookAheadError):
            window[index]

    def test_lookahead_error_is_also_an_index_error(self, calendar: MarketCalendar) -> None:
        """So the window still behaves as a sequence for anything iterating it."""
        window = BarWindow.of(_thirty_minute_bars(calendar)[:3])
        assert isinstance(LookAheadError("x"), IndexError)
        assert len(list(window)) == 3

    def test_tail_never_widens(self, calendar: MarketCalendar) -> None:
        window = BarWindow.of(_thirty_minute_bars(calendar)[:5])
        assert len(window.tail(50)) == 5
        assert len(window.tail(2)) == 2
        assert window.tail(2).current is window.current

    def test_out_of_order_bars_are_rejected(self, calendar: MarketCalendar) -> None:
        bars = _thirty_minute_bars(calendar)
        with pytest.raises(DomainError, match="strictly increasing"):
            BarWindow.of([bars[1], bars[0]])


class TestFuturePoisoning:
    """The property test the accessor checks only approximate.

    Run the strategy over the real series. Then, for every decision point, replace
    every bar after it with randomised garbage and run again. If a single value
    differs, something read forward.
    """

    def test_randomised_future_changes_nothing(
        self, calendar: MarketCalendar, specs: ContractSpecStore
    ) -> None:
        bars = _thirty_minute_bars(calendar)
        assert len(bars) == 29

        clean = RecordingStrategy()
        _run(clean, bars, calendar, specs)

        rng = random.Random(1234)
        for decision_point in range(len(bars)):
            poisoned = list(bars[: decision_point + 1]) + [
                _garbage(bar, rng) for bar in bars[decision_point + 1 :]
            ]
            observer = RecordingStrategy()
            _run(observer, poisoned, calendar, specs)
            assert observer.seen[decision_point] == clean.seen[decision_point], (
                f"bar {decision_point} changed when only the FUTURE was altered — "
                "the engine is reading forward"
            )

    def test_the_poison_is_actually_different(self, calendar: MarketCalendar) -> None:
        """Guards the guard: a no-op poison would make the test above vacuous."""
        bars = _thirty_minute_bars(calendar)
        rng = random.Random(1234)
        poisoned = [_garbage(bar, rng) for bar in bars]
        assert any(p.close != b.close for p, b in zip(poisoned, bars, strict=True))


def _garbage(bar: Bar, rng: random.Random) -> Bar:
    """Same timestamp and shape, completely different prices.

    Timestamps are preserved so the session structure is untouched and the test
    isolates the one thing being asked: can a price from the future leak backwards?
    """
    level = Decimal(rng.randint(1, 400_000)) * Decimal("0.50")
    spread = Decimal(rng.randint(0, 200)) * Decimal("0.50")
    return Bar(
        ts=bar.ts,
        timeframe=bar.timeframe,
        open=level,
        high=level + spread,
        low=level,
        close=level + spread,
        volume=rng.randint(0, 9999),
        is_partial=bar.is_partial,
    )


class TestResamplerNeverEmitsAnUnfinishedBar:
    """Brief §7.5 — the in-progress higher-timeframe bar is unusable until it closes."""

    def test_partial_source_does_not_produce_a_bar(self, calendar: MarketCalendar) -> None:
        minute_bars = one_minute_session(calendar, SUMMER_DAY, seed=3)
        # Cut the source 13 minutes into the second 30-minute bar.
        truncated = minute_bars[:43]
        assert truncated[-1].timeframe == M1

        out = resample(truncated, calendar=calendar, timeframe=M30)
        assert len(out) == 1, "only the completed 09:00-09:30 bar may be emitted"

    def test_bar_appears_only_once_its_boundary_has_passed(
        self, calendar: MarketCalendar
    ) -> None:
        minute_bars = one_minute_session(calendar, SUMMER_DAY, seed=3)
        assert len(resample(minute_bars[:59], calendar=calendar, timeframe=M30)) == 1
        assert len(resample(minute_bars[:60], calendar=calendar, timeframe=M30)) == 2
