"""Indicators. Currently EMA and MACD, computed to match what already exists.

`tools/macd_telegram_alert` has been watching MACD crossovers for a while and
states its own definition precisely: EMA(12) - EMA(26), signal EMA(9) of that,
**`adjust=False`** so the values match TradingView. This reimplements the same
arithmetic against the engine's bar window rather than pandas, so a signal here
and an alert there cannot disagree about what a crossover is.

`adjust=False` is the whole of the compatibility question. Pandas' default
(`adjust=True`) computes a weighted average with a growing denominator, which
converges to the recursive form but is **not equal to it** early in the series -
and TradingView, MT4/5 and every broker platform use the recursive form. Two
tools disagreeing on the first few hundred bars of a warmup is exactly the kind
of difference nobody notices until a signal fires on one and not the other.

Floats, deliberately. These feed a comparison - is the histogram above or below
zero - not a money calculation. `Decimal` would buy no accuracy in an
exponential average and `chain_greeks` already sets the precedent for the same
reason: the number selects, the price stays a `Decimal`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from algo.core.errors import DomainError


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average, recursive form (`adjust=False`).

    Seeded with the first value rather than with an SMA of the first `period`.
    That is what the alert tool does, what pandas does with `adjust=False`, and
    what MT5 does; seeding differently shifts every subsequent value.
    """
    if period < 1:
        raise DomainError(f"EMA period must be at least 1, got {period}")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


@dataclass(frozen=True, slots=True)
class Macd:
    """MACD, its signal line, and the histogram, one value per input bar."""

    macd: list[float]
    signal: list[float]
    histogram: list[float]

    def crossed_up(self, index: int = -1) -> bool:
        """Histogram moved from at-or-below zero to above it, at `index`.

        The same test the alert tool applies: `<= 0` then `> 0`. Using `<=`
        rather than `<` means a histogram sitting exactly at zero and then
        rising counts as a crossing, which matters more often than it sounds on
        a five-minute chart where flat stretches are common.
        """
        return self._crossed(index, up=True)

    def crossed_down(self, index: int = -1) -> bool:
        return self._crossed(index, up=False)

    def _crossed(self, index: int, *, up: bool) -> bool:
        if len(self.histogram) < 2:
            return False
        current = self.histogram[index]
        # `index == 0` and `index == -len(self.histogram)` both name the first
        # element - either form must return False rather than wrapping to
        # `histogram[-1]` (a same-index "previous") or raising `IndexError` on
        # the negative form, which `index != 0` alone let through.
        is_first = index % len(self.histogram) == 0
        previous = self.histogram[index - 1] if not is_first else None
        if previous is None:
            return False
        if up:
            return previous <= 0.0 < current
        return previous >= 0.0 > current


