"""Indicators, checked against pandas and against `tools/macd_telegram_alert`.

The whole point of this module is that a crossover here and an alert from the
standalone tool must never disagree. `adjust=False` is the one setting that
makes that true — pandas' default (`adjust=True`) is a weighted average with a
growing denominator that only *converges* to the recursive EMA, and differs from
it materially over the first couple hundred bars. Every numeric test below
either reproduces the alert tool's own reference values or cross-checks against
pandas with `adjust=False` explicitly selected, so a silent switch back to the
pandas default would be caught here.

`rsi` and `wma` are cross-checked differently, because pandas has no RSI and its
`ewm` cannot express Wilder's SMA-seeded `1 / n` smoothing without becoming a
restatement of the implementation. `_wilder_rsi` below is written from Wilder's
own recurrence instead - a second derivation, not a second copy - and the WMA is
checked against its weights arithmetic directly.
"""

from __future__ import annotations

import math
import random

import pytest

from algo.core.errors import DomainError
from algo.pricing.indicators import (
    Macd,
    ema,
    hilega_milega,
    hilega_milega_warmup_bars,
    macd,
    rsi,
    warmup_bars,
    wma,
)

pd = pytest.importorskip("pandas")


def _random_walk(n: int, *, seed: int = 1, start: float = 4400.0) -> list[float]:
    rng = random.Random(seed)
    values = [start]
    for _ in range(n - 1):
        values.append(values[-1] + rng.gauss(0, 3))
    return values


class TestEmaMatchesPandasAdjustFalse:
    def test_a_random_walk_matches_exactly(self) -> None:
        values = _random_walk(500)
        mine = ema(values, 12)
        theirs = pd.Series(values).ewm(span=12, adjust=False).mean().tolist()

        assert mine == pytest.approx(theirs, abs=1e-9)

    def test_a_short_series_still_matches(self) -> None:
        values = _random_walk(3, seed=7)
        mine = ema(values, 12)
        theirs = pd.Series(values).ewm(span=12, adjust=False).mean().tolist()

        assert mine == pytest.approx(theirs, abs=1e-9)

    def test_it_would_NOT_match_the_pandas_default(self) -> None:
        """Guards the premise. If this ever passed, `adjust=True` and
        `adjust=False` would have become numerically indistinguishable, which
        would mean the compatibility claim above needs re-examining."""
        values = _random_walk(30, seed=3)
        mine = ema(values, 12)
        theirs_default = pd.Series(values).ewm(span=12).mean().tolist()

        assert mine != pytest.approx(theirs_default, abs=1e-9)

    def test_seeded_with_the_first_value_not_an_sma(self) -> None:
        assert ema([10.0, 20.0, 30.0], 5)[0] == 10.0

    def test_a_flat_series_stays_flat(self) -> None:
        assert ema([100.0] * 10, 12) == pytest.approx([100.0] * 10)


class TestEmaValidation:
    def test_period_below_one_is_refused(self) -> None:
        with pytest.raises(DomainError, match="at least 1"):
            ema([1.0, 2.0], 0)

    def test_empty_input_returns_empty_output(self) -> None:
        assert ema([], 12) == []

    def test_a_single_value_returns_itself(self) -> None:
        assert ema([42.0], 12) == [42.0]


class TestMacdMatchesPandas:
    def test_macd_line_matches(self) -> None:
        values = _random_walk(800, seed=11)
        mine = macd(values, fast=12, slow=26, signal=9)

        s = pd.Series(values)
        theirs_macd = (
            s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
        )

        assert mine.macd == pytest.approx(theirs_macd.tolist(), abs=1e-9)

    def test_signal_line_matches(self) -> None:
        values = _random_walk(800, seed=11)
        mine = macd(values, fast=12, slow=26, signal=9)

        s = pd.Series(values)
        theirs_macd = (
            s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
        )
        theirs_signal = theirs_macd.ewm(span=9, adjust=False).mean()

        assert mine.signal == pytest.approx(theirs_signal.tolist(), abs=1e-9)

    def test_histogram_is_macd_minus_signal(self) -> None:
        values = _random_walk(200, seed=4)
        mine = macd(values)

        for m, s, h in zip(mine.macd, mine.signal, mine.histogram, strict=True):
            assert h == pytest.approx(m - s, abs=1e-12)

    def test_default_parameters_are_12_26_9(self) -> None:
        """The alert tool's own default, and what makes a crossover here mean
        the same thing as an alert there without either side passing periods."""
        values = _random_walk(200, seed=2)

        assert macd(values).macd == pytest.approx(
            macd(values, fast=12, slow=26, signal=9).macd
        )


