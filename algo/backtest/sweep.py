"""A two-axis parameter sweep, and an opinion about whether its best cell is real.

D-125 through D-127 were this done by hand: run the same strategy across
timeframes and stop settings, tabulate nine cells, read the pattern. This does
it in one request.

## The panel argues against its own headline, on purpose

A heatmap invites reading off the greenest square and adopting it. D-131 is the
standing evidence that doing so is how you fit noise: optimising channel length
per window returned a third of what leaving it alone did, and the chosen value
wandered across the whole grid. A grid search over one window is a *weaker*
procedure than that walk-forward, not a stronger one - it has no out-of-sample
step at all.

So every sweep is scored for **robustness**, not just for its maximum:

**A peak with poor neighbours is noise.** If the best cell's orthogonal
neighbours are far below it, that cell is a spike in a rough surface and its
parameters are fitted to whichever trades happened to fall inside this window.
A best cell sitting on a plateau of similar values is a different and much more
credible object.

**A grid where almost nothing works is not a grid with one good setting.** If
only a couple of cells out of forty are positive, the honest reading is that
the strategy does not work here and two cells got lucky, so the positive
fraction is reported next to the maximum.

Neither verdict blocks anything. They are rendered beside the number they
qualify, in the same spirit as `metrics.py` refusing to print a ratio without
its sample size.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from algo.backtest.cfd_runner import run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.core.instrument import CfdId
from algo.live.mt5_runner import strategy_for

#: The most cells one sweep may run. Each is a full backtest over the requested
#: history, so this bounds both wall time and the temptation to search wide.
MAX_CELLS = 64

#: A best cell whose neighbours average below this fraction of it is a spike in
#: a rough surface rather than a plateau. Not a law of nature - a threshold
#: chosen to separate "the setting either side is nearly as good" from "only
#: this exact square works", which is the distinction that matters.
PLATEAU_RATIO = Decimal("0.5")

#: Axes a sweep may vary. `timeframe` is special - it changes the bars
#: themselves, so those are fetched once per value rather than once per cell.
SWEEP_AXES: dict[str, tuple[str, ...]] = {
    "timeframe": ("15", "30", "60", "240"),
    "lookback": ("10", "20", "40", "80"),
    "stop_loss_pct": ("0", "0.5", "1", "2"),
    "trail_pct": ("0", "0.5", "1", "2"),
    "trail_activation_pct": ("1", "2", "3", "5"),
}


@dataclass(frozen=True, slots=True)
class Cell:
    """One backtest in the grid."""

    row: str
    column: str
    net_pnl: Decimal
    trades: int


@dataclass(frozen=True, slots=True)
class Robustness:
    """Whether the best cell looks like signal or like a lucky square."""

    verdict: str
    best_row: str
    best_column: str
    best_net_pnl: Decimal
    neighbour_mean: Decimal | None
    positive_cells: int
    total_cells: int
    median_net_pnl: Decimal
    notes: tuple[str, ...]


def _neighbours(cells: Mapping[tuple[str, str], Cell], rows: Sequence[str],
                columns: Sequence[str], row: str, column: str) -> list[Cell]:
    """Orthogonal neighbours of one cell - the settings one step either way."""
    r, c = rows.index(row), columns.index(column)
    out: list[Cell] = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = r + dr, c + dc
        if 0 <= nr < len(rows) and 0 <= nc < len(columns):
            found = cells.get((rows[nr], columns[nc]))
            if found is not None:
                out.append(found)
    return out


def assess(
    cells: Mapping[tuple[str, str], Cell], rows: Sequence[str], columns: Sequence[str]
) -> Robustness:
    """Score the grid's maximum against its own neighbourhood."""
    values = list(cells.values())
    if not values:
        raise DataError("nothing to assess - the sweep produced no cells")

    best = max(values, key=lambda cell: cell.net_pnl)
    neighbours = _neighbours(cells, rows, columns, best.row, best.column)
    neighbour_mean = (
        sum((n.net_pnl for n in neighbours), Decimal("0")) / Decimal(len(neighbours))
        if neighbours
        else None
    )
    positive = sum(1 for cell in values if cell.net_pnl > 0)
    mid = Decimal(str(median(sorted(float(cell.net_pnl) for cell in values))))

    notes: list[str] = []
    verdict = "PLATEAU"
    if best.net_pnl <= 0:
        verdict = "NOTHING WORKS"
        notes.append(
            "Every cell in this grid lost money. There is no setting to pick "
            "here; the strategy does not work on this instrument over this "
            "window."
        )
    elif neighbour_mean is not None and neighbour_mean < best.net_pnl * PLATEAU_RATIO:
        verdict = "ISOLATED PEAK"
        notes.append(
            "The best cell's neighbours average well below it, so this is a "
            "spike in a rough surface rather than a plateau. Those parameters "
            "are fitted to whichever trades fell inside this one window - the "
            "same failure the walk-forward panel measures directly."
        )
    else:
        notes.append(
            "The best cell sits on a plateau: the settings either side perform "
            "similarly, which is what a real effect looks like. That is not "
            "proof - this is still one window, with no out-of-sample step."
        )

    if positive * 4 < len(values):
        notes.append(
            f"Only {positive} of {len(values)} cells are profitable. A grid "
            "where most settings lose is better read as 'this does not work' "
            "than as 'one setting works'."
        )
    notes.append(
        "A sweep has no out-of-sample step. Run the walk-forward panel before "
        "trusting any cell here."
    )

    return Robustness(
        verdict=verdict,
        best_row=best.row,
        best_column=best.column,
        best_net_pnl=best.net_pnl,
        neighbour_mean=neighbour_mean,
        positive_cells=positive,
        total_cells=len(values),
        median_net_pnl=mid,
        notes=tuple(notes),
    )


