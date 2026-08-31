"""Is the result distinguishable from luck?

`metrics.py` answers "what happened": net P&L, Sharpe, drawdown, win rate. It
refuses to print a ratio without its sample size, which is the right instinct,
but a sample size is not an answer. This module answers the next question, and
it is the one every result in this project has so far been unable to address:
**could a strategy with no edge have produced this?**

Two different questions live here, and conflating them is the usual mistake.

## The bootstrap asks how uncertain the number is

Given the trades that actually happened, how much would the total move if the
same process ran again? Resampling the realised trade sequence gives a
confidence interval around the reported net P&L. A run whose 95% interval
comfortably straddles zero is a run whose headline figure is not distinguishable
from noise *in its own terms*, before anyone asks whether the strategy has edge.

It is a **stationary bootstrap** (Politis-Romano), not an i.i.d. one, and the
difference matters here specifically. Trend-following trades are serially
dependent: a trending regime produces runs of winners and a choppy one produces
runs of losers. Resampling individual trades independently would break those
runs, understate the variance, and hand back an interval that is too narrow -
which is precisely the direction that flatters a result. Geometric block lengths
preserve the runs on average while still randomising the sequence.

## The permutation test asks whether the edge exists at all

A confidence interval cannot tell you that. A strategy can produce a tight,
zero-excluding interval purely by having been fitted to the one price path it
was tested on. So: destroy the thing the strategy claims to exploit, and see
whether it still earns.

`permuted_bars` shuffles the *order* of the bar-to-bar returns and rebuilds a
price series from them. The result has the same return distribution, the same
volatility, the same fat tails and the same bar shapes as the real market - and
no serial structure whatsoever. A momentum or breakout strategy has, by its own
account, nothing to trade on such a series. Running it over many such series
gives the null distribution: **what this strategy earns on a market where its
premise is false.** If the real result sits inside that distribution, the
premise is not evidenced.

This is a stronger test than shuffling trade order or randomising entry signs,
both of which leave the price path intact and therefore leave the fitted
relationship intact with it.

## What neither test can do

Neither corrects for the searching that produced the parameters in the first
place. A p-value computed on the winning cell of a grid is not the p-value of
the procedure that found it - `sweep.py`'s docstring makes the same point about
its own heatmap. Report the grid size alongside, and treat a single-hypothesis
p-value on a searched parameter as the optimistic bound it is.

Every figure is `Decimal`, and every draw comes from a seeded `random.Random`,
so two runs on two machines produce the same interval - the same rule
`algo/data/synthetic.py` follows and for the same reason.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from algo.core.bar import Bar
from algo.core.errors import DataError

#: Confidence level for the interval, unless a caller asks for another.
DEFAULT_CONFIDENCE_PCT = Decimal("95")

#: Resamples for the bootstrap. Cheap - no strategy is re-run - so this is set
#: where the interval's own sampling error stops mattering rather than where
#: the runtime starts to.
DEFAULT_RESAMPLES = 10_000

#: One-sided alpha the summaries call "significant". Stated as a constant
#: because a threshold chosen after seeing the p-value is not a threshold.
ALPHA = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """A confidence interval around a total, from resampling the sequence."""

    observed: Decimal
    lower: Decimal
    upper: Decimal
    confidence_pct: Decimal
    resamples: int
    sample_size: int
    mean_block: Decimal
    #: How many resampled totals came out at or below zero. Reported as a count
    #: rather than only as an interval bound because "3% of resamples lost
    #: money" is a sentence a person can act on.
    resamples_at_or_below_zero: int

    @property
    def excludes_zero(self) -> bool:
        """True when the whole interval is on one side of zero."""
        return (self.lower > 0 and self.upper > 0) or (self.lower < 0 and self.upper < 0)

    @property
    def loss_probability_pct(self) -> Decimal:
        return (
            Decimal(self.resamples_at_or_below_zero) / Decimal(self.resamples)
        ) * Decimal("100")

    def summary(self) -> str:
        verdict = (
            "excludes zero"
            if self.excludes_zero
            else "STRADDLES ZERO - the total is not distinguishable from noise"
        )
        return (
            f"  observed total        {self.observed:>16,.2f}\n"
            f"  {self.confidence_pct}% interval        "
            f"{self.lower:>16,.2f} to {self.upper:,.2f}\n"
            f"  verdict               {verdict}\n"
            f"  resamples losing      {self.resamples_at_or_below_zero} of "
            f"{self.resamples} ({self.loss_probability_pct:.1f}%)\n"
            f"  sample                {self.sample_size} trades, mean block "
            f"{self.mean_block:.1f}"
        )


@dataclass(frozen=True, slots=True)
class PermutationResult:
    """Where the real result sits in the distribution of no-edge results."""

    observed: Decimal
    permutations: int
    at_or_above_observed: int
    null_mean: Decimal
    null_median: Decimal
    null_p95: Decimal
    null_best: Decimal

    @property
    def p_value(self) -> Decimal:
        """One-sided, with the observed result counted in its own null.

        The `+1` on both sides is not a fudge: the observed series is itself one
        of the arrangements being tested, and omitting it allows a p-value of
        exactly zero, which claims more than any finite number of permutations
        can support (Davison & Hinkley).
        """
        return Decimal(self.at_or_above_observed + 1) / Decimal(self.permutations + 1)

    @property
    def is_significant(self) -> bool:
        return self.p_value <= ALPHA

    def summary(self) -> str:
        verdict = (
            f"p = {self.p_value:.4f} - the no-edge explanation does not account "
            "for this result"
            if self.is_significant
            else f"p = {self.p_value:.4f} - NOT significant. A strategy with no "
            "edge produces this on shuffled data often enough to explain it."
        )
        return (
            f"  observed              {self.observed:>16,.2f}\n"
            f"  null mean             {self.null_mean:>16,.2f}\n"
            f"  null median           {self.null_median:>16,.2f}\n"
            f"  null 95th percentile  {self.null_p95:>16,.2f}\n"
            f"  null best of {self.permutations:<8}{self.null_best:>16,.2f}\n"
            f"  beaten or matched by  {self.at_or_above_observed} of "
            f"{self.permutations} permutations\n"
            f"  verdict               {verdict}"
        )


def percentile(values: Sequence[Decimal], pct: Decimal) -> Decimal:
    """The `pct`-th percentile by nearest rank, on sorted `values`.

    Nearest rank rather than interpolated: the returned figure is then always
    a value the sample actually contained, which keeps a money figure a money
    figure rather than a weighted average of two of them.
    """
    if not values:
        raise DataError("cannot take a percentile of nothing")
    if not Decimal("0") <= pct <= Decimal("100"):
        raise DataError(f"percentile must be between 0 and 100, got {pct}")
    ordered = sorted(values)
    scaled = pct / Decimal("100") * Decimal(len(ordered))
    rank = scaled.to_integral_value(rounding="ROUND_CEILING")
    index = max(0, min(len(ordered) - 1, int(rank) - 1))
    return ordered[index]


def stationary_bootstrap(
    values: Sequence[Decimal],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    mean_block: Decimal | None = None,
    confidence_pct: Decimal = DEFAULT_CONFIDENCE_PCT,
    seed: int = 0,
) -> BootstrapResult:
    """Confidence interval on the sum of `values`, preserving serial structure.

    Each resample is built by starting at a random index and walking forward,
    restarting at a new random index with probability `1 / mean_block` at every
    step. Block lengths are therefore geometric with the given mean, and the
    resampled series is stationary - unlike a fixed-block bootstrap, whose
    resamples depend on where the block boundaries happen to fall.

    `mean_block` defaults to the square root of the sample size, the usual rule
    of thumb, and is reported in the result rather than hidden: an interval's
    width depends on it, so it is part of the claim.
    """
    if not values:
        raise DataError("cannot bootstrap an empty sequence")
    if resamples < 1:
        raise DataError(f"resamples must be positive, got {resamples}")
    if not Decimal("0") < confidence_pct < Decimal("100"):
        raise DataError(f"confidence_pct must be between 0 and 100, got {confidence_pct}")

    size = len(values)
    block = mean_block if mean_block is not None else Decimal(size).sqrt()
    if block <= 0:
        raise DataError(f"mean_block must be positive, got {block}")
    restart_probability = float(1 / block)

    rng = random.Random(f"stationary-bootstrap:{seed}:{size}:{resamples}")
    totals: list[Decimal] = []
    at_or_below_zero = 0
    for _ in range(resamples):
        total = Decimal("0")
        index = rng.randrange(size)
        for _ in range(size):
            total += values[index]
            if rng.random() < restart_probability:
                index = rng.randrange(size)
            else:
                # Wraps rather than stopping, so every position is equally
                # likely to be sampled. Without the wrap the tail of the series
                # is systematically under-represented.
                index = (index + 1) % size
        totals.append(total)
        if total <= 0:
            at_or_below_zero += 1

    tail = (Decimal("100") - confidence_pct) / Decimal("2")
    return BootstrapResult(
        observed=sum(values, Decimal("0")),
        lower=percentile(totals, tail),
        upper=percentile(totals, Decimal("100") - tail),
        confidence_pct=confidence_pct,
        resamples=resamples,
        sample_size=size,
        mean_block=block,
        resamples_at_or_below_zero=at_or_below_zero,
    )


def permutation_result(
    observed: Decimal, null_samples: Sequence[Decimal]
) -> PermutationResult:
    """Locate `observed` in a null distribution someone else generated.

    Deliberately pure and separate from the generating of that distribution:
    building it means re-running a whole backtest per permutation, which is
    slow, needs a terminal, and has nothing to do with the arithmetic of
    reading the answer off. This half is the part worth testing exhaustively.
    """
    if not null_samples:
        raise DataError("a permutation test needs at least one null sample")
    ordered = sorted(null_samples)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / Decimal("2")
    )
    return PermutationResult(
        observed=observed,
        permutations=len(ordered),
        at_or_above_observed=sum(1 for value in ordered if value >= observed),
        null_mean=sum(ordered, Decimal("0")) / Decimal(len(ordered)),
        null_median=median,
        null_p95=percentile(ordered, Decimal("95")),
        null_best=ordered[-1],
    )


def permuted_bars(bars: Sequence[Bar], *, seed: int, tick: Decimal) -> list[Bar]:
    """A price series with the same returns as `bars`, in a shuffled order.

    What is preserved: the marginal distribution of bar-to-bar returns, so
    volatility and fat tails are unchanged; each bar's internal shape, carried
    with its return as ratios to its own close, so a wide bar stays wide; and
    every timestamp, so the session calendar, the swap roll and the bar count
    are identical to the real run.

    What is destroyed: serial dependence - trends, momentum, mean reversion,
    every relationship between one bar and the next. That is the whole point.
    A strategy that claims to trade trend has nothing to trade here, so what it
    earns on these series is what it earns from no edge at all.

    Prices are quantised back to the tick grid, and the high and low are then
    re-widened to contain the open and close, because rounding three prices
    independently can otherwise produce a bar whose high is below its open -
    which is not a bar any venue ever printed.
    """
    if len(bars) < 3:
        raise DataError(f"permuting a price series needs at least 3 bars, got {len(bars)}")
    if tick <= 0:
        raise DataError(f"tick must be positive, got {tick}")

    #: (return, open/close, high/close, low/close) for each bar after the first.
    steps: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for previous, current in pairwise(bars):
        if previous.close <= 0 or current.close <= 0:
            raise DataError("cannot permute a series containing a non-positive close")
        steps.append(
            (
                current.close / previous.close,
                current.open / current.close,
                current.high / current.close,
                current.low / current.close,
            )
        )

    rng = random.Random(f"permuted-bars:{seed}:{len(bars)}")
    order = list(range(len(steps)))
    rng.shuffle(order)

    out = [bars[0]]
    close = bars[0].close
    for position, source in enumerate(order, start=1):
        ratio, open_r, high_r, low_r = steps[source]
        close = close * ratio
        original = bars[position]
        out.append(
            original.model_copy(
                update={
                    "open": _on_tick(close * open_r, tick),
                    "high": _on_tick(close * high_r, tick),
                    "low": _on_tick(close * low_r, tick),
                    "close": _on_tick(close, tick),
                }
            )
        )
        # Re-read the quantised close so drift cannot accumulate off-grid over
        # thousands of bars.
        close = out[-1].close
        out[-1] = out[-1].model_copy(
            update={
                "high": max(out[-1].high, out[-1].open, out[-1].close),
                "low": min(out[-1].low, out[-1].open, out[-1].close),
            }
        )
    return out


def _on_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding="ROUND_HALF_UP") * tick
