"""HilegaMilega: the incremental indicator, the rules, and restart safety.

Same harness and same division of labour as `test_macd_crossover.py`: the
strategy is fed one bar at a time through a real `BarContext`, never through the
`BacktestEngine`, because it reads only `ctx.bar` and `ctx.positions()`.

Numeric correctness of RSI/EMA/WMA is `test_indicators.py`'s job. What this file
owns is everything the vectorised form cannot check: that the strategy's
one-bar-at-a-time update reproduces `indicators.hilega_milega` exactly, that the
rules fire on the right alignments, that the exit is deliberately looser than
the entry, and that a restart resumes on the same numbers it stopped on.

The alignment tests drive the indicator directly rather than searching for a
price path that happens to produce the reading they want. A test that has to
hunt for its own precondition tends to stop testing what it was named after the
moment the indicator's constants change.
"""

from __future__ import annotations

import math
import random
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
from algo.pricing.indicators import hilega_milega
from algo.strategy.context import BarContext, PositionView, SessionInfo
from algo.strategy.hilega_milega import HilegaMilega

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


def _bar_range(index: int, *, high: str, low: str, close: str) -> Bar:
    """A bar with a real intrabar range — `_bar` collapses high/low/close to one
    value, which cannot exercise an intrabar-only stop touch."""
    return Bar(
        ts=START + timedelta(minutes=5 * index),
        timeframe=TF,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
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


def _feed(
    strategy: HilegaMilega, closes: list[str], *, held: Position | None = None
) -> list[list[Signal]]:
    signals: list[list[Signal]] = []
    for i, close in enumerate(closes):
        signals.append(strategy.on_bar(_ctx(_bar(i, close), held=held)))
    return signals


def _random_walk(n: int, *, seed: int = 1, start: float = 4400.0) -> list[float]:
    rng = random.Random(seed)
    values = [start]
    for _ in range(n - 1):
        values.append(values[-1] + rng.gauss(0, 3))
    return values


def _closes(values: list[float]) -> list[str]:
    return [f"{v:.2f}" for v in values]


def _rally_then_selloff() -> list[str]:
    """Choppy sideways, a sustained rally, then a sustained selloff.

    Every leg zig-zags rather than moving one way only, and that is not
    decoration. A monotone leg has no losing bar at all, which drives Wilder's
    average loss to zero and pins the RSI at 100 — where it equals both of its
    own averages and therefore leads neither, so a strictly-greater alignment
    rule correctly refuses to fire. A price path a real market cannot produce
    would have been testing the wrong thing.

    Each leg is comfortably longer than `warmup_bars()` so both alignments are
    reachable. Beyond that, real market noise is not needed to test the
    strategy's own bookkeeping — only that the alignments occur at all.
    """
    values = [4400.0]
    for step in ([1.0, -1.0] * 25) + ([3.0, -1.0] * 45) + ([-3.0, 1.0] * 45):
        values.append(values[-1] + step)
    return _closes(values)


class _Fixed(HilegaMilega):
    """A `HilegaMilega` whose indicator returns a fixed reading.

    Reaching a specific alignment by choosing prices means solving backwards for
    an RSI, which is a different test from the one being written. Fixing the
    reading keeps each rule test about the rule.

    Subclassed rather than monkeypatched so the override is visible at the call
    site, and so every other part of the strategy — the exits, the warmup gate,
    the position discipline — runs exactly as it does in production.
    """

    def __init__(self, lines: tuple[float, float, float] | None, **kwargs: object) -> None:
        super().__init__(instrument=XAUUSD, **kwargs)  # type: ignore[arg-type]
        self._lines = lines
        self._bars_seen = self.warmup_bars()

    def _update(self, close: float) -> tuple[float, float, float] | None:
        self._bars_seen += 1
        return self._lines


class TestConstruction:
    def test_the_trend_ema_must_be_shorter_than_the_weighted_ma(self) -> None:
        with pytest.raises(DomainError, match="trend EMA must be shorter"):
            HilegaMilega(instrument=XAUUSD, trend_period=21, weighted_period=21)

    def test_a_degenerate_rsi_period_is_refused(self) -> None:
        with pytest.raises(DomainError, match="RSI period must be at least 2"):
            HilegaMilega(instrument=XAUUSD, rsi_period=1)

    def test_a_negative_separation_is_refused(self) -> None:
        with pytest.raises(DomainError, match="min_separation cannot be negative"):
            HilegaMilega(instrument=XAUUSD, min_separation=Decimal("-1"))

    def test_the_defaults_are_the_published_ones(self) -> None:
        params = HilegaMilega(instrument=XAUUSD).params()

        assert params["rsi_period"] == "9"
        assert params["trend_period"] == "3"
        assert params["weighted_period"] == "21"

    def test_warmup_covers_the_whole_indicator_stack(self) -> None:
        assert HilegaMilega(instrument=XAUUSD).warmup_bars() == 9 + 21 + 3 + 2

    def test_the_parameters_reach_the_hash(self) -> None:
        """A different RSI length must produce different signal ids, or a replay
        after a config edit would match orders placed under the old settings."""
        base = HilegaMilega(instrument=XAUUSD)
        other = HilegaMilega(instrument=XAUUSD, rsi_period=14)

        assert base.params_hash() != other.params_hash()


class TestTheIncrementalIndicatorMatchesTheVectorisedOne:
    """The claim `hilega_milega.py` makes in its docstring, checked rather than
    trusted. Two implementations of one indicator is the drift `indicators.py`
    exists to prevent, and this is the only place the two ever meet."""

    def test_every_bar_agrees_to_the_last_float(self) -> None:
        values = _random_walk(400, seed=21)
        strategy = HilegaMilega(instrument=XAUUSD)
        expected = hilega_milega(values)

        compared = 0
        for i, value in enumerate(values):
            got = strategy._update(value)
            if got is None:
                assert math.isnan(expected.weighted[i])
                continue
            compared += 1
            assert got[0] == expected.strength[i]
            assert got[1] == expected.trend[i]
            assert got[2] == expected.weighted[i]

        assert compared > 300

    def test_it_returns_nothing_until_all_three_lines_exist(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD)
        values = _random_walk(40, seed=22)

        first_defined = next(i for i, v in enumerate(values) if strategy._update(v) is not None)

        assert first_defined == 9 + 21 - 1

    def test_custom_periods_track_the_vectorised_form_too(self) -> None:
        values = _random_walk(300, seed=23)
        strategy = HilegaMilega(
            instrument=XAUUSD, rsi_period=14, trend_period=5, weighted_period=30
        )
        expected = hilega_milega(values, rsi_period=14, trend_period=5, weighted_period=30)

        for i, value in enumerate(values):
            got = strategy._update(value)
            if got is not None:
                assert got == (
                    expected.strength[i],
                    expected.trend[i],
                    expected.weighted[i],
                )


class TestEntryRules:
    def test_the_rsi_leading_both_averages_above_50_opens_a_long(self) -> None:
        strategy = _Fixed((62.0, 58.0, 55.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400")))

        assert [s.action for s in signals] == [SignalAction.OPEN]
        assert signals[0].legs[0].direction is Side.BUY

    def test_the_rsi_trailing_both_averages_below_50_opens_a_short(self) -> None:
        strategy = _Fixed((38.0, 42.0, 45.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400")))

        assert [s.action for s in signals] == [SignalAction.OPEN]
        assert signals[0].legs[0].direction is Side.SELL

    def test_strength_above_50_is_not_enough_on_its_own(self) -> None:
        """The WMA still sits above the RSI — the source's "red line outside the
        strength", which is the half of the rule people drop."""
        strategy = _Fixed((62.0, 58.0, 66.0), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []

    def test_alignment_below_50_never_opens_a_long(self) -> None:
        """The RSI leads both averages, but on the wrong half of the panel."""
        strategy = _Fixed((48.0, 44.0, 41.0), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []

    def test_the_midline_itself_is_not_above_the_midline(self) -> None:
        strategy = _Fixed((50.0, 46.0, 44.0), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []

    def test_nothing_fires_before_warmup(self) -> None:
        strategy = _Fixed((62.0, 58.0, 55.0), stop_loss_pct=Decimal("0"))
        strategy._bars_seen = 0

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []
        assert "need 35" in strategy.drain_notes()[0]

    def test_an_undefined_reading_is_a_note_not_a_trade(self) -> None:
        strategy = _Fixed(None, stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []
        assert strategy.drain_notes()


class TestMinSeparation:
    def test_zero_leaves_the_plain_alignment_rule_alone(self) -> None:
        strategy = _Fixed((51.0, 50.9, 50.8), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400")))[0].action is SignalAction.OPEN

    def test_a_narrow_alignment_is_refused_once_it_is_set(self) -> None:
        strategy = _Fixed(
            (51.0, 50.9, 50.8),
            min_separation=Decimal("5"),
            stop_loss_pct=Decimal("0"),
        )

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []

    def test_a_wide_alignment_still_passes(self) -> None:
        strategy = _Fixed(
            (70.0, 60.0, 55.0),
            min_separation=Decimal("5"),
            stop_loss_pct=Decimal("0"),
        )

        assert strategy.on_bar(_ctx(_bar(0, "4400")))[0].action is SignalAction.OPEN

    def test_it_measures_from_the_nearer_average_not_the_further_one(self) -> None:
        """`trend` is 4 points away, `weighted` 15. A rule reading the further
        line would let this through; the setup's claim is about the gap that has
        actually closed."""
        strategy = _Fixed(
            (70.0, 66.0, 55.0),
            min_separation=Decimal("5"),
            stop_loss_pct=Decimal("0"),
        )

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []

    def test_it_applies_to_shorts_the_same_way(self) -> None:
        strategy = _Fixed(
            (49.0, 49.1, 49.2),
            min_separation=Decimal("5"),
            stop_loss_pct=Decimal("0"),
        )

        assert strategy.on_bar(_ctx(_bar(0, "4400"))) == []

    def test_it_reaches_the_hash(self) -> None:
        base = HilegaMilega(instrument=XAUUSD)
        tight = HilegaMilega(instrument=XAUUSD, min_separation=Decimal("5"))

        assert base.params_hash() != tight.params_hash()


class TestExitRules:
    def test_a_long_closes_when_the_rsi_falls_back_to_its_weighted_average(
        self,
    ) -> None:
        strategy = _Fixed((55.0, 57.0, 55.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400"), held=_long()))

        assert [s.action for s in signals] == [SignalAction.CLOSE]
        assert signals[0].legs[0].direction is Side.SELL

    def test_a_long_is_held_while_the_rsi_still_leads_its_weighted_average(
        self,
    ) -> None:
        strategy = _Fixed((60.0, 58.0, 52.0), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"), held=_long())) == []

    def test_the_exit_does_not_wait_for_the_entry_condition_to_invert(self) -> None:
        """The point of the looser exit. The RSI is below its WMA but still
        above 50 and above its EMA, so a mirror-of-entry rule would hold on."""
        strategy = _Fixed((60.0, 58.0, 62.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400"), held=_long()))

        assert [s.action for s in signals] == [SignalAction.CLOSE]

    def test_a_short_closes_when_the_rsi_climbs_back_to_its_weighted_average(
        self,
    ) -> None:
        strategy = _Fixed((45.0, 43.0, 44.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400"), held=_short()))

        assert [s.action for s in signals] == [SignalAction.CLOSE]
        assert signals[0].legs[0].direction is Side.BUY

    def test_a_short_is_held_while_the_rsi_still_trails_its_weighted_average(
        self,
    ) -> None:
        strategy = _Fixed((40.0, 42.0, 48.0), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"), held=_short())) == []

    def test_a_close_and_a_reversal_never_share_a_bar(self) -> None:
        """`ProtectiveExits.check` relies on one signal per bar, and acting
        twice on one close would be two decisions from one price."""
        strategy = _Fixed((38.0, 42.0, 45.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400"), held=_long()))

        assert len(signals) == 1
        assert signals[0].action is SignalAction.CLOSE


class TestProtectiveExits:
    def test_the_stop_fires_before_the_warmup_gate(self) -> None:
        """A held position must never go unprotected because the indicator has
        not converged yet — `MacdCrossover`'s rule, and it matters more here
        because the published method has no stop at all."""
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0.5"))

        signals = strategy.on_bar(
            _ctx(_bar_range(0, high="4400", low="4300", close="4390"), held=_long())
        )

        assert [s.action for s in signals] == [SignalAction.CLOSE]
        assert signals[0].context["exit"] == "stop"

    def test_the_stop_reads_the_bars_range_not_only_its_close(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0.5"))

        signals = strategy.on_bar(
            _ctx(_bar_range(0, high="4405", low="4300", close="4402"), held=_long())
        )

        assert [s.action for s in signals] == [SignalAction.CLOSE]

    def test_a_stop_of_zero_disables_it(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))

        assert (
            strategy.on_bar(
                _ctx(_bar_range(0, high="4400", low="3000", close="3100"), held=_long())
            )
            == []
        )

    def test_the_exit_kind_is_structured_not_only_prose(self) -> None:
        """So a consumer never has to parse `reason` to price the fill."""
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0.5"))

        signals = strategy.on_bar(
            _ctx(_bar_range(0, high="4400", low="4300", close="4390"), held=_long())
        )

        assert signals[0].context["exit"] in ("stop", "trail")


class TestPositionDiscipline:
    def test_it_reads_the_position_from_the_context_not_from_memory(self) -> None:
        """D-041. The strategy opened a long on an earlier bar; the context then
        says flat, so the next aligned bar must open again rather than manage a
        position the book does not have."""
        strategy = _Fixed((62.0, 58.0, 55.0), stop_loss_pct=Decimal("0"))

        first = strategy.on_bar(_ctx(_bar(0, "4400")))
        second = strategy.on_bar(_ctx(_bar(1, "4401")))

        assert first[0].action is SignalAction.OPEN
        assert second[0].action is SignalAction.OPEN

    def test_it_does_not_add_to_a_position_it_already_holds(self) -> None:
        strategy = _Fixed((62.0, 58.0, 55.0), stop_loss_pct=Decimal("0"))

        assert strategy.on_bar(_ctx(_bar(0, "4400"), held=_long())) == []

    def test_a_flat_position_row_is_treated_as_flat(self) -> None:
        flat = Position(instrument=XAUUSD, lots=0, qty=Decimal(0), cost_basis=Decimal(0))
        strategy = _Fixed((62.0, 58.0, 55.0), stop_loss_pct=Decimal("0"))

        signals = strategy.on_bar(_ctx(_bar(0, "4400"), held=flat))

        assert [s.action for s in signals] == [SignalAction.OPEN]


class TestOnRealisticSeries:
    def test_a_rally_then_a_selloff_produces_both_sides(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))

        emitted = [s for batch in _feed(strategy, _rally_then_selloff()) for s in batch]
        directions = {s.legs[0].direction for s in emitted if s.action is SignalAction.OPEN}

        assert directions == {Side.BUY, Side.SELL}

    def test_it_emits_nothing_at_all_during_warmup(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))
        closes = _rally_then_selloff()

        batches = _feed(strategy, closes[: strategy.warmup_bars() - 1])

        assert all(batch == [] for batch in batches)

    def test_a_flat_series_never_trades(self) -> None:
        """No change at all means Wilder's averages are both zero, which
        `rsi_from_averages` reads as 100 — but with the WMA pinned to the same
        value, nothing is ever leading anything."""
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))

        emitted = [s for batch in _feed(strategy, ["4400"] * 200) for s in batch]

        assert emitted == []

    def test_every_signal_id_is_distinct(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))

        emitted = [s for batch in _feed(strategy, _rally_then_selloff()) for s in batch]

        assert len({s.signal_id for s in emitted}) == len(emitted)


class TestRestartSafety:
    def test_a_cold_strategy_persists_nothing(self) -> None:
        assert HilegaMilega(instrument=XAUUSD).state() == {}

    def test_a_round_trip_leaves_the_indicator_where_it_was(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD)
        _feed(strategy, _closes(_random_walk(120, seed=24)))

        restored = HilegaMilega(instrument=XAUUSD)
        restored.restore(strategy.state())

        assert restored.state() == strategy.state()

    def test_a_restart_mid_series_reaches_the_same_readings(self) -> None:
        """The reason the state exists. A cold restart would spend
        `warmup_bars()` bars blind, and would then disagree about the lines."""
        values = _random_walk(200, seed=25)
        closes = _closes(values)

        continuous = HilegaMilega(instrument=XAUUSD)
        _feed(continuous, closes)

        first_half = HilegaMilega(instrument=XAUUSD)
        _feed(first_half, closes[:120])
        restarted = HilegaMilega(instrument=XAUUSD)
        restarted.restore(first_half.state())
        _feed(restarted, closes[120:])

        assert restarted.state() == continuous.state()

    def test_the_seed_buffers_survive_a_restart_during_warmup(self) -> None:
        """They are non-empty only for the first nine bars, which is exactly
        when dropping them would reseed Wilder's average from bars that came
        after the restart."""
        closes = _closes(_random_walk(200, seed=26))

        continuous = HilegaMilega(instrument=XAUUSD)
        _feed(continuous, closes)

        early = HilegaMilega(instrument=XAUUSD)
        _feed(early, closes[:5])
        assert early.state()["seed_gains"]
        restarted = HilegaMilega(instrument=XAUUSD)
        restarted.restore(early.state())
        _feed(restarted, closes[5:])

        assert restarted.state() == continuous.state()

    def test_an_empty_state_is_a_cold_start_not_an_error(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD)
        strategy.restore({})

        assert strategy.state() == {}

    def test_a_corrupt_state_is_refused_rather_than_half_restored(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD)

        with pytest.raises(DomainError, match="partially-restored indicator"):
            strategy.restore({"prev_close": "4400.0", "bars_seen": "not a number"})

    def test_one_wilder_average_without_the_other_is_refused(self) -> None:
        """Both parse as floats, so nothing upstream catches this — and a
        settled numerator over an unsettled denominator would produce an RSI
        that looks entirely plausible."""
        strategy = HilegaMilega(instrument=XAUUSD)

        with pytest.raises(DomainError, match="both be seeded or both unseeded"):
            strategy.restore(
                {
                    "prev_close": "4400.0",
                    "average_gain": "1.5",
                    "average_loss": "",
                    "bars_seen": "50",
                }
            )

    def test_an_over_long_saved_window_is_refused(self) -> None:
        strategy = HilegaMilega(instrument=XAUUSD)

        with pytest.raises(DomainError, match="but the weighted MA is 21 bars"):
            strategy.restore(
                {
                    "prev_close": "4400.0",
                    "average_gain": "1.5",
                    "average_loss": "1.5",
                    "rsi_window": ",".join(["50.0"] * 30),
                    "bars_seen": "50",
                }
            )

    def test_a_restored_strategy_does_not_re_serve_its_warmup(self) -> None:
        closes = _closes(_random_walk(120, seed=27))
        strategy = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))
        _feed(strategy, closes)

        restarted = HilegaMilega(instrument=XAUUSD, stop_loss_pct=Decimal("0"))
        restarted.restore(strategy.state())
        restarted.on_bar(_ctx(_bar(0, closes[-1])))

        assert not any("need 35" in note for note in restarted.drain_notes())
