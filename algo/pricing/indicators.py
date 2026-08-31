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
