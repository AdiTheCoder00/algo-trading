"""Walk-forward analysis. Brief §9 Milestone 5.

    "Rolling optimise/validate windows. Report out-of-sample metrics separately
     and prominently. Flag any parameter whose optimal value jumps around between
     windows — that is curve fitting, not signal."

Three things this module does that a naive walk-forward does not.

**It refuses to blend.** There is no combined metric on `WalkForwardReport`, and
no property that would produce one. In-sample and out-of-sample are reported side
by side because they mean different things, and a single number that averages them
is worse than either.

**It flags unstable parameters.** A parameter whose optimal value changes almost
every window is being fitted to noise. The report says so per parameter, with the
sequence of chosen values printed so the reader can see the wobble rather than
take a verdict on trust.

**It checks the optimisation against doing nothing.** Every window is also
evaluated out of sample using a **fixed** parameter set that was never optimised.
If optimising does not beat leaving the parameters alone, the optimisation is
fitting noise, and that comparison is the single most informative line in the
report. It is easy to leave out, and leaving it out is how walk-forward analyses
come to look convincing.

And one thing it refuses to do: produce a confident headline on too little data.
At roughly twelve trades a year, a walk-forward over two years of recorded data
has perhaps twenty out-of-sample trades in total. `Feasibility` says that plainly
rather than printing a Sharpe ratio and letting the reader assume otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from algo.core.errors import DomainError
from algo.reporting.metrics import Metrics

#: Below this many out-of-sample trades, no ratio in the report means anything.
#: Not a rule of thumb pulled from nowhere: with ~12 trades a year it is the
#: difference between "two years of data" and "enough data".
MIN_OOS_TRADES = 30
#: A parameter that changes its optimal value in half the windows or more is
#: tracking noise, not signal.
UNSTABLE_FLIP_RATE = Decimal("0.5")


ParameterSet = Mapping[str, str]
#: Runs one backtest for a parameter set over a date range and returns its metrics.
RunFn = Callable[[ParameterSet, date, date], Metrics]
#: Scores a candidate. Higher is better. Returns None when the candidate is not
#: eligible — too few trades, say — so it can be excluded with a reason.
ObjectiveFn = Callable[[Metrics], Decimal | None]


@dataclass(frozen=True, slots=True)
class Window:
    """One optimise/validate pair."""

    index: int
    in_sample_start: date
    in_sample_end: date
    out_of_sample_start: date
    out_of_sample_end: date

    def __str__(self) -> str:
        return (
            f"window {self.index}: IS {self.in_sample_start}..{self.in_sample_end} "
            f"-> OOS {self.out_of_sample_start}..{self.out_of_sample_end}"
        )


def rolling_windows(
    *,
    start: date,
    end: date,
    in_sample_days: int,
    out_of_sample_days: int,
    step_days: int | None = None,
) -> list[Window]:
    """Build the rolling windows.

    Out-of-sample periods never overlap each other, so the out-of-sample trades
    can be pooled without double-counting. In-sample periods may overlap — that is
    what makes it *rolling* — but the validation data must not, or the same trade
    would be counted as evidence twice.
    """
    if in_sample_days < 1 or out_of_sample_days < 1:
        raise DomainError("window lengths must be at least one day")
    step = step_days if step_days is not None else out_of_sample_days
    if step < 1:
        raise DomainError("step must be at least one day")
    if step > out_of_sample_days:
        raise DomainError(
            f"a step of {step} days over a {out_of_sample_days}-day validation window "
            "would skip data entirely — every day should be validated exactly once"
        )
    if step < out_of_sample_days:
        raise DomainError(
            f"a step of {step} days with a {out_of_sample_days}-day validation window "
            "would overlap out-of-sample periods, counting the same trades twice"
        )

    windows: list[Window] = []
    is_start = start
    while True:
        is_end = is_start + timedelta(days=in_sample_days - 1)
        oos_start = is_end + timedelta(days=1)
        oos_end = oos_start + timedelta(days=out_of_sample_days - 1)
        if oos_end > end:
            break
        windows.append(
            Window(
                index=len(windows),
                in_sample_start=is_start,
                in_sample_end=is_end,
                out_of_sample_start=oos_start,
                out_of_sample_end=oos_end,
            )
        )
        is_start = is_start + timedelta(days=step)
    return windows


class Stability(StrEnum):
    STABLE = "STABLE"
    DRIFTING = "DRIFTING"
    UNSTABLE = "UNSTABLE"


@dataclass(frozen=True, slots=True)
class ParameterStability:
    """How much one parameter's optimal value moved across windows."""

    name: str
    values: tuple[str, ...]
    distinct: int
    flips: int
    flip_rate: Decimal
    verdict: Stability

    def describe(self) -> str:
        sequence = " -> ".join(self.values)
        return f"{self.name:<24} {self.verdict:<9} {sequence}"


