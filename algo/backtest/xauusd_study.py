"""Shared plumbing for the XAUUSD scalper studies.

Two scripts run this strategy - the baseline backtest and the stop-loss study -
and both need the same dataset loaded the same way, the same cost ladder, the
same trading-day boundary and the same comparison table. Duplicating any of
that would let the two disagree about what "realistic costs" means while
printing numbers side by side, which is the drift `strategy_for` in
`mt5_runner.py` and `cfd_runner`'s own docstring both argue against.

Nothing here decides anything about the strategy. The rules live in
`algo/strategy/rsi_stoch_reversal.py` and the execution in
`algo/backtest/xauusd_runner.py`; this is the harness they are run inside.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from algo.backtest.cfd_runner import CfdCosts
from algo.backtest.xauusd_runner import Indicators, ScalperResult, run_scalper
from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.costs.slippage import TickSlippage
from algo.data.dukascopy import MeasuredSpread, load_spread_series, regrid_bars
from algo.data.econ_calendar import EconomicCalendar
from algo.data.mt5_history import fetch_history, resolve_server_offset
from algo.data.mt5_spread import load_profile
from algo.data.parquet_feed import read_parquet_bars
from algo.reporting.scalper_report import Summary
from algo.strategy.rsi_stoch_reversal import ScalperParams

M5 = Timeframe(minutes=5)
H1 = Timeframe(minutes=60)

#: XAUUSD quotes to three decimals, so slippage in "ticks" is in thousandths.
TICK = Decimal("0.001")

#: A plausible account for 0.01 lots. Only the drawdown *percentage* and the
#: return depend on it; every dollar figure does not.
ACCOUNT = Decimal("1000")

#: Bars per year on a 24/5 five-minute grid - 288 a day, five days a week. Used
#: only to annualise Sharpe and Sortino on the bar-resolution equity curve.
BARS_PER_YEAR = Decimal(288 * 5 * 52)

#: The flat half-spread measured on the live Vantage account (D-121). Used only
#: where a per-bar measurement does not exist - see `mt5_m5_bars`.
D121_HALF_SPREAD = Decimal("0.145")


@dataclass(frozen=True, slots=True)
class Dataset:
    """One window of bars, with the spread that was quoted inside it."""

    m5: list[Bar]
    h1: list[Bar]
    half_spread: MeasuredSpread
    #: Bars dropped because they sat past a hole in the archive.
    dropped: int
    label: str


def load_dataset(data: Path) -> Dataset:
    """The Dukascopy-derived window, truncated at the first hole in the archive."""
    m5_all = read_parquet_bars(data / "m5.parquet", M5)
    h1_all = read_parquet_bars(data / "h1.parquet", H1)
    m5 = contiguous(m5_all)
    h1 = [b for b in h1_all if m5[0].ts <= b.ts <= m5[-1].ts]

    profile = load_profile("XAUUSD", data / "spread.json")
    if profile is None:
        raise DataError(f"no spread profile at {data / 'spread.json'}")

    return Dataset(
        m5=m5,
        h1=h1,
        half_spread=MeasuredSpread(load_spread_series(data / "m5_spread.csv"), profile),
        dropped=len(m5_all) - len(m5),
        label=f"Dukascopy {m5[0].ts:%Y-%m} .. {m5[-1].ts:%Y-%m}",
    )


def contiguous(bars: list[Bar], *, tolerance: timedelta = timedelta(days=7)) -> list[Bar]:
    """The longest run of bars with no gap wider than `tolerance`.

    A weekend is 2 days and the two Easter closes in this archive are 3, so a
    week's tolerance keeps every real market closure and cuts only a hole in the
    data. The caller reports what was dropped either way - a silently shortened
    study is a study about a period nobody chose.
    """
    if not bars:
        return []
    runs: list[list[Bar]] = [[bars[0]]]
    for previous, bar in pairwise(bars):
        if bar.ts - previous.ts > tolerance:
            runs.append([])
        runs[-1].append(bar)
    return max(runs, key=len)


def cost_scenarios(half: MeasuredSpread) -> dict[str, tuple[CfdCosts, TickSlippage]]:
    """The three cost levels, of which only the first is a measurement.

      realistic     the spread actually quoted inside each bar, zero commission
                    (verified on the Vantage account, D-121), measured swap, and
                    $0.05 of slippage on a stop only
      moderate      1.5x the measured spread, $0.10 market / $0.25 stop slippage
      conservative  2.5x the measured spread, $0.20 market / $0.50 stop slippage,
                    plus $0.03 a fill of commission - a RAW/ECN tier's $3 a lot,
                    at 0.01 lots

    The last two are stress tests. Their point is not to be right but to say how
    much worse a venue would have to be before the answer changes.
    """
    swap = SwapModel.vantage_xauusd()
    return {
        "realistic": (
            CfdCosts(
                half_spread_at=half, swap=swap, commission=CfdChargeModel.vantage_standard()
            ),
            TickSlippage(market_ticks=0, stop_ticks=50),
        ),
        "moderate": (
            CfdCosts(
                half_spread_at=MeasuredSpread(
                    half.by_ts, half.profile, multiplier=Decimal("1.5")
                ),
                swap=swap,
                commission=CfdChargeModel.vantage_standard(),
            ),
            TickSlippage(market_ticks=100, stop_ticks=250),
        ),
        "conservative": (
            CfdCosts(
                half_spread_at=MeasuredSpread(
                    half.by_ts, half.profile, multiplier=Decimal("2.5")
                ),
                swap=swap,
                commission=CfdChargeModel(commission_per_lot=Decimal("0.03")),
            ),
            TickSlippage(market_ticks=200, stop_ticks=500),
        ),
    }


def free_costs() -> tuple[CfdCosts, TickSlippage]:
    """Zero of everything - the reference row, never a result."""
    return (
        CfdCosts(
            half_spread=Decimal("0"),
            swap=SwapModel(
                long_points=Decimal("0"), short_points=Decimal("0"), point_value=Decimal("0.01")
            ),
            commission=CfdChargeModel(),
        ),
        TickSlippage(market_ticks=0, stop_ticks=0),
    )


def run(
    m5: list[Bar],
    h1: list[Bar],
    params: ScalperParams,
    costs: CfdCosts,
    slippage: TickSlippage,
    calendar: EconomicCalendar,
    indicators: Indicators | None = None,
) -> ScalperResult:
    """One run, with this study's fixed conventions applied in one place."""
    return run_scalper(
        m5,
        h1,
        params=params,
        costs=costs,
        calendar=calendar,
        slippage=slippage,
        tick=TICK,
        starting_equity=ACCOUNT,
        indicators=indicators,
    )


