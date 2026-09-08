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


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """Wilder's true range: the bar's own span, or its gap from the last close.

    The first bar has no previous close and so has no gap to measure; its true
    range is simply its high minus its low. That is the standard convention and
    it matters here only for one bar out of a quarter of a million.
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise DomainError("true range needs highs, lows and closes of the same length")
    out: list[float] = []
    for i in range(len(highs)):
        span = float(highs[i]) - float(lows[i])
        if i == 0:
            out.append(span)
            continue
        previous = float(closes[i - 1])
        out.append(
            max(span, abs(float(highs[i]) - previous), abs(float(lows[i]) - previous))
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float]:
    """Average true range, Wilder-smoothed and seeded with a simple average.

    Same seeding as `ta.atr` on every chart: a stop or a sweep threshold written
    as "a tenth of an ATR" should mean the same distance here as it does on the
    screen the rule was written against.

    NaN until there are `period` bars to average, so a caller cannot place a
    stop at a volatility estimate that does not exist yet.
    """
    if period < 1:
        raise DomainError(f"ATR period must be at least 1, got {period}")
    ranges = true_range(highs, lows, closes)
    n = len(ranges)
    out = [float("nan")] * n
    if n < period:
        return out
    average = sum(ranges[:period]) / period
    out[period - 1] = average
    for i in range(period, n):
        average = (average * (period - 1) + ranges[i]) / period
        out[i] = average
    return out


class WilderAtr:
    """The same ATR as `atr()`, advanced one bar at a time.

    `atr()` needs the whole series in hand, which suits a study that precomputes
    it. A strategy that must run identically in a backtest and in a live loop
    cannot hold the series - it sees one bar and then the next - and a Wilder
    average is path-dependent, so recomputing it over a rolling window would
    give a *different number* rather than the same one more cheaply.

    So this carries the running average, and `test_liquidity_sweep.py` asserts
    bar for bar that it reproduces `atr()` over the same input. Two
    implementations are justified only while that test exists.

    `value` is `None` until `period` bars have been seen - the same statement
    `atr()`'s NaN makes, in the form a caller has to handle.
    """

    __slots__ = ("_average", "_period", "_previous_close", "_seed")

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise DomainError(f"ATR period must be at least 1, got {period}")
        self._period = period
        self._seed: list[float] = []
        self._average: float | None = None
        self._previous_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        """Fold one closed bar in and return the ATR as of that bar."""
        span = high - low
        if self._previous_close is None:
            span_true = span
        else:
            span_true = max(
                span, abs(high - self._previous_close), abs(low - self._previous_close)
            )
        self._previous_close = close

        if self._average is None:
            self._seed.append(span_true)
            if len(self._seed) == self._period:
                self._average = sum(self._seed) / self._period
            return self._average
        self._average = (self._average * (self._period - 1) + span_true) / self._period
        return self._average

    @property
    def value(self) -> float | None:
        return self._average
