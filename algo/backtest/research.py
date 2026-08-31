"""The research service behind the dashboard's backtest console.

## Why this is separate from the monitoring API's read path

`algo/api/app.py` opens with "Read-only means read-only... A dashboard that can
move a position is not a dashboard", and Q21 settled that live parameters change
through config and a restart, never through the UI. A backtest console is not a
violation of either, and the distinction is worth stating precisely rather than
assumed:

**A backtest touches no live state.** It reads historical bars, runs them
through a strategy instance built for that one request, and returns numbers. It
holds no `Portfolio` the engine shares, no `OrderRouter`, no broker connection,
and it does not write to the `StateStore` the live engine writes. Nothing a
person types into the console can reach a position, an order, or a running
loop's parameters.

**Live parameters remain config-file driven.** Choosing `--timeframe 60` for a
*study* is exploration; changing what the running loop trades is a deployment,
and still goes through a config file and a restart. This module deliberately
offers no way to do the second.

## Bounded on purpose

`MAX_BARS` caps a request. Without it, a browser can ask for 50,000 bars across
six timeframes and tie up the process the monitoring API also serves - a
research tool that can starve the thing watching a live position is not an
acceptable trade, and the cap is far above any study these strategies need.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from algo.backtest.cfd_runner import run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.core.instrument import CfdId
from algo.data.mt5_history import TIMEFRAME_CONSTANTS, fetch_history, resolve_server_offset
from algo.live.mt5_runner import strategy_for

#: The most bars one request may pull. Well past what any study here needs
#: (D-124's fair-window comparison used 25,000) and far short of tying up the
#: process for minutes.
MAX_BARS = 50_000

#: Timeframes offered to the console. A subset of what MT5 exposes: these are
#: the ones this project has actually measured against (D-123/D-124).
OFFERED_TIMEFRAMES: tuple[int, ...] = (5, 15, 30, 60, 240, 1440)


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One knob the console renders, with the range it is allowed to take."""

    name: str
    label: str
    kind: str  # "decimal" | "int"
    default: str
    minimum: str
    maximum: str
    help: str
    applies_to: tuple[str, ...] = ()


#: The catalogue the console builds its form from. Defined here rather than in
#: the dashboard so the UI cannot offer a parameter the engine does not have,
#: or default it differently from the strategy's own constructor.
PARAMETERS: tuple[ParamSpec, ...] = (
    ParamSpec(
        name="lookback",
        label="Channel length",
        kind="int",
        default="20",
        minimum="2",
        maximum="500",
        help="Bars in the Donchian channel. The breakout is measured against "
        "the highest high and lowest low of the prior N bars.",
        applies_to=("breakout",),
    ),
    ParamSpec(
        name="stop_loss_pct",
        label="Stop loss %",
        kind="decimal",
        default="0",
        minimum="0",
        maximum="25",
        help="Flat stop, as a percentage of entry price, checked against the "
        "bar's actual range. 0 disables it. D-125/D-126 measured this as not "
        "a uniform improvement - it rescued the worst case and taxed the rest.",
    ),
    ParamSpec(
        name="trail_activation_pct",
        label="Trail arms at %",
        kind="decimal",
        default="2",
        minimum="0",
        maximum="50",
        help="Profit the position must reach before the trailing stop arms. "
        "Nothing is protected before this.",
    ),
    ParamSpec(
        name="trail_pct",
        label="Trail distance %",
        kind="decimal",
        default="0",
        minimum="0",
        maximum="25",
        help="How far behind the best price the trail follows, once armed. "
        "0 disables it. Floored at entry, so an armed trail's worst outcome "
        "is a scratch (D-127).",
    ),
    ParamSpec(
        name="lots",
        label="Size (ounces)",
        kind="int",
        default="100",
        minimum="1",
        maximum="10000",
        help="1 engine lot = 1 troy ounce = 0.01 MT5 lots.",
    ),
    ParamSpec(
        name="bars",
        label="Bars of history",
        kind="int",
        default="5000",
        minimum="100",
        maximum=str(MAX_BARS),
        help="How much history to pull. More bars is a longer window, not a "
        "better study - see the caveats on any result.",
    ),
)