def bar_range(bars: list[Bar]) -> Decimal:
    """The median high-to-low of a bar series.

    A $10 stop is not a fixed amount of risk; it is a fixed *distance*, and what
    that distance means depends entirely on how far the market moves in five
    minutes. This is the number that makes two windows comparable.
    """
    ranges = sorted(bar.high - bar.low for bar in bars)
    return ranges[len(ranges) // 2]


def close_labelled(bars: list[Bar]) -> list[Bar]:
    """Shift MT5's open-stamped bars onto the engine's close-stamped convention.

    `algo.core.bar.Bar` states its rule plainly: "a bar with `ts = 09:30` covers
    the half-open interval (09:00, 09:30]". MT5's `time` field is the bar's
    **opening** instant - confirmed against this archive, where an MT5 H1 bar
    stamped 09:00 matches the Dukascopy bar covering 09:00-10:00. Feeding those
    straight in would put every bar one interval early, and the H1 trend would
    then be read from an hour that had not closed. That is precisely the
    look-ahead the rest of this study is arranged to make impossible, so the
    conversion is explicit and here rather than assumed anywhere.

    Done here rather than in `algo/data/mt5_history.py`: that module also feeds
    the live loop, and changing what a timestamp means there is a change to
    trading behaviour that nobody asked for in a backtest.
    """
    return [
        bar.model_copy(update={"ts": bar.ts + timedelta(minutes=bar.timeframe.minutes)})
        for bar in bars
    ]


def mt5_m5_bars(count: int = 50_000) -> tuple[list[Bar], list[Bar], Any] | None:
    """M5 and H1 bars from the live terminal, or None with the reason unprinted.

    The window this reaches (2025-12 onward) is later than the tick archive and
    does not overlap it, which is what makes it a holdout rather than a slice.
    Returns the resolved server offset alongside so a caller can report it.
    """
    try:
        import MetaTrader5 as terminal
    except ImportError:
        return None
    if not terminal.initialize():
        return None
    terminal.symbol_select("XAUUSD", True)
    try:
        resolved = resolve_server_offset(terminal, "XAUUSD")
        bars = close_labelled(
            fetch_history(
                terminal, symbol="XAUUSD", timeframe=M5, count=count, offset=resolved.offset
            )
        )
    except DataError:
        return None
    return bars, regrid_bars(bars, H1), resolved


def mt5_costs() -> CfdCosts:
    """What the holdout window can honestly be charged.

    The terminal refuses tick history for that period, so the spread cannot be
    measured per bar the way the main window's is. This uses the flat D-121
    half-spread - a real measurement from a live quote, but one number standing
    in for eight months.

    MT5 bars are **bid**, not mid. Treating them as mid and charging half a
    spread per fill is nevertheless right on a round trip: a long under-charges
    by half going in and over-charges by half coming out, and the two cancel
    exactly. The residue is where the stop sits relative to the true bid - half
    a spread, about seven cents.
    """
    return CfdCosts(
        half_spread=D121_HALF_SPREAD,
        swap=SwapModel.vantage_xauusd(),
        commission=CfdChargeModel.vantage_standard(),
    )


# ---------------------------------------------------------------- rendering


HEADER = (
    f"    {'':<26}{'trades':>7}{'win%':>9}{'net $':>12}{'PF':>9}{'exp $':>10}{'maxDD':>11}"
)


def line(summary: Summary) -> str:
    """One row of a comparison table. Deliberately the same fields every time."""
    return (
        f"    {summary.label:<26}{summary.trades:>7}"
        f"{pct(summary.win_rate):>9}"
        f"{money(summary.net_pnl):>12}"
        f"{num(summary.profit_factor):>9}"
        f"{money(summary.expectancy):>10}"
        f"{money(-summary.max_drawdown):>11}"
    )


def pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def num(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def money(value: Decimal | None) -> str:
    """A signed quantity - a P&L, an expectancy, an excursion."""
    return "n/a" if value is None else f"{value:+,.2f}"


def amount(value: Decimal | None) -> str:
    """A magnitude - a cost, a drawdown depth. A forced `+` on those reads wrong."""
    return "n/a" if value is None else f"{value:,.2f}"


def wrap(text: str, width: int = 86) -> list[str]:
    rows: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + len(word) + 1 > width:
            rows.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        rows.append(current)
    return rows
