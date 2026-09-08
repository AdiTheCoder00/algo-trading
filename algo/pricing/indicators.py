"""Indicators. EMA, WMA, RSI, MACD and the Hilega-Milega composite.

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

## RSI and WMA follow Pine, for the same reason the EMA follows the alert tool

`rsi` and `wma` arrived for the Hilega-Milega composite (D-151), which is
described entirely in terms of what TradingView draws. So they reproduce Pine's
`ta.rsi` and `ta.wma` exactly rather than picking a convention:

- **`rsi` uses Wilder's smoothing seeded with an SMA**, which is what `ta.rma`
  does - `alpha = 1 / period`, seeded with the mean of the first `period`
  changes. This is *not* the `2 / (period + 1)` weight `ema` uses, and it is not
  seeded with the first value either. Both differences are deliberate: an RSI
  seeded the way this module's EMA is seeded reads several points away from
  TradingView's for a long time, which is precisely the disagreement the EMA
  docstring above exists to prevent.
- **`wma` weights the newest bar heaviest**, `period` down to `1`, denominator
  `period * (period + 1) / 2`. Pine again.

## Undefined is `nan`, never a neutral-looking number

Wilder's average does not exist until `period` changes have been seen, and a
`period`-bar WMA does not exist until `period` bars have. Those leading slots
come back as `math.nan` rather than `50.0`, `0.0`, or a truncated list.

`50.0` would be a lie a caller could act on - it is the exact value the
Hilega-Milega rules test against, so a padded head would read as "perfectly
neutral strength" on bars where nothing has been measured at all. Truncating
instead would be worse in a different way: every caller would then have to
carry an offset to line a value back up with the bar that produced it, and one
caller getting that arithmetic wrong is an off-by-one in a trading signal.

`nan` fails in the only safe direction. Both `nan > x` and `nan < x` are
`False`, so a rule written against these lines declines to fire on a bar it
cannot evaluate, instead of firing on a fabricated one.
"""

from __future__ import annotations

import math
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


def wma(values: Sequence[float], period: int) -> list[float]:
    """Weighted moving average, Pine's `ta.wma`: newest bar weighted heaviest.

    Weights run `period` down to `1` over the window ending at each bar, divided
    by `period * (period + 1) / 2`. The first `period - 1` slots are `nan` -
    there is no window there yet - and a `nan` anywhere in a window makes that
    window's output `nan` too, so an undefined input can never be laundered into
    a defined-looking average.
    """
    if period < 1:
        raise DomainError(f"WMA period must be at least 1, got {period}")
    count = len(values)
    out = [math.nan] * count
    denominator = period * (period + 1) / 2.0
    for end in range(period - 1, count):
        window = [float(v) for v in values[end - period + 1 : end + 1]]
        if any(math.isnan(v) for v in window):
            continue
        out[end] = sum(v * (i + 1) for i, v in enumerate(window)) / denominator
    return out


def rsi_from_averages(average_gain: float, average_loss: float) -> float:
    """One RSI reading from a pair of Wilder averages. Pine's three-way form.

    Public because `HilegaMilega` computes the same averages incrementally, one
    bar at a time, and must turn them into a reading the identical way - a
    second copy of `100 - 100 / (1 + rs)` with its own edge cases is exactly the
    drift this module exists to prevent.

    Not `100 - 100 / (1 + rs)` alone: a window with no losing bar divides by
    zero, and a window with no winning bar is `0`, not `nan`. Pine states both
    cases explicitly and so does this.
    """
    if average_loss == 0.0:
        return 100.0
    if average_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def rsi(values: Sequence[float], period: int = 9) -> list[float]:
    """Wilder's RSI, Pine's `ta.rsi`. Default 9, not 14 - see `hilega_milega`.

    `nan` until `period` changes have been seen, then Wilder-smoothed
    (`alpha = 1 / period`) from an SMA seed. One value per input bar, so
    `rsi(closes)[i]` is the reading for `closes[i]` with no offset to carry.
    """
    if period < 1:
        raise DomainError(f"RSI period must be at least 1, got {period}")
    count = len(values)
    out = [math.nan] * count
    if count <= period:
        return out

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, count):
        delta = float(values[i]) - float(values[i - 1])
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # `gains[j]` is the change *into* bar `j + 1`, so the mean of the first
    # `period` of them is the average at bar `period` - the first bar Wilder's
    # own definition produces a number for.
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    out[period] = rsi_from_averages(average_gain, average_loss)

    alpha = 1.0 / period
    for i in range(period, count - 1):
        average_gain = alpha * gains[i] + (1.0 - alpha) * average_gain
        average_loss = alpha * losses[i] + (1.0 - alpha) * average_loss
        out[i + 1] = rsi_from_averages(average_gain, average_loss)
    return out


@dataclass(frozen=True, slots=True)
class HilegaMilegaLines:
    """The three lines the Hilega-Milega panel draws, one value per input bar.

    Named for what each one measures rather than for the colour it is drawn in,
    because the colours are a charting choice and the meanings are not:

    - `strength` - RSI(9) itself. The black line. Above 50 is the long half of
      the panel, below 50 the short half.
    - `trend` - EMA(3) *of the RSI*. The green line, drawn hugging the RSI. It
      is the RSI's own short-term direction, which is why it is the first thing
      to cross when a move is running out.
    - `weighted` - WMA(21) *of the RSI*. The red line, and the slow one. Its
      position relative to the RSI is the setup's main read.
    """

    strength: list[float]
    trend: list[float]
    weighted: list[float]


def hilega_milega(
    values: Sequence[float],
    *,
    rsi_period: int = 9,
    trend_period: int = 3,
    weighted_period: int = 21,
) -> HilegaMilegaLines:
    """RSI(9), plus an EMA(3) and a WMA(21) computed over that RSI.

    The defaults are the published ones: RSI shortened from 14 to 9, its
    overbought/oversold bands both moved to 50 (which is a drawing decision, so
    it lives in the strategy's rules rather than here), a 3-period EMA and a
    21-period WMA plotted on the RSI rather than on price.

    Both averages are taken over the RSI's **defined** tail and then padded back
    out with `nan`, never over a series with `nan` in it. Feeding the raw list
    to `ema` would poison every subsequent value - one `nan` in a recursive
    average is permanent - and it would also seed the EMA from a slot that has
    no reading. Padding after the fact means `trend[i]` and `weighted[i]` still
    describe `values[i]`, which is the whole point of the one-value-per-bar
    convention.
    """
    strength = rsi(values, rsi_period)
    first_defined = next((i for i, value in enumerate(strength) if not math.isnan(value)), None)
    if first_defined is None:
        blank = [math.nan] * len(values)
        return HilegaMilegaLines(strength=strength, trend=blank, weighted=list(blank))

    tail = strength[first_defined:]
    head = [math.nan] * first_defined
    return HilegaMilegaLines(
        strength=strength,
        trend=head + ema(tail, trend_period),
        weighted=head + wma(tail, weighted_period),
    )


def hilega_milega_warmup_bars(
    *, rsi_period: int = 9, trend_period: int = 3, weighted_period: int = 21
) -> int:
    """Bars needed before all three lines mean anything.

    Same shape of argument as `warmup_bars` above, added up over this stack:
    `rsi_period` bars before Wilder's average exists at all, `weighted_period`
    more before the slowest average over it does, `trend_period` for the
    fastest one to settle, and two closed bars so a rule can compare one against
    its predecessor. The EMA term is a settling allowance rather than a hard
    requirement - a recursive average is never exactly settled - which is the
    same admission `warmup_bars` makes about its own `signal` term.
    """
    return rsi_period + weighted_period + trend_period + 2