def assess_stability(chosen: Sequence[ParameterSet]) -> tuple[ParameterStability, ...]:
    """Flag parameters whose optimal value jumps between windows.

    `flip_rate` is the fraction of window-to-window transitions in which the value
    changed. It is preferred to counting distinct values because it distinguishes
    a parameter that drifted once and settled from one that alternates every
    window — the second is noise, the first may not be.
    """
    if not chosen:
        return ()
    names = sorted({name for params in chosen for name in params})
    out: list[ParameterStability] = []

    for name in names:
        values = tuple(str(params.get(name, "")) for params in chosen)
        transitions = len(values) - 1
        flips = sum(1 for a, b in pairwise(values) if a != b)
        rate = (
            Decimal(flips) / Decimal(transitions) if transitions > 0 else Decimal("0")
        )
        distinct = len(set(values))
        if distinct == 1:
            verdict = Stability.STABLE
        elif rate >= UNSTABLE_FLIP_RATE:
            verdict = Stability.UNSTABLE
        else:
            verdict = Stability.DRIFTING
        out.append(
            ParameterStability(
                name=name,
                values=values,
                distinct=distinct,
                flips=flips,
                flip_rate=rate,
                verdict=verdict,
            )
        )
    return tuple(out)


class Confidence(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    THIN = "THIN"
    ADEQUATE = "ADEQUATE"


@dataclass(frozen=True, slots=True)
class Feasibility:
    """Whether this walk-forward can support a conclusion at all."""

    windows: int
    oos_trades: int
    min_trades_in_a_window: int
    confidence: Confidence
    message: str

    @property
    def supports_a_conclusion(self) -> bool:
        return self.confidence is Confidence.ADEQUATE


def assess_feasibility(windows: int, oos_trades: int, per_window: Sequence[int]) -> Feasibility:
    smallest = min(per_window) if per_window else 0
    if windows < 3 or oos_trades < MIN_OOS_TRADES // 3:
        confidence = Confidence.INSUFFICIENT
        message = (
            f"{windows} windows and {oos_trades} out-of-sample trades. This cannot "
            "distinguish a strategy from a coin flip. Nothing below should be read "
            "as evidence either way."
        )
    elif oos_trades < MIN_OOS_TRADES:
        confidence = Confidence.THIN
        message = (
            f"{oos_trades} out-of-sample trades across {windows} windows, against a "
            f"minimum of {MIN_OOS_TRADES} for any ratio to mean much. Treat every "
            "number below as directional at best."
        )
    else:
        confidence = Confidence.ADEQUATE
        message = f"{oos_trades} out-of-sample trades across {windows} windows."
    return Feasibility(
        windows=windows,
        oos_trades=oos_trades,
        min_trades_in_a_window=smallest,
        confidence=confidence,
        message=message,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    params: ParameterSet
    metrics: Metrics
    score: Decimal | None
    excluded_because: str = ""


@dataclass(frozen=True, slots=True)
class WindowResult:
    window: Window
    chosen: ParameterSet
    in_sample: Metrics
    out_of_sample: Metrics
    baseline_out_of_sample: Metrics | None
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """In-sample and out-of-sample, side by side. Deliberately never combined.

    There is no `overall` here, and no property that would produce one. Averaging
    a number the parameters were fitted to with a number they were not is worse
    than reporting either alone.
    """

    results: tuple[WindowResult, ...]
    stability: tuple[ParameterStability, ...]
    feasibility: Feasibility

    @property
    def in_sample_net_pnl(self) -> Decimal:
        return sum((r.in_sample.net_pnl for r in self.results), Decimal("0"))

    @property
    def out_of_sample_net_pnl(self) -> Decimal:
        return sum((r.out_of_sample.net_pnl for r in self.results), Decimal("0"))

    @property
    def baseline_net_pnl(self) -> Decimal | None:
        """Out-of-sample P&L from the fixed, unoptimised parameters."""
        if any(r.baseline_out_of_sample is None for r in self.results):
            return None
        return sum(
            (
                r.baseline_out_of_sample.net_pnl
                for r in self.results
                if r.baseline_out_of_sample is not None
            ),
            Decimal("0"),
        )

    @property
    def optimisation_beat_doing_nothing(self) -> bool | None:
        """Did choosing parameters per window beat leaving them alone?

        `None` when no baseline was supplied. When this is False, the honest
        reading is that the optimisation is fitting noise — which is the finding,
        not a failure of the run.
        """
        baseline = self.baseline_net_pnl
        if baseline is None:
            return None
        return self.out_of_sample_net_pnl > baseline

    @property
    def unstable_parameters(self) -> tuple[ParameterStability, ...]:
        return tuple(s for s in self.stability if s.verdict is Stability.UNSTABLE)

    def summary(self) -> str:
        lines = [
            "WALK-FORWARD",
            "=" * 64,
            f"  {self.feasibility.confidence}: {self.feasibility.message}",
            "",
            f"  {'':<14}{'in-sample':>18}{'OUT OF SAMPLE':>20}",
            f"  {'net P&L':<14}{self.in_sample_net_pnl:>18,}{self.out_of_sample_net_pnl:>20,}",
            f"  {'trades':<14}"
            f"{sum(r.in_sample.trade_count for r in self.results):>18}"
            f"{sum(r.out_of_sample.trade_count for r in self.results):>20}",
        ]

        baseline = self.baseline_net_pnl
        if baseline is not None:
            lines.extend(
                [
                    "",
                    f"  fixed parameters, out of sample: {baseline:,}",
                    f"  optimised parameters beat them:  {self.optimisation_beat_doing_nothing}",
                ]
            )
            if self.optimisation_beat_doing_nothing is False:
                lines.append(
                    "  -> optimising did not beat leaving the parameters alone. "
                    "That is curve fitting."
                )

        lines.extend(["", "  parameter stability across windows:"])
        lines.extend(f"    {s.describe()}" for s in self.stability)
        if self.unstable_parameters:
            names = ", ".join(s.name for s in self.unstable_parameters)
            lines.append(
                f"    -> {names} changed in half the windows or more. "
                "That is fitting to noise, not finding signal."
            )
        return "\n".join(lines)


def run_walk_forward(
    *,
    windows: Sequence[Window],
    grid: Sequence[ParameterSet],
    run: RunFn,
    objective: ObjectiveFn,
    baseline: ParameterSet | None = None,
) -> WalkForwardReport:
    """Optimise on each in-sample window, validate on the window that follows.

    The chosen parameters for a window are fitted **only** to that window's
    in-sample period, and are then applied unchanged to a period the optimiser
    never saw. That is the whole point, and it is why the two columns of the
    report must never be added together.
    """
    if not windows:
        raise DomainError("walk-forward needs at least one window")
    if not grid:
        raise DomainError("walk-forward needs at least one parameter set to try")

    results: list[WindowResult] = []
    for window in windows:
        candidates: list[Candidate] = []
        for params in grid:
            metrics = run(params, window.in_sample_start, window.in_sample_end)
            score = objective(metrics)
            candidates.append(
                Candidate(
                    params=params,
                    metrics=metrics,
                    score=score,
                    excluded_because="" if score is not None else "ineligible under the objective",
                )
            )

        eligible = [c for c in candidates if c.score is not None]
        if not eligible:
            raise DomainError(
                f"no eligible parameter set for {window} — every candidate was "
                "excluded by the objective. Widen the grid or relax the objective "
                "rather than lowering the bar silently."
            )
        # Ties break on grid order, so the result does not depend on dict ordering.
        best = max(eligible, key=lambda c: (c.score or Decimal("0"), -grid.index(c.params)))

        results.append(
            WindowResult(
                window=window,
                chosen=best.params,
                in_sample=best.metrics,
                out_of_sample=run(
                    best.params, window.out_of_sample_start, window.out_of_sample_end
                ),
                baseline_out_of_sample=(
                    run(baseline, window.out_of_sample_start, window.out_of_sample_end)
                    if baseline is not None
                    else None
                ),
                candidates=tuple(candidates),
            )
        )

    per_window = [r.out_of_sample.trade_count for r in results]
    return WalkForwardReport(
        results=tuple(results),
        stability=assess_stability([r.chosen for r in results]),
        feasibility=assess_feasibility(len(results), sum(per_window), per_window),
    )