class TestMacdValidation:
    def test_fast_must_be_shorter_than_slow(self) -> None:
        with pytest.raises(DomainError, match="fast period must be shorter"):
            macd([1.0] * 40, fast=26, slow=12, signal=9)

    def test_equal_periods_are_refused(self) -> None:
        with pytest.raises(DomainError):
            macd([1.0] * 40, fast=12, slow=12, signal=9)

    def test_empty_input_produces_empty_output(self) -> None:
        result = macd([])

        assert result.macd == []
        assert result.signal == []
        assert result.histogram == []


class TestCrossovers:
    """The alert tool's own rule: `<= 0` then `> 0` is bullish, `>= 0` then
    `< 0` is bearish. `<=`/`>=` on the *previous* bar, not the current one — a
    histogram sitting exactly at zero and then rising must count."""

    def _macd_from_histogram(self, histogram: list[float]) -> Macd:
        return Macd(macd=histogram, signal=[0.0] * len(histogram), histogram=histogram)

    def test_a_clean_upward_cross_is_detected(self) -> None:
        result = self._macd_from_histogram([-2.0, -1.0, 1.0, 2.0])

        assert result.crossed_up(2) is True
        assert result.crossed_down(2) is False

    def test_a_clean_downward_cross_is_detected(self) -> None:
        result = self._macd_from_histogram([2.0, 1.0, -1.0, -2.0])

        assert result.crossed_down(2) is True
        assert result.crossed_up(2) is False

    def test_sitting_at_zero_then_rising_counts_as_a_cross(self) -> None:
        """The `<=` in the alert tool's own rule, not `<`."""
        result = self._macd_from_histogram([-1.0, 0.0, 1.0])

        assert result.crossed_up(2) is True

    def test_sitting_at_zero_then_falling_counts_as_a_cross(self) -> None:
        result = self._macd_from_histogram([1.0, 0.0, -1.0])

        assert result.crossed_down(2) is True

    def test_staying_positive_is_not_a_cross(self) -> None:
        result = self._macd_from_histogram([1.0, 2.0, 3.0])

        assert result.crossed_up(2) is False
        assert result.crossed_down(2) is False

    def test_the_default_index_is_the_newest_bar(self) -> None:
        result = self._macd_from_histogram([-2.0, -1.0, 1.0, 2.0])

        assert result.crossed_up() is False  # 1.0 -> 2.0, no cross
        assert result.crossed_up(-2) is True  # -1.0 -> 1.0

    def test_fewer_than_two_points_never_crosses(self) -> None:
        assert self._macd_from_histogram([1.0]).crossed_up(0) is False
        assert self._macd_from_histogram([]).crossed_up(0) is False

    def test_the_first_point_has_no_predecessor(self) -> None:
        result = self._macd_from_histogram([1.0, 2.0, 3.0])

        assert result.crossed_up(0) is False
        assert result.crossed_down(0) is False

    def test_the_first_point_has_no_predecessor_via_its_negative_index_either(
        self,
    ) -> None:
        """`-len(histogram)` names the same first element `0` does. Without
        normalizing both forms the same way, this used to evaluate
        `histogram[-len(histogram) - 1]` - out of range - and raise
        `IndexError` instead of returning `False` the way `crossed_up(0)`
        does for an equivalent index."""
        result = self._macd_from_histogram([1.0, 2.0, 3.0])

        assert result.crossed_up(-3) is False
        assert result.crossed_down(-3) is False