STRATEGIES: tuple[dict[str, str], ...] = (
    {
        "id": "breakout",
        "label": "Trendline breakout (Donchian)",
        "blurb": "Long on a fresh N-bar high, short on a fresh N-bar low. The "
        "well-defined form of a trend line; the core of Turtle trading.",
    },
    {
        "id": "macd",
        "label": "MACD crossover (12, 26, 9)",
        "blurb": "Long on a bullish histogram cross, short on a bearish one. "
        "Matches tools/macd_telegram_alert exactly.",
    },
)


def catalogue() -> dict[str, Any]:
    """Everything the console needs to render its form."""
    return {
        "strategies": [dict(s) for s in STRATEGIES],
        "timeframes": [
            {"minutes": m, "label": Timeframe(minutes=m).label} for m in OFFERED_TIMEFRAMES
        ],
        "parameters": [
            {
                "name": p.name,
                "label": p.label,
                "kind": p.kind,
                "default": p.default,
                "minimum": p.minimum,
                "maximum": p.maximum,
                "help": p.help,
                "applies_to": list(p.applies_to),
            }
            for p in PARAMETERS
        ],
        "max_bars": MAX_BARS,
        "walk_forward_axes": walk_forward_axes(),
        "sweep_axes": sweep_axes(),
    }


def _spec(name: str) -> ParamSpec:
    for candidate in PARAMETERS:
        if candidate.name == name:
            return candidate
    raise DataError(f"no such parameter: {name}")


def _decimal(name: str, raw: str) -> Decimal:
    """Parse and range-check one parameter, refusing rather than clamping.

    Clamping would run a study the caller did not ask for and report it as
    though they had - the same reason the config loader refuses an unquoted
    float rather than rounding it.
    """
    spec = _spec(name)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise DataError(f"{spec.label}: {raw!r} is not a number") from exc
    if value < Decimal(spec.minimum) or value > Decimal(spec.maximum):
        raise DataError(
            f"{spec.label} must be between {spec.minimum} and {spec.maximum}, got {value}"
        )
    return value


def _int(name: str, raw: object) -> int:
    spec = _spec(name)
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise DataError(f"{spec.label}: {raw!r} is not a whole number") from exc
    if value < int(spec.minimum) or value > int(spec.maximum):
        raise DataError(
            f"{spec.label} must be between {spec.minimum} and {spec.maximum}, got {value}"
        )
    return value


