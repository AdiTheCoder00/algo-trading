"""EMA/MACD, checked against pandas and against `tools/macd_telegram_alert`.

The whole point of this module is that a crossover here and an alert from the
standalone tool must never disagree. `adjust=False` is the one setting that
makes that true — pandas' default (`adjust=True`) is a weighted average with a
growing denominator that only *converges* to the recursive EMA, and differs from
it materially over the first couple hundred bars. Every numeric test below
either reproduces the alert tool's own reference values or cross-checks against
pandas with `adjust=False` explicitly selected, so a silent switch back to the
pandas default would be caught here.
"""

from __future__ import annotations

import random

import pytest

from algo.core.errors import DomainError
from algo.pricing.indicators import Macd, ema, macd, warmup_bars

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
