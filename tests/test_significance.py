"""Bootstrap intervals and permutation tests.

This is machinery whose whole purpose is to say "no" to a flattering number, so
the tests are mostly about it saying no when it should.

Three properties carry the weight.

**It is deterministic.** A confidence interval that moves between runs is not
evidence of anything. Every draw is seeded, and two runs with the same seed must
produce identical bounds - the same rule `algo/data/synthetic.py` follows.

**A p-value is never zero.** No finite number of permutations can support that
claim, and a test that reports it would be overstating its own evidence in the
one direction that matters.

**A permuted series is still a price series.** The permutation destroys serial
structure and must destroy nothing else: same timestamps, same bar count, same
return distribution, and every bar still satisfying high >= open/close >= low.
A permutation that produced impossible bars would be testing the strategy
against something no venue ever printed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from algo.core.bar import M30, Bar
from algo.core.errors import DataError
from algo.reporting.significance import (
    BootstrapResult,
    percentile,
    permutation_result,
    permuted_bars,
    stationary_bootstrap,
)

FINE = Decimal("0.00000001")
TICK = Decimal("0.01")


def _bars(closes: list[str], *, spread: str = "0.5") -> list[Bar]:
    """A series whose every bar has the same shape around its close."""
    start = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
    out: list[Bar] = []
    for index, close in enumerate(closes):
        value = Decimal(close)
        width = Decimal(spread)
        out.append(
            Bar(
                ts=start + timedelta(minutes=30 * index),
                timeframe=M30,
                open=value - width,
                high=value + width,
                low=value - width * 2,
                close=value,
            )
        )
    return out


def _walk(n: int, *, start: Decimal = Decimal("4000")) -> list[Bar]:
    """A deterministic zig-zag, so returns vary without needing an RNG here."""
    closes: list[str] = []
    price = start
    for index in range(n):
        price = price * (Decimal("1.004") if index % 3 else Decimal("0.997"))
        closes.append(str(price.quantize(TICK)))
    return _bars(closes)


class TestPercentile:
    def test_the_median_of_an_odd_sample(self) -> None:
        values = [Decimal(v) for v in ("1", "2", "3", "4", "5")]
        assert percentile(values, Decimal("50")) == Decimal("3")

    def test_the_extremes(self) -> None:
        values = [Decimal(v) for v in ("1", "2", "3", "4", "5")]
        assert percentile(values, Decimal("0")) == Decimal("1")
        assert percentile(values, Decimal("100")) == Decimal("5")

    def test_it_returns_a_value_the_sample_contained(self) -> None:
        """Nearest rank, not interpolated - so a money figure stays one rather
        than becoming a weighted average of two figures."""
        values = [Decimal("10"), Decimal("20")]
        assert percentile(values, Decimal("75")) in values

    def test_an_empty_sample_is_refused(self) -> None:
        with pytest.raises(DataError, match="percentile of nothing"):
            percentile([], Decimal("50"))

    def test_a_percentile_outside_the_range_is_refused(self) -> None:
        with pytest.raises(DataError, match="between 0 and 100"):
            percentile([Decimal("1")], Decimal("101"))


class TestTheBootstrapIsDeterministic:
    def test_the_same_seed_gives_the_same_interval(self) -> None:
        values = [Decimal(v) for v in ("120", "-45", "300", "-80", "15", "-200", "95")]
        first = stationary_bootstrap(values, resamples=500, seed=7)
        second = stationary_bootstrap(values, resamples=500, seed=7)
        assert (first.lower, first.upper) == (second.lower, second.upper)

    def test_a_different_seed_moves_it(self) -> None:
        values = [Decimal(v) for v in ("120", "-45", "300", "-80", "15", "-200", "95")]
        first = stationary_bootstrap(values, resamples=500, seed=7)
        second = stationary_bootstrap(values, resamples=500, seed=8)
        assert (first.lower, first.upper) != (second.lower, second.upper)


class TestWhatTheIntervalSays:
    def test_the_observed_total_is_the_real_sum(self) -> None:
        """Not a resampled estimate of it. The interval surrounds the number
        that was actually reported, or it is surrounding something else."""
        values = [Decimal("10"), Decimal("20"), Decimal("30")]
        assert stationary_bootstrap(values, resamples=200).observed == Decimal("60")

    def test_an_all_winning_series_excludes_zero(self) -> None:
        values = [Decimal("100")] * 40
        result = stationary_bootstrap(values, resamples=500, seed=1)
        assert result.excludes_zero
        assert result.lower > 0
        assert result.resamples_at_or_below_zero == 0

    def test_an_all_losing_series_excludes_zero_on_the_other_side(self) -> None:
        values = [Decimal("-100")] * 40
        result = stationary_bootstrap(values, resamples=500, seed=1)
        assert result.excludes_zero
        assert result.upper < 0
        assert result.loss_probability_pct == Decimal("100")

    def test_a_coin_flip_series_straddles_zero(self) -> None:
        """The case the whole module exists for: a total that looks like a
        result and is not distinguishable from noise."""
        values = [Decimal("500"), Decimal("-500")] * 25 + [Decimal("100")]
        result = stationary_bootstrap(values, resamples=1000, seed=3)
        assert not result.excludes_zero
        assert "STRADDLES ZERO" in result.summary()

    def test_the_interval_brackets_the_observed_total_when_it_is_typical(self) -> None:
        values = [Decimal("100")] * 40
        result = stationary_bootstrap(values, resamples=500, seed=1)
        assert result.lower <= result.observed <= result.upper

    def test_the_block_length_is_reported(self) -> None:
        """The interval's width depends on it, so it is part of the claim."""
        values = [Decimal("1")] * 100
        assert stationary_bootstrap(values, resamples=100).mean_block == Decimal("10")

    def test_an_explicit_block_length_is_honoured(self) -> None:
        values = [Decimal("1")] * 100
        result = stationary_bootstrap(values, resamples=100, mean_block=Decimal("25"))
        assert result.mean_block == Decimal("25")