class TestWarmup:
    def test_default_matches_the_alert_tools_formula(self) -> None:
        """slow + signal + 2, the alert tool's own `warmup_needed`."""
        assert warmup_bars() == 26 + 9 + 2

    def test_it_follows_custom_periods(self) -> None:
        assert warmup_bars(slow=50, signal=20) == 50 + 20 + 2


def _wilder_rsi(values: list[float], period: int) -> list[float]:
    """An independent Wilder RSI, written from the definition rather than from
    `indicators.rsi`, so this is a real cross-check and not a restatement.

    Uses the recurrence in the form Wilder's book states it —
    `(new + (n - 1) * previous) / n` — rather than the `alpha * new +
    (1 - alpha) * previous` form the module uses. They are algebraically the
    same, which is the point: if the module quietly switched to the
    `2 / (n + 1)` EMA weight, the two would stop agreeing.
    """
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    out = [math.nan] * len(values)
    if len(values) <= period:
        return out
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    out[period] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    for i in range(period, len(changes)):
        average_gain = (gains[i] + (period - 1) * average_gain) / period
        average_loss = (losses[i] + (period - 1) * average_loss) / period
        out[i + 1] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return out


class TestRsi:
    def test_it_matches_wilders_own_recurrence(self) -> None:
        values = _random_walk(400, seed=11)

        assert rsi(values, 9)[9:] == pytest.approx(_wilder_rsi(values, 9)[9:], abs=1e-9)

    def test_it_is_undefined_until_the_period_is_filled(self) -> None:
        """`nan`, not 50 — the padding question `indicators.py` argues out."""
        line = rsi(_random_walk(40, seed=12), 9)

        assert all(math.isnan(v) for v in line[:9])
        assert not math.isnan(line[9])

    def test_it_returns_one_value_per_bar(self) -> None:
        assert len(rsi(_random_walk(40, seed=13), 9)) == 40

    def test_a_series_shorter_than_the_period_is_all_undefined(self) -> None:
        line = rsi([1.0, 2.0, 3.0], 9)

        assert len(line) == 3
        assert all(math.isnan(v) for v in line)

    def test_it_would_NOT_match_the_two_over_n_plus_one_weight(self) -> None:
        """Guards the premise. Wilder smoothing is `1 / n`, not the
        `2 / (n + 1)` weight `ema` uses. If these ever agreed, the claim that
        this matches Pine's `ta.rsi` would need re-examining."""
        values = _random_walk(200, seed=14)
        changes = [values[i] - values[i - 1] for i in range(1, len(values))]
        gains = [max(c, 0.0) for c in changes]
        losses = [max(-c, 0.0) for c in changes]
        alpha = 2.0 / (9 + 1)
        gain, loss = sum(gains[:9]) / 9, sum(losses[:9]) / 9
        wrong = []
        for i in range(9, len(changes)):
            gain = alpha * gains[i] + (1 - alpha) * gain
            loss = alpha * losses[i] + (1 - alpha) * loss
            wrong.append(100.0 - 100.0 / (1.0 + gain / loss))

        assert rsi(values, 9)[10:] != pytest.approx(wrong, abs=1e-9)

    def test_an_unbroken_rally_reads_100(self) -> None:
        """No losing bar means no denominator. Pine states this case, so does
        `rsi_from_averages`, and neither may return `nan` for it."""
        assert rsi([float(i) for i in range(30)], 9)[-1] == 100.0

    def test_an_unbroken_selloff_reads_0(self) -> None:
        assert rsi([float(30 - i) for i in range(30)], 9)[-1] == 0.0

    def test_a_period_below_one_is_refused(self) -> None:
        with pytest.raises(DomainError, match="at least 1"):
            rsi([1.0, 2.0], 0)