def run_sweep(
    *,
    bars_for: Mapping[str, list[Bar]],
    strategy: str,
    instrument: CfdId,
    row_axis: str,
    row_values: Sequence[str],
    column_axis: str,
    column_values: Sequence[str],
    base: Mapping[str, str],
    lots: int,
    timeframe_minutes: int,
) -> tuple[list[Cell], Robustness]:
    """Run every combination and score the result.

    `bars_for` maps a timeframe (as a string of minutes) to its bars. When
    neither axis is `timeframe` it holds one entry; when one is, it holds one
    per value, so history is fetched once per timeframe rather than once per
    cell.
    """
    if len(row_values) * len(column_values) > MAX_CELLS:
        raise DataError(
            f"{len(row_values)} x {len(column_values)} is "
            f"{len(row_values) * len(column_values)} backtests, past the "
            f"{MAX_CELLS} cap. Narrow an axis rather than raising it - a wider "
            "grid finds a better-looking cell without finding more evidence."
        )

    cells: dict[tuple[str, str], Cell] = {}
    for row in row_values:
        for column in column_values:
            params = {**base, row_axis: row, column_axis: column}
            minutes = int(params.get("timeframe", timeframe_minutes))
            bars = bars_for[str(minutes)]
            timeframe = Timeframe(minutes=minutes)

            stop = Decimal(params.get("stop_loss_pct", "0"))
            trail_activation = Decimal(params.get("trail_activation_pct", "2"))
            trail = Decimal(params.get("trail_pct", "0"))
            lookback = int(params.get("lookback", "20"))

            result = run_cfd_backtest(
                bars,
                instrument=instrument,
                timeframe=timeframe,
                strategy_factory=lambda s=stop, ta=trail_activation, t=trail, lb=lookback: (  # type: ignore[misc]
                    strategy_for(
                        strategy,
                        instrument=instrument,
                        stop_loss_pct=s,
                        trail_activation_pct=ta,
                        trail_pct=t,
                        lookback=lb,
                    )
                ),
                stop_loss_pct=stop,
                trail_activation_pct=trail_activation,
                trail_pct=trail,
                lots=lots,
            )
            cells[(row, column)] = Cell(
                row=row,
                column=column,
                net_pnl=result.net_pnl,
                trades=len(result.trades),
            )

    return list(cells.values()), assess(cells, row_values, column_values)


def sweep_to_json(
    cells: Sequence[Cell],
    robustness: Robustness,
    *,
    row_axis: str,
    row_values: Sequence[str],
    column_axis: str,
    column_values: Sequence[str],
) -> dict[str, Any]:
    return {
        "row_axis": row_axis,
        "row_values": list(row_values),
        "column_axis": column_axis,
        "column_values": list(column_values),
        "cells": [
            {
                "row": cell.row,
                "column": cell.column,
                "net_pnl": str(cell.net_pnl),
                "trades": cell.trades,
            }
            for cell in cells
        ],
        "robustness": {
            "verdict": robustness.verdict,
            "best_row": robustness.best_row,
            "best_column": robustness.best_column,
            "best_net_pnl": str(robustness.best_net_pnl),
            "neighbour_mean": (
                str(robustness.neighbour_mean)
                if robustness.neighbour_mean is not None
                else None
            ),
            "positive_cells": robustness.positive_cells,
            "total_cells": robustness.total_cells,
            "median_net_pnl": str(robustness.median_net_pnl),
            "notes": list(robustness.notes),
        },
    }