class TestTheBootstrapRefusesNonsense:
    def test_an_empty_series(self) -> None:
        with pytest.raises(DataError, match="empty sequence"):
            stationary_bootstrap([])

    def test_zero_resamples(self) -> None:
        with pytest.raises(DataError, match="resamples must be positive"):
            stationary_bootstrap([Decimal("1")], resamples=0)

    def test_a_confidence_level_of_a_hundred_percent(self) -> None:
        with pytest.raises(DataError, match="between 0 and 100"):
            stationary_bootstrap([Decimal("1")], confidence_pct=Decimal("100"))

    def test_a_negative_block_length(self) -> None:
        with pytest.raises(DataError, match="mean_block must be positive"):
            stationary_bootstrap([Decimal("1")], mean_block=Decimal("-5"))


class TestThePermutationPValue:
    def test_a_result_no_null_reached_gets_the_smallest_possible_p(self) -> None:
        nulls = [Decimal(str(v)) for v in range(100)]
        result = permutation_result(Decimal("1000"), nulls)
        assert result.at_or_above_observed == 0
        assert result.p_value == Decimal(1) / Decimal(101)
        assert result.is_significant

    def test_it_is_never_zero(self) -> None:
        """No finite number of permutations can support p = 0."""
        nulls = [Decimal("-1")] * 10_000
        assert permutation_result(Decimal("1000000"), nulls).p_value > 0

    def test_a_result_every_null_beat_is_not_significant(self) -> None:
        nulls = [Decimal("1000")] * 99
        result = permutation_result(Decimal("0"), nulls)
        assert result.p_value == Decimal("1")
        assert not result.is_significant

    def test_a_middling_result_is_not_significant(self) -> None:
        nulls = [Decimal(str(v)) for v in range(100)]
        result = permutation_result(Decimal("50"), nulls)
        assert not result.is_significant
        assert "NOT significant" in result.summary()

    def test_the_null_summary_statistics(self) -> None:
        nulls = [Decimal("10"), Decimal("20"), Decimal("30")]
        result = permutation_result(Decimal("25"), nulls)
        assert result.null_mean == Decimal("20")
        assert result.null_median == Decimal("20")
        assert result.null_best == Decimal("30")
        assert result.at_or_above_observed == 1

    def test_an_even_sample_takes_the_midpoint_median(self) -> None:
        nulls = [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")]
        assert permutation_result(Decimal("0"), nulls).null_median == Decimal("25")

    def test_an_empty_null_is_refused(self) -> None:
        with pytest.raises(DataError, match="at least one null sample"):
            permutation_result(Decimal("1"), [])


class TestAPermutedSeriesIsStillAPriceSeries:
    def test_the_length_and_timestamps_are_unchanged(self) -> None:
        """The session calendar, the swap roll and the bar count all key off
        these. Changing them would test the strategy against a different
        market, not a shuffled one."""
        original = _walk(200)
        shuffled = permuted_bars(original, seed=1, tick=TICK)
        assert len(shuffled) == len(original)
        assert [b.ts for b in shuffled] == [b.ts for b in original]

    def test_the_first_bar_is_the_anchor_and_does_not_move(self) -> None:
        original = _walk(50)
        assert permuted_bars(original, seed=1, tick=TICK)[0] == original[0]

    def test_every_bar_is_internally_consistent(self) -> None:
        """Rounding three prices independently can otherwise produce a high
        below the open, which is not a bar any venue ever printed."""
        for bar in permuted_bars(_walk(300), seed=4, tick=TICK):
            assert bar.high >= bar.open
            assert bar.high >= bar.close
            assert bar.low <= bar.open
            assert bar.low <= bar.close

    def test_prices_stay_on_the_tick_grid(self) -> None:
        for bar in permuted_bars(_walk(200), seed=2, tick=TICK):
            for price in (bar.open, bar.high, bar.low, bar.close):
                assert price % TICK == 0

    def test_the_return_distribution_is_preserved(self) -> None:
        """The point of the null: same volatility, same fat tails, different
        order. Checked on a fine grid so quantisation is not the thing being
        measured."""
        original = _walk(200)
        shuffled = permuted_bars(original, seed=5, tick=FINE)

        def returns(bars: list[Bar]) -> list[Decimal]:
            return sorted(
                (later.close / earlier.close).quantize(Decimal("0.000001"))
                for earlier, later in pairwise(bars)
            )

        for real, permuted in zip(returns(original), returns(shuffled), strict=True):
            assert abs(real - permuted) < Decimal("0.00001")

    def test_the_order_actually_changes(self) -> None:
        original = _walk(200)
        shuffled = permuted_bars(original, seed=1, tick=TICK)
        assert [b.close for b in shuffled] != [b.close for b in original]

    def test_it_is_deterministic(self) -> None:
        original = _walk(100)
        first = permuted_bars(original, seed=9, tick=TICK)
        second = permuted_bars(original, seed=9, tick=TICK)
        assert [b.close for b in first] == [b.close for b in second]

    def test_a_different_seed_gives_a_different_series(self) -> None:
        original = _walk(100)
        first = permuted_bars(original, seed=9, tick=TICK)
        second = permuted_bars(original, seed=10, tick=TICK)
        assert [b.close for b in first] != [b.close for b in second]

    def test_too_few_bars_is_refused(self) -> None:
        with pytest.raises(DataError, match="at least 3 bars"):
            permuted_bars(_walk(2), seed=1, tick=TICK)

    def test_a_non_positive_tick_is_refused(self) -> None:
        with pytest.raises(DataError, match="tick must be positive"):
            permuted_bars(_walk(10), seed=1, tick=Decimal("0"))

    def test_a_non_positive_close_is_refused(self) -> None:
        """A ratio against zero is not a return, and silently producing one
        would put an infinity into the null distribution."""
        bars = _bars(["100", "0", "100"], spread="0")
        with pytest.raises(DataError, match="non-positive close"):
            permuted_bars(bars, seed=1, tick=TICK)


class TestTheSummariesSayWhatHappened:
    def test_a_straddling_interval_says_so_loudly(self) -> None:
        result = BootstrapResult(
            observed=Decimal("100"),
            lower=Decimal("-500"),
            upper=Decimal("700"),
            confidence_pct=Decimal("95"),
            resamples=1000,
            sample_size=50,
            mean_block=Decimal("7"),
            resamples_at_or_below_zero=380,
        )
        assert not result.excludes_zero
        assert "STRADDLES ZERO" in result.summary()
        assert result.loss_probability_pct == Decimal("38")

    def test_a_significant_permutation_says_so(self) -> None:
        nulls = [Decimal(str(v)) for v in range(200)]
        assert "does not account" in permutation_result(Decimal("500"), nulls).summary()
