"""Walk-forward for the CFD strategies, over real MT5 history.

`algo/backtest/walkforward.py` has held the honest machinery since Milestone 5 -
rolling windows, per-parameter stability, the fixed-parameter baseline, and a
`Feasibility` check that refuses a confident headline on thin data. Nothing had
ever driven it with real bars, and nothing exposed it beyond the CLI's synthetic
`walkforward` feasibility command. This is the driver.

## Why this is the panel worth having

Every CFD result this project has produced carries the same caveat: one window,
and gold trended through it (D-124, D-127, D-130). A single backtest cannot tell
edge from a bull market. Walk-forward is the direct answer - fit on a window,
validate on the window that follows, and report **only** what the optimiser
never saw.

Three properties come from the underlying module and are not re-derived here:

**In-sample and out-of-sample are never blended.** `WalkForwardReport` has no
combined metric and no property that would produce one.

**The optimisation is checked against doing nothing.** Every window is also
validated with a fixed parameter set that was never optimised. If choosing
parameters per window does not beat leaving them alone, the optimisation is
fitting noise - and that is the finding, not a failure of the run.

**Thin data says so.** Below `MIN_OOS_TRADES` out-of-sample trades the report
declines to support a conclusion, which for these strategies at these
timeframes is a real and frequent outcome rather than a theoretical guard.

## The grid is deliberately small

One or two axes, a handful of values each. A wide grid searched against a short
history is how walk-forward analyses come to look convincing: more candidates
means a better in-sample fit and no more real evidence. `MAX_GRID` caps it, and
the console offers the axes this project has actually studied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from algo.backtest.cfd_runner import run_cfd_backtest
from algo.backtest.walkforward import (
    MIN_OOS_TRADES,
    ParameterSet,
    WalkForwardReport,
    rolling_windows,
    run_walk_forward,
)
from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.core.instrument import CfdId
from algo.live.mt5_runner import strategy_for
from algo.portfolio.book import EquityPoint
from algo.reporting.metrics import Metrics, compute

#: The most parameter combinations one study may search. Small on purpose - see
#: the module docstring on why a wide grid is worse evidence, not better.
MAX_GRID = 24

#: A candidate with fewer in-sample trades than this is not eligible to be
#: chosen. Without it the optimiser picks whichever parameter set happened to
#: take two lucky trades, which is the purest form of fitting to noise.
MIN_IS_TRADES = 5

#: Grid axes the console offers, and the values it steps through. Only knobs
#: this project has actually measured across (D-124 through D-127).
GRID_AXES: dict[str, tuple[str, ...]] = {
    "lookback": ("10", "20", "40", "80"),
    "stop_loss_pct": ("0", "0.5", "1", "2"),
    "trail_pct": ("0", "0.5", "1", "2"),
}


def _as_equity_points(
    curve: Sequence[tuple[datetime, Decimal, int]],
) -> tuple[EquityPoint, ...]:
    """Adapt the CFD runner's curve to what `metrics.compute` reads.

    Only `ts`, `equity` and `open_positions` are load-bearing there (returns,
    drawdown and exposure); the cash/market-value split a real `Portfolio`
    tracks has no meaning for this runner, so those are filled consistently
    rather than invented per point.
    """
    return tuple(
        EquityPoint(
            ts=ts,
            cash=equity,
            market_value=Decimal("0"),
            equity=equity,
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("0"),
            charges=Decimal("0"),
            open_positions=open_positions,
        )
        for ts, equity, open_positions in curve
    )


def _slice(bars: Sequence[Bar], start: date, end: date) -> list[Bar]:
    """Bars whose close falls inside `[start, end]`, inclusive of both dates."""
    lo = datetime.combine(start, time.min, tzinfo=bars[0].ts.tzinfo)
    hi = datetime.combine(end, time.max, tzinfo=bars[0].ts.tzinfo)
    return [bar for bar in bars if lo <= bar.ts <= hi]


def build_grid(axes: Mapping[str, Sequence[str]], base: Mapping[str, str]) -> list[ParameterSet]:
    """Cartesian product of the chosen axes, on top of the fixed base values.

    Refuses rather than truncates past `MAX_GRID`: silently searching a smaller
    grid than asked for would report a study the caller did not run.
    """
    grid: list[ParameterSet] = [dict(base)]
    for name, values in axes.items():
        grid = [{**params, name: value} for params in grid for value in values]
    if len(grid) > MAX_GRID:
        raise DataError(
            f"that grid is {len(grid)} combinations, past the {MAX_GRID} cap. A "
            "wider grid fits the in-sample window better without producing more "
            "real evidence - narrow the axes rather than raising the cap."
        )
    return grid


def run_cfd_walk_forward(
    bars: list[Bar],
    *,
    strategy: str,
    instrument: CfdId,
    timeframe: Timeframe,
    axes: Mapping[str, Sequence[str]],
    base: Mapping[str, str],
    lots: int,
    in_sample_days: int,
    out_of_sample_days: int,
    starting_equity: Decimal = Decimal("100000"),
) -> WalkForwardReport:
    """Optimise on each in-sample window, validate on the one that follows."""
    if not bars:
        raise DataError("walk-forward needs bars")

    windows = rolling_windows(
        start=bars[0].ts.date(),
        end=bars[-1].ts.date(),
        in_sample_days=in_sample_days,
        out_of_sample_days=out_of_sample_days,
    )
    if not windows:
        raise DataError(
            f"{(bars[-1].ts.date() - bars[0].ts.date()).days} days of history is not "
            f"enough for even one {in_sample_days}+{out_of_sample_days}-day window. "
            "Pull more bars, or shorten the windows."
        )

    grid = build_grid(axes, base)

    def run(params: ParameterSet, start: date, end: date) -> Metrics:
        window_bars = _slice(bars, start, end)
        if len(window_bars) < 2:
            # An empty window is a real outcome over a holiday stretch; it is
            # reported as a zero-trade result rather than raising, so one thin
            # window does not abort the whole study.
            empty = _as_equity_points(
                [(bars[0].ts, starting_equity, 0), (bars[-1].ts, starting_equity, 0)]
            )
            return compute(empty, trade_count=0, total_cost=Decimal("0"))

        stop = Decimal(params.get("stop_loss_pct", "0"))
        trail_activation = Decimal(params.get("trail_activation_pct", "2"))
        trail = Decimal(params.get("trail_pct", "0"))
        lookback = int(params.get("lookback", "20"))

        result = run_cfd_backtest(
            window_bars,
            instrument=instrument,
            timeframe=timeframe,
            strategy_factory=lambda: strategy_for(
                strategy,
                instrument=instrument,
                stop_loss_pct=stop,
                trail_activation_pct=trail_activation,
                trail_pct=trail,
                lookback=lookback,
            ),
            stop_loss_pct=stop,
            trail_activation_pct=trail_activation,
            trail_pct=trail,
            lots=lots,
            starting_equity=starting_equity,
        )
        return compute(
            _as_equity_points(result.equity_curve),
            trade_count=len(result.trades),
            total_cost=result.spread_paid + result.swap_paid + result.commission_paid,
        )

    def objective(metrics: Metrics) -> Decimal | None:
        """Net P&L, but only for candidates that actually traded.

        Deliberately not Sharpe: over a window this short the ratio is dominated
        by how few trades it took, and `metrics.py` already refuses to print one
        without its sample size for the same reason.
        """
        if metrics.trade_count < MIN_IS_TRADES:
            return None
        return metrics.net_pnl

    return run_walk_forward(
        windows=windows,
        grid=grid,
        run=run,
        objective=objective,
        # The fixed set the optimisation must beat to have earned anything.
        baseline=dict(base),
    )


def report_to_json(report: WalkForwardReport) -> dict[str, Any]:
    """Flatten the report for the dashboard, keeping the two columns apart."""
    baseline = report.baseline_net_pnl
    return {
        "confidence": report.feasibility.confidence.value,
        "feasibility": report.feasibility.message,
        "supports_a_conclusion": report.feasibility.supports_a_conclusion,
        "min_oos_trades": MIN_OOS_TRADES,
        "windows": report.feasibility.windows,
        "oos_trades": report.feasibility.oos_trades,
        "in_sample_net_pnl": str(report.in_sample_net_pnl),
        "out_of_sample_net_pnl": str(report.out_of_sample_net_pnl),
        "baseline_net_pnl": str(baseline) if baseline is not None else None,
        "optimisation_beat_doing_nothing": report.optimisation_beat_doing_nothing,
        "stability": [
            {
                "name": s.name,
                "verdict": s.verdict.value,
                "values": list(s.values),
                "distinct": s.distinct,
                "flip_rate": str(s.flip_rate),
            }
            for s in report.stability
        ],
        "unstable": [s.name for s in report.unstable_parameters],
        "results": [
            {
                "index": r.window.index,
                "in_sample_start": r.window.in_sample_start.isoformat(),
                "in_sample_end": r.window.in_sample_end.isoformat(),
                "out_of_sample_start": r.window.out_of_sample_start.isoformat(),
                "out_of_sample_end": r.window.out_of_sample_end.isoformat(),
                "chosen": dict(r.chosen),
                "in_sample_net_pnl": str(r.in_sample.net_pnl),
                "in_sample_trades": r.in_sample.trade_count,
                "out_of_sample_net_pnl": str(r.out_of_sample.net_pnl),
                "out_of_sample_trades": r.out_of_sample.trade_count,
                "baseline_net_pnl": (
                    str(r.baseline_out_of_sample.net_pnl)
                    if r.baseline_out_of_sample is not None
                    else None
                ),
            }
            for r in report.results
        ],
    }