def macd(
    values: Sequence[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Macd:
    """MACD(12, 26, 9) by default - the same parameters the alert tool uses."""
    if fast >= slow:
        raise DomainError(
            f"the fast period must be shorter than the slow one, got {fast} and {slow}"
        )
    if not values:
        return Macd(macd=[], signal=[], histogram=[])
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line = [f - s for f, s in zip(fast_ema, slow_ema, strict=True)]
    signal_line = ema(line, signal)
    histogram = [m - s for m, s in zip(line, signal_line, strict=True)]
    return Macd(macd=line, signal=signal_line, histogram=histogram)


def warmup_bars(*, slow: int = 26, signal: int = 9) -> int:
    """Bars needed before a crossover means anything.

    The same figure the alert tool uses: the slow EMA and the signal EMA both
    need room to settle, and two closed bars are compared. A recursive EMA is
    never *exactly* settled - seeding with the first value leaves an error that
    decays rather than vanishing - so this is the point past which the residue
    is smaller than a tick, not a point of exactness.
    """
    return slow + signal + 2


@dataclass(frozen=True, slots=True)
class Bollinger:
    """Bollinger bands, one value per input bar.

    The first `period - 1` entries are `None` rather than an average of
    whatever bars exist so far. An SMA of three closes is not a warming-up
    20-period SMA, it is a different statistic, and letting it stand in would
    put a band on the chart during exactly the stretch where nothing should be
    traded.
    """

    middle: list[float | None]
    upper: list[float | None]
    lower: list[float | None]

    def width_at(self, index: int = -1) -> float | None:
        """Upper minus lower, or `None` before the bands exist.

        Bandwidth is the squeeze measure - the thing that separates "price is
        stretched" from "price has been quiet and is about to not be".
        """
        upper, lower = self.upper[index], self.lower[index]
        if upper is None or lower is None:
            return None
        return upper - lower


def bollinger(
    values: Sequence[float],
    *,
    period: int = 20,
    num_stdev: float = 2.0,
) -> Bollinger:
    """Bollinger bands over a simple moving average.

    POPULATION standard deviation (divide by N), not the sample form (N - 1).
    That is what MT5's `iBands` computes and what TradingView's `ta.stdev`
    defaults to, and the two differ by `sqrt(N / (N - 1))` - about 2.6% of the
    band's half-width at the default 20. Small, but it is the difference
    between a touch and a near-miss on exactly the bars this indicator exists
    to flag, so it is pinned here rather than left to whichever formula came to
    hand. Same reasoning as `ema()` seeding with the first value.
    """
    if period < 2:
        raise DomainError(f"Bollinger period must be at least 2, got {period}")
    if num_stdev <= 0:
        raise DomainError(f"num_stdev must be positive, got {num_stdev}")

    middle: list[float | None] = []
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < period:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            continue
        window = [float(v) for v in values[i + 1 - period : i + 1]]
        mean = sum(window) / period
        variance = sum((v - mean) ** 2 for v in window) / period
        sd = variance**0.5
        middle.append(mean)
        upper.append(mean + num_stdev * sd)
        lower.append(mean - num_stdev * sd)
    return Bollinger(middle=middle, upper=upper, lower=lower)


@dataclass(frozen=True, slots=True)
class FibPivots:
    """One session's pivot levels, in the Fibonacci form.

    Named for the setting rather than the author: TradingView's "Pivot Points
    Standard" indicator offers several types under one name, and "Fibonacci" is
    a *type* of that indicator, not a different one. The levels are the classic
    pivot `P` with the ratios laid over the previous session's range instead of
    the classic doubling formula.
    """

    p: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float

    def levels(self) -> tuple[float, ...]:
        """Every line, ascending. Order is by price, not by name.

        A strategy asking "did this bar cross a pivot line" does not care which
        line it was, and sorting here means the caller never has to assume that
        `s3 < s2 < s1 < p`. It is true for a positive range, but it is true
        because of the arithmetic, not by construction.
        """
        return tuple(sorted((self.s3, self.s2, self.s1, self.p, self.r1, self.r2, self.r3)))

    def nearest(self, price: float) -> tuple[str, float]:
        """The line closest to `price`, as `(name, level)` - for the log line."""
        named = (
            ("S3", self.s3), ("S2", self.s2), ("S1", self.s1), ("P", self.p),
            ("R1", self.r1), ("R2", self.r2), ("R3", self.r3),
        )
        return min(named, key=lambda item: abs(item[1] - price))


#: The three ratios TradingView's Fibonacci pivots lay over the previous
#: session's range. 1.0 rather than 1.618 for the third: the indicator's default
#: shows three levels a side, and the third of those is the full range.
FIB_RATIOS = (0.382, 0.618, 1.000)


def fib_pivots(*, high: float, low: float, close: float) -> FibPivots:
    """Fibonacci pivots from one completed session's high, low and close.

    `P = (H + L + C) / 3` - the same pivot every type of this indicator shares -
    then each level is `P +/- ratio * (H - L)`. The range is the PREVIOUS
    session's, which is what makes these levels usable: they are fixed before
    the session they are drawn on opens, so a strategy reading them is reading
    something it could genuinely have known.

    A zero-range session (`high == low`, which real data does produce on a
    holiday stub) collapses every level onto `P`. That is arithmetically correct
    and is left to say so rather than being special-cased into an error - a
    strategy that requires a crossing simply will not find one.
    """
    if low > high:
        raise DomainError(f"pivot low {low} is above pivot high {high}")
    if not (low <= close <= high):
        raise DomainError(f"pivot close {close} is outside [{low}, {high}]")
    pivot = (high + low + close) / 3.0
    span = high - low
    r1, r2, r3 = (pivot + ratio * span for ratio in FIB_RATIOS)
    s1, s2, s3 = (pivot - ratio * span for ratio in FIB_RATIOS)
    return FibPivots(p=pivot, r1=r1, r2=r2, r3=r3, s1=s1, s2=s2, s3=s3)