class TestWma:
    def test_it_weights_the_newest_bar_heaviest(self) -> None:
        """Pine's `ta.wma`: the newest bar carries the full `period`, so a
        three-bar window is (1*1 + 2*2 + 3*3) / 6, not a plain mean."""
        assert wma([1.0, 2.0, 3.0], 3)[-1] == pytest.approx((1 + 4 + 9) / 6.0)

    def test_a_flat_series_averages_to_itself(self) -> None:
        assert wma([7.0] * 10, 4)[-1] == pytest.approx(7.0)

    def test_it_is_undefined_until_the_window_is_full(self) -> None:
        line = wma([1.0, 2.0, 3.0, 4.0], 3)

        assert math.isnan(line[0])
        assert math.isnan(line[1])
        assert not math.isnan(line[2])

    def test_an_undefined_input_is_not_laundered_into_a_defined_average(self) -> None:
        """One `nan` in a window makes that window `nan` too. Without it, the
        leading `nan`s of an RSI would silently become a number the moment a
        WMA was taken over them."""
        line = wma([math.nan, 2.0, 3.0, 4.0, 5.0], 3)

        assert math.isnan(line[2])
        assert not math.isnan(line[3])

    def test_it_returns_one_value_per_bar(self) -> None:
        assert len(wma(_random_walk(40, seed=15), 21)) == 40

    def test_a_period_below_one_is_refused(self) -> None:
        with pytest.raises(DomainError, match="at least 1"):
            wma([1.0, 2.0], 0)


class TestHilegaMilega:
    def test_the_averages_are_taken_over_the_rsi_not_over_price(self) -> None:
        """The whole premise of the setup. An EMA of price would sit near 4400;
        an EMA of an RSI cannot leave 0..100."""
        lines = hilega_milega(_random_walk(300, seed=16))

        assert 0.0 <= lines.trend[-1] <= 100.0
        assert 0.0 <= lines.weighted[-1] <= 100.0

    def test_neither_average_is_defined_where_the_rsi_is_not(self) -> None:
        lines = hilega_milega(_random_walk(300, seed=17))

        for i, strength in enumerate(lines.strength):
            if math.isnan(strength):
                assert math.isnan(lines.trend[i])
                assert math.isnan(lines.weighted[i])

    def test_the_trend_ema_is_seeded_from_the_first_real_rsi_reading(self) -> None:
        """Not from a `nan` and not from bar 0 — a recursive average seeded
        with `nan` stays `nan` for the rest of the series."""
        lines = hilega_milega(_random_walk(300, seed=18))

        assert lines.trend[9] == pytest.approx(lines.strength[9])
        assert not any(math.isnan(v) for v in lines.trend[9:])

    def test_every_line_has_one_value_per_bar(self) -> None:
        lines = hilega_milega(_random_walk(120, seed=19))

        assert len(lines.strength) == 120
        assert len(lines.trend) == 120
        assert len(lines.weighted) == 120

    def test_a_series_too_short_for_the_rsi_produces_three_blank_lines(self) -> None:
        lines = hilega_milega([1.0, 2.0, 3.0])

        assert all(math.isnan(v) for v in lines.strength)
        assert all(math.isnan(v) for v in lines.trend)
        assert all(math.isnan(v) for v in lines.weighted)

    def test_an_empty_series_produces_three_empty_lines(self) -> None:
        lines = hilega_milega([])

        assert lines.strength == []
        assert lines.trend == []
        assert lines.weighted == []


class TestHilegaMilegaWarmup:
    def test_the_default_covers_every_term_in_the_stack(self) -> None:
        assert hilega_milega_warmup_bars() == 9 + 21 + 3 + 2

    def test_it_follows_custom_periods(self) -> None:
        assert (
            hilega_milega_warmup_bars(rsi_period=14, trend_period=5, weighted_period=50)
            == 14 + 50 + 5 + 2
        )

    def test_all_three_lines_are_defined_by_the_time_it_elapses(self) -> None:
        """The figure has to be at least large enough, or a strategy trusting it
        would read a `nan` on its first post-warmup bar."""
        lines = hilega_milega(_random_walk(200, seed=20))
        warmup = hilega_milega_warmup_bars()

        assert not math.isnan(lines.strength[warmup - 1])
        assert not math.isnan(lines.trend[warmup - 1])
        assert not math.isnan(lines.weighted[warmup - 1])
