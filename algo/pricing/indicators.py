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


def rsi(values: Sequence[float], period: int = 14) -> list[float]:
    """Wilder's RSI, seeded with a simple average - TradingView's `ta.rsi`.

    Two seeding conventions exist and they do not agree. TradingView computes
    `ta.rma`, which averages the first `period` changes arithmetically and then
    smooths recursively; a plain recursive EMA seeded on the first change gives
    visibly different values for hundreds of bars. The strategy this feeds
    compares RSI against 40, 60, 25 and 75 - fixed levels, where a systematic
    offset does not average out, it changes which bars are signals. So this
    matches TradingView rather than being merely "an RSI".

    The first `period` entries are `float('nan')`: there is no RSI before there
    are `period` changes to average, and returning 50 or 0 there would let a
    caller trade a value that does not exist. Callers must skip NaN.
    """
    if period < 1:
        raise DomainError(f"RSI period must be at least 1, got {period}")
    n = len(values)
    out = [float("nan")] * n
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = float(values[i]) - float(values[i - 1])
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = float(values[i]) - float(values[i - 1])
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    """RSI from the two smoothed averages.

    `avg_loss == 0` is not a division by zero to be guarded with an epsilon: it
    means no down move in the window, which is exactly RSI 100. The mirrored
    case - no up move - is RSI 0, and both flat is 50 rather than undefined.
    """
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


@dataclass(frozen=True, slots=True)
class StochRsi:
    """Stochastic RSI: the RSI's own position in its recent range, smoothed."""

    k: list[float]
    d: list[float]
    #: The unsmoothed stochastic of the RSI, before the %K smoothing. Carried
    #: because it is what a reader checking against a chart's source sees.
    raw: list[float]


def stoch_rsi(
    values: Sequence[float],
    *,
    rsi_period: int = 14,
    stoch_period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> StochRsi:
    """TradingView's Stochastic RSI (14, 14, 3, 3).

    The chain is exactly the one in TradingView's built-in script: RSI, then
    `ta.stoch` of that RSI against its own high and low over `stoch_period`,
    then `%K = SMA(stoch, smooth_k)` and `%D = SMA(%K, smooth_d)`. The common
    mistake is to smooth once and call the result %K and the raw stochastic %D;
    that produces two lines that cross at different bars from the chart's.

    A flat RSI window - `highest == lowest`, which happens on a quiet
    five-minute chart more often than it sounds - has no defined position in a
    zero-width range. That is 50, the midpoint, not 0 and not 100, either of
    which would read as an extreme that never happened.
    """
    if stoch_period < 1 or smooth_k < 1 or smooth_d < 1:
        raise DomainError("stochastic RSI periods must all be at least 1")

    base = rsi(values, rsi_period)
    n = len(base)
    raw = [float("nan")] * n
    for i in range(n):
        if i + 1 < stoch_period:
            continue
        window = base[i - stoch_period + 1 : i + 1]
        if any(v != v for v in window):  # NaN in the warmup
            continue
        low = min(window)
        high = max(window)
        raw[i] = 50.0 if high == low else (base[i] - low) / (high - low) * 100.0

    k = _sma(raw, smooth_k)
    d = _sma(k, smooth_d)
    return StochRsi(k=k, d=d, raw=raw)


def _sma(values: Sequence[float], period: int) -> list[float]:
    """Simple moving average that propagates NaN rather than averaging around it.

    A window containing a warmup NaN has no average; filling it from the
    non-NaN members would put a value on a bar where the indicator does not yet
    exist, which is the same class of error as look-ahead.
    """
    n = len(values)
    out = [float("nan")] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v != v for v in window):
            continue
        out[i] = sum(window) / period
    return out


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """Wilder's true range: the bar's own span, or its gap from the last close.

    The first bar has no previous close and so has no gap to measure; its true
    range is simply its high minus its low. That is the standard convention and
    it matters here only for one bar out of ninety-five thousand.
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

    Same seeding as `rsi` and for the same reason: this is the `ta.atr` every
    chart draws, and a stop placed at "two ATRs" should mean the same distance
    here as it does on the screen the rule was written against.

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