def run_study(
    terminal: object,
    *,
    strategy: str,
    timeframe_minutes: int,
    symbol: str = "XAUUSD",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one backtest and return it as plain JSON-ready data.

    Every money figure is a string, for the reason `lib/api.ts` already states:
    the engine keeps `Decimal` precisely so it never passes through a float, and
    parsing it into a JavaScript number at the last step would undo that.
    """
    supplied = params or {}
    # Everything cheap is checked before the terminal is touched: attaching to
    # MT5 and pulling 50,000 bars to then refuse a misspelled strategy name
    # wastes seconds and reports the wrong problem first.
    if strategy not in {s["id"] for s in STRATEGIES}:
        raise DataError(
            f"unknown strategy {strategy!r}; available: "
            f"{', '.join(s['id'] for s in STRATEGIES)}"
        )
    if timeframe_minutes not in TIMEFRAME_CONSTANTS:
        raise DataError(
            f"no {timeframe_minutes}-minute timeframe; available: "
            f"{sorted(TIMEFRAME_CONSTANTS)}"
        )

    lookback = _int("lookback", supplied.get("lookback", _spec("lookback").default))
    lots = _int("lots", supplied.get("lots", _spec("lots").default))
    count = _int("bars", supplied.get("bars", _spec("bars").default))
    stop_loss_pct = _decimal(
        "stop_loss_pct", supplied.get("stop_loss_pct", _spec("stop_loss_pct").default)
    )
    trail_activation_pct = _decimal(
        "trail_activation_pct",
        supplied.get("trail_activation_pct", _spec("trail_activation_pct").default),
    )
    trail_pct = _decimal("trail_pct", supplied.get("trail_pct", _spec("trail_pct").default))

    timeframe = Timeframe(minutes=timeframe_minutes)
    instrument = CfdId(symbol=symbol)

    resolved = resolve_server_offset(terminal, symbol)  # type: ignore[arg-type]
    bars = fetch_history(
        terminal,  # type: ignore[arg-type]
        symbol=symbol,
        timeframe=timeframe,
        count=count,
        offset=resolved.offset,
    )
    if len(bars) < 2:
        raise DataError(f"only {len(bars)} bar(s) available - not enough to study")

    result = run_cfd_backtest(
        bars,
        instrument=instrument,
        timeframe=timeframe,
        strategy_factory=lambda: strategy_for(
            strategy,
            instrument=instrument,
            stop_loss_pct=stop_loss_pct,
            trail_activation_pct=trail_activation_pct,
            trail_pct=trail_pct,
            lookback=lookback,
        ),
        stop_loss_pct=stop_loss_pct,
        trail_activation_pct=trail_activation_pct,
        trail_pct=trail_pct,
        lots=lots,
    )

    span = bars[-1].ts - bars[0].ts
    buy_and_hold = (bars[-1].close - bars[0].close) * lots
    return {
        "strategy": strategy,
        "symbol": symbol,
        "timeframe_minutes": timeframe_minutes,
        "timeframe_label": timeframe.label,
        "params": {
            "lookback": lookback,
            "lots": lots,
            "bars": count,
            "stop_loss_pct": str(stop_loss_pct),
            "trail_activation_pct": str(trail_activation_pct),
            "trail_pct": str(trail_pct),
        },
        "server_offset": resolved.describe(),
        "offset_was_cached": not resolved.measured_now,
        "bars_seen": result.bars_seen,
        "window_start": bars[0].ts.isoformat(),
        "window_end": bars[-1].ts.isoformat(),
        "span_days": round(span / timedelta(days=1), 2),
        "trades": len(result.trades),
        "wins": result.wins,
        "win_rate": str(result.win_rate) if result.win_rate is not None else None,
        "gross_pnl": str(result.gross_pnl),
        "net_pnl": str(result.net_pnl),
        "spread_paid": str(result.spread_paid),
        "swap_paid": str(result.swap_paid),
        "commission_paid": str(result.commission_paid),
        "buy_and_hold": str(buy_and_hold),
        "max_drawdown_pct": (
            str(result.max_drawdown_pct) if result.max_drawdown_pct is not None else None
        ),
        "equity_curve": [
            {"ts": ts.isoformat(), "equity": str(equity)}
            # A browser does not need 50,000 points to draw a line 900px wide.
            # Thinned by stride rather than by averaging: an averaged curve would
            # hide the drawdown depth this is partly drawn to show.
            for ts, equity, _open in result.equity_curve[:: max(1, len(result.equity_curve) // 600)]
        ],
        "recent_trades": [
            {
                "side": trade.side.value,
                "entry_ts": trade.entry_ts.isoformat(),
                "exit_ts": trade.exit_ts.isoformat() if trade.exit_ts else None,
                "entry_price": str(trade.entry_price),
                "exit_price": str(trade.exit_price) if trade.exit_price else None,
                "net_pnl": str(trade.net_pnl),
                "exit_reason": trade.exit_reason,
            }
            for trade in result.trades[-50:]
        ],
        "caveats": _caveats(result, span),
    }


def run_walk_forward_study(
    terminal: object,
    *,
    strategy: str,
    timeframe_minutes: int,
    symbol: str = "XAUUSD",
    axis: str = "lookback",
    second_axis: str = "",
    in_sample_days: int = 90,
    out_of_sample_days: int = 30,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk-forward over MT5 history: fit on a window, validate on the next.

    The answer to the caveat every single-window backtest in this project has
    carried. Same cheap-checks-first ordering as `run_study`: nothing touches
    the terminal until the request is known to be runnable.
    """
    from algo.backtest.cfd_walkforward import GRID_AXES, report_to_json, run_cfd_walk_forward

    supplied = params or {}
    if strategy not in {s["id"] for s in STRATEGIES}:
        raise DataError(
            f"unknown strategy {strategy!r}; available: "
            f"{', '.join(s['id'] for s in STRATEGIES)}"
        )
    if timeframe_minutes not in TIMEFRAME_CONSTANTS:
        raise DataError(
            f"no {timeframe_minutes}-minute timeframe; available: "
            f"{sorted(TIMEFRAME_CONSTANTS)}"
        )

    chosen = [name for name in (axis, second_axis) if name]
    if not chosen:
        raise DataError("walk-forward needs at least one parameter to optimise over")
    for name in chosen:
        if name not in GRID_AXES:
            raise DataError(
                f"{name!r} is not an optimisable axis; available: "
                f"{', '.join(GRID_AXES)}"
            )
    # `lookback` is a Donchian channel length - MACD has no such knob, and
    # optimising a parameter the strategy ignores would produce a grid whose
    # cells are all identical and a "stability" verdict that means nothing.
    if strategy != "breakout" and "lookback" in chosen:
        raise DataError(
            "channel length is a breakout parameter; MACD has no such knob, so "
            "optimising over it would search a grid of identical runs"
        )
    if len(set(chosen)) != len(chosen):
        raise DataError("the two axes must differ")

    if in_sample_days < 7 or out_of_sample_days < 7:
        raise DataError("each window must be at least 7 days")

    lots = _int("lots", supplied.get("lots", _spec("lots").default))
    count = _int("bars", supplied.get("bars", _spec("bars").default))
    timeframe = Timeframe(minutes=timeframe_minutes)
    instrument = CfdId(symbol=symbol)

    resolved = resolve_server_offset(terminal, symbol)  # type: ignore[arg-type]
    bars = fetch_history(
        terminal,  # type: ignore[arg-type]
        symbol=symbol,
        timeframe=timeframe,
        count=count,
        offset=resolved.offset,
    )

    report = run_cfd_walk_forward(
        bars,
        strategy=strategy,
        instrument=instrument,
        timeframe=timeframe,
        axes={name: GRID_AXES[name] for name in chosen},
        # Whatever is not being optimised is held fixed, and is also the
        # baseline the optimisation has to beat.
        base={
            "lookback": str(_int("lookback", supplied.get("lookback", "20"))),
            "stop_loss_pct": str(
                _decimal("stop_loss_pct", supplied.get("stop_loss_pct", "0"))
            ),
            "trail_activation_pct": str(
                _decimal("trail_activation_pct", supplied.get("trail_activation_pct", "2"))
            ),
            "trail_pct": str(_decimal("trail_pct", supplied.get("trail_pct", "0"))),
        },
        lots=lots,
        in_sample_days=in_sample_days,
        out_of_sample_days=out_of_sample_days,
    )

    body = report_to_json(report)
    body.update(
        {
            "strategy": strategy,
            "symbol": symbol,
            "timeframe_label": timeframe.label,
            "axes": chosen,
            "in_sample_days": in_sample_days,
            "out_of_sample_days": out_of_sample_days,
            "window_start": bars[0].ts.isoformat(),
            "window_end": bars[-1].ts.isoformat(),
            "server_offset": resolved.describe(),
        }
    )
    return body


def run_sweep_study(
    terminal: object,
    *,
    strategy: str,
    timeframe_minutes: int = 60,
    symbol: str = "XAUUSD",
    row_axis: str = "timeframe",
    column_axis: str = "stop_loss_pct",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A two-axis grid of backtests, scored for whether its best cell is real.

    Bars are fetched **once per timeframe**, not once per cell: a 4x4 grid
    varying timeframe would otherwise pull the same history four times over.
    """
    from algo.backtest.sweep import SWEEP_AXES, run_sweep, sweep_to_json

    supplied = params or {}
    if strategy not in {s["id"] for s in STRATEGIES}:
        raise DataError(
            f"unknown strategy {strategy!r}; available: "
            f"{', '.join(s['id'] for s in STRATEGIES)}"
        )
    for name in (row_axis, column_axis):
        if name not in SWEEP_AXES:
            raise DataError(
                f"{name!r} is not a sweep axis; available: {', '.join(SWEEP_AXES)}"
            )
    if row_axis == column_axis:
        raise DataError("the two axes must differ")
    if strategy != "breakout" and "lookback" in (row_axis, column_axis):
        raise DataError(
            "channel length is a breakout parameter; MACD has no such knob, so "
            "sweeping it would produce a grid of identical columns"
        )

    lots = _int("lots", supplied.get("lots", _spec("lots").default))
    count = _int("bars", supplied.get("bars", _spec("bars").default))
    instrument = CfdId(symbol=symbol)
    row_values = SWEEP_AXES[row_axis]
    column_values = SWEEP_AXES[column_axis]

    resolved = resolve_server_offset(terminal, symbol)  # type: ignore[arg-type]

    # Which timeframes this grid actually needs: every value of whichever axis
    # is `timeframe`, or just the fixed one when neither axis varies it.
    if row_axis == "timeframe":
        needed = list(row_values)
    elif column_axis == "timeframe":
        needed = list(column_values)
    else:
        needed = [str(timeframe_minutes)]

    bars_for: dict[str, list[Bar]] = {}
    for minutes in needed:
        if int(minutes) not in TIMEFRAME_CONSTANTS:
            raise DataError(f"no {minutes}-minute timeframe")
        bars_for[minutes] = fetch_history(
            terminal,  # type: ignore[arg-type]
            symbol=symbol,
            timeframe=Timeframe(minutes=int(minutes)),
            count=count,
            offset=resolved.offset,
        )

    cells, robustness = run_sweep(
        bars_for=bars_for,
        strategy=strategy,
        instrument=instrument,
        row_axis=row_axis,
        row_values=row_values,
        column_axis=column_axis,
        column_values=column_values,
        base={
            "lookback": str(_int("lookback", supplied.get("lookback", "20"))),
            "stop_loss_pct": str(
                _decimal("stop_loss_pct", supplied.get("stop_loss_pct", "0"))
            ),
            "trail_activation_pct": str(
                _decimal("trail_activation_pct", supplied.get("trail_activation_pct", "2"))
            ),
            "trail_pct": str(_decimal("trail_pct", supplied.get("trail_pct", "0"))),
        },
        lots=lots,
        timeframe_minutes=timeframe_minutes,
    )

    body = sweep_to_json(
        cells,
        robustness,
        row_axis=row_axis,
        row_values=row_values,
        column_axis=column_axis,
        column_values=column_values,
    )
    sample = bars_for[needed[0]]
    body.update(
        {
            "strategy": strategy,
            "symbol": symbol,
            "window_start": sample[0].ts.isoformat(),
            "window_end": sample[-1].ts.isoformat(),
            "server_offset": resolved.describe(),
        }
    )
    return body


def sweep_axes() -> list[dict[str, Any]]:
    """Which parameters a sweep may vary, for the console's form."""
    from algo.backtest.sweep import SWEEP_AXES

    labels = {p.name: p.label for p in PARAMETERS}
    labels["timeframe"] = "Timeframe"
    return [
        {"name": name, "label": labels.get(name, name), "values": list(values)}
        for name, values in SWEEP_AXES.items()
    ]


def walk_forward_axes() -> list[dict[str, Any]]:
    """Which parameters may be optimised over, for the console's form."""
    from algo.backtest.cfd_walkforward import GRID_AXES

    labels = {p.name: p.label for p in PARAMETERS}
    return [
        {"name": name, "label": labels.get(name, name), "values": list(values)}
        for name, values in GRID_AXES.items()
    ]


def _caveats(result: object, span: timedelta) -> list[str]:
    """What this number is not evidence of. Always non-empty by design.

    The tearsheet already refuses to print a ratio without its sample size
    (D-090's reasoning); a console that returns a big green number with nothing
    qualifying it would undo that at the last step.
    """
    trades = len(result.trades)  # type: ignore[attr-defined]
    notes = [
        "Spread and financing are modelled from measured Vantage terms, not "
        "charged per fill - see D-121.",
        "One instrument, one historical window. A trend-following result during "
        "a trending period is not distinguishable from edge on a single run.",
    ]
    if trades < 30:
        notes.append(
            f"Only {trades} completed trade(s) - too few for any ratio here to "
            "separate skill from luck."
        )
    if span < timedelta(days=180):
        notes.append(
            f"The window is {span.days} days. Short windows flatter whichever "
            "direction happened to dominate them."
        )
    return notes
