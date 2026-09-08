"""Turning a liquidity-sweep run into the report section 28 asks for.

Kept apart from the runner because they answer different questions and fail in
different ways: the runner is arithmetic about one trade at a time and has to
be right, and this is arithmetic about a population and has to be *honest* -
which mostly means saying how many observations are behind each number.

Two rules borrowed from `reporting.metrics`, which this builds on rather than
replaces:

**A statistic that cannot be computed is `None`, not zero.** A profit factor of
0.00 reads as "loses everything" and `None` reads as "no losing trades to
divide by". The renderers print a dash.

**Sample size travels with every ratio.** Every breakdown row carries its trade
count, and the renderer prints it next to the win rate rather than in a
footnote, because a 100% win rate over two trades and over two hundred are not
the same claim and should not look alike.

The daily equity curve, not the per-bar one, is what the risk ratios are
computed from. A five-minute curve is flat on ninety-nine bars in a hundred, so
its standard deviation measures how often the strategy was *in* a trade rather
than how variable the trades were, and annualising it needs a number of periods
per year that nobody would recognise. One point per trading day and 252 of them
is the convention every published Sharpe uses.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from algo.backtest.liquidity_sweep_runner import ExitReason, SweepResult, SweepTrade
from algo.core.enums import Side
from algo.core.timeutil import iso
from algo.portfolio.book import EquityPoint
from algo.reporting.metrics import Metrics
from algo.reporting.metrics import compute as compute_metrics
from algo.strategy.liquidity_sweep import SIGNAL_LOG_COLUMNS, SetupState, trading_day

TRADING_DAYS_PER_YEAR = Decimal("252")


# ------------------------------------------------------------------ grouping


@dataclass(frozen=True, slots=True)
class GroupStats:
    """One row of a breakdown. Every field is per-group, nothing is shared."""

    label: str
    trades: int
    wins: int
    win_rate: Decimal | None
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_r: Decimal | None
    median_r: Decimal | None
    expectancy: Decimal | None

    @property
    def losses(self) -> int:
        return self.trades - self.wins


def group_stats(label: str, trades: Sequence[SweepTrade]) -> GroupStats:
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    return GroupStats(
        label=label,
        trades=len(trades),
        wins=len(wins),
        win_rate=(
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal("100") if trades else None
        ),
        net_pnl=sum(nets, Decimal("0")),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        average_r=(mean(rs) if rs else None),
        median_r=(median(rs) if rs else None),
        # Expectancy in R is the mean R: the average outcome of taking the
        # trade, in units of what it risked. Reported alongside the mean rather
        # than instead of it because they are the same number and readers look
        # for both names.
        expectancy=(mean(rs) if rs else None),
    )


def breakdown(
    trades: Sequence[SweepTrade],
    key: Callable[[SweepTrade], str],
    *,
    order: Sequence[str] | None = None,
) -> list[GroupStats]:
    """Group `trades` by `key` and compute a row per group.

    `order` fixes the row order where the categories are known in advance, so
    two runs' tables line up and can be read side by side.
    """
    buckets: dict[str, list[SweepTrade]] = {}
    for trade in trades:
        buckets.setdefault(key(trade), []).append(trade)
    labels = list(order) if order is not None else sorted(buckets)
    return [group_stats(label, buckets.get(label, [])) for label in labels if label in buckets]


def mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


# ------------------------------------------------------------------ summary


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Everything section 28 asks for about one run."""

    label: str
    bars: int
    days: int
    starting_equity: Decimal
    final_equity: Decimal
    orders_placed: int
    orders_filled: int
    orders_expired: int
    orders_unaffordable: int
    setups_seen: int

    trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    total_costs: Decimal
    profit_factor: Decimal | None
    average_r: Decimal | None
    median_r: Decimal | None
    expectancy_r: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    largest_win: Decimal | None
    largest_loss: Decimal | None
    consecutive_wins: int
    consecutive_losses: int
    average_duration: timedelta
    max_duration: timedelta
    trades_per_day: Decimal | None
    exits: dict[str, int]
    metrics: Metrics

    def r_histogram(self, buckets: Sequence[Decimal]) -> list[tuple[str, int]]:
        """Counts of R in half-open bins, plus one bin for everything beyond."""
        counts = [0] * (len(buckets) + 1)
        for value in self._r_values:
            placed = False
            for index, edge in enumerate(buckets):
                if value < edge:
                    counts[index] += 1
                    placed = True
                    break
            if not placed:
                counts[-1] += 1
        labels = [f"< {edge:+.1f}R" for edge in buckets] + [f">= {buckets[-1]:+.1f}R"]
        return list(zip(labels, counts, strict=True))

    _r_values: tuple[Decimal, ...] = ()


def summarise(result: SweepResult, *, label: str) -> RunSummary:
    trades = result.trades
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    rs = tuple(t.r_multiple for t in trades if t.r_multiple is not None)

    best_win_streak = worst_loss_streak = win_streak = loss_streak = 0
    for net in nets:
        win_streak = win_streak + 1 if net > 0 else 0
        loss_streak = loss_streak + 1 if net <= 0 else 0
        best_win_streak = max(best_win_streak, win_streak)
        worst_loss_streak = max(worst_loss_streak, loss_streak)

    durations = [t.holding_time for t in trades]
    days = len({trading_day(ts) for ts, _equity, _open in result.equity_curve})
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))

    return RunSummary(
        label=label,
        bars=result.bars_seen,
        days=days,
        starting_equity=result.starting_equity,
        final_equity=result.final_equity,
        orders_placed=result.orders_placed,
        orders_filled=result.orders_filled,
        orders_expired=result.orders_expired,
        orders_unaffordable=result.orders_unaffordable,
        setups_seen=len(result.records),
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=(
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal("100") if trades else None
        ),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=result.net_pnl,
        total_costs=result.total_costs,
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        average_r=(mean(rs) if rs else None),
        median_r=(median(rs) if rs else None),
        expectancy_r=(mean(rs) if rs else None),
        average_win=(mean(wins) if wins else None),
        average_loss=(mean(losses) if losses else None),
        largest_win=(max(wins) if wins else None),
        largest_loss=(min(losses) if losses else None),
        consecutive_wins=best_win_streak,
        consecutive_losses=worst_loss_streak,
        average_duration=(
            sum(durations, timedelta(0)) / len(durations) if durations else timedelta(0)
        ),
        max_duration=(max(durations) if durations else timedelta(0)),
        trades_per_day=(Decimal(len(trades)) / Decimal(days) if days else None),
        exits=dict(
            Counter(
                t.exit_reason.value if t.exit_reason is not None else "OPEN" for t in trades
            )
        ),
        metrics=compute_metrics(
            daily_curve(result),
            trade_count=len(trades),
            total_cost=result.total_costs,
            periods_per_year=TRADING_DAYS_PER_YEAR,
        ),
        _r_values=rs,
    )


def daily_curve(result: SweepResult) -> tuple[EquityPoint, ...]:
    """One equity point per trading day - see the module docstring.

    `open_positions` carries whether a position was held at any point that day,
    so `metrics.compute`'s exposure figure means "days with a position on"
    rather than "bars", which is the version a person can act on.
    """
    by_day: dict[date, tuple[Decimal, int]] = {}
    for ts, equity, held in result.equity_curve:
        day = trading_day(ts)
        previous = by_day.get(day)
        exposed = held or (previous[1] if previous else 0)
        by_day[day] = (equity, exposed)

    points: list[EquityPoint] = []
    for day in sorted(by_day):
        equity, exposed = by_day[day]
        points.append(
            EquityPoint(
                ts=_noon(day),
                cash=equity,
                market_value=Decimal("0"),
                equity=equity,
                realised_pnl=equity - result.starting_equity,
                unrealised_pnl=Decimal("0"),
                charges=Decimal("0"),
                open_positions=exposed,
            )
        )
    return tuple(points)


def _noon(day: date) -> object:
    from datetime import UTC, datetime, time

    return datetime.combine(day, time(12, 0), tzinfo=UTC)


# ------------------------------------------------------------------- logging


def write_signal_log(result: SweepResult, path: Path) -> Path:
    """Every setup the strategy considered, in the order it considered them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SIGNAL_LOG_COLUMNS))
        writer.writeheader()
        for record in result.records:
            writer.writerow(record.to_row())
    return path


def write_trade_log(result: SweepResult, path: Path) -> Path:
    """Every round trip, with the setup that produced it on the same row."""
    from algo.backtest.liquidity_sweep_runner import trade_context_row

    rows = [trade_context_row(trade) for trade in result.trades]
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


#: Marker kinds, matching the labels section 34 asks for.
MARKER_COLUMNS: tuple[str, ...] = ("timestamp", "kind", "price", "direction", "label")


def write_markers(result: SweepResult, path: Path) -> Path:
    """Chart annotations, as data.

    Section 34 asks for markers on a chart. This project has no candlestick
    chart to draw them on - the tearsheet is an equity curve and a histogram,
    deliberately dependency-free - so what is produced instead is the
    annotation *list*: one row per event, typed and timestamped, which any
    charting tool can consume and a person can read directly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    for record in result.records:
        if record.signal_status is not SetupState.WAITING_FOR_RETRACE:
            continue
        stamp = iso(record.timestamp)
        rows.append(
            {
                "timestamp": stamp,
                "kind": "SWEEP",
                "price": str(record.sweep_price),
                "direction": record.direction.value,
                "label": f"{record.liquidity_type.value} @ {record.liquidity_level}",
            }
        )
        if record.mss_level is not None:
            rows.append(
                {
                    "timestamp": stamp,
                    "kind": "MSS",
                    "price": str(record.mss_level),
                    "direction": record.direction.value,
                    "label": "close through the last confirmed swing",
                }
            )
        if record.fvg_low is not None and record.fvg_high is not None:
            rows.append(
                {
                    "timestamp": stamp,
                    "kind": "FVG",
                    "price": str(record.fvg_low),
                    "direction": record.direction.value,
                    "label": f"gap {record.fvg_low} - {record.fvg_high}",
                }
            )
    for trade in result.trades:
        rows.append(
            {
                "timestamp": iso(trade.entry_ts),
                "kind": "ENTRY",
                "price": str(trade.entry_price),
                "direction": trade.side.value,
                "label": f"{trade.lots} oz, risk {trade.risk_amount:.2f}",
            }
        )
        rows.append(
            {
                "timestamp": iso(trade.entry_ts),
                "kind": "SL",
                "price": str(trade.stop_exec),
                "direction": trade.side.value,
                "label": "stop",
            }
        )
        rows.append(
            {
                "timestamp": iso(trade.entry_ts),
                "kind": "TP",
                "price": str(trade.target_exec),
                "direction": trade.side.value,
                "label": "target",
            }
        )
        if trade.exit_ts is not None and trade.exit_price is not None:
            kind = (
                "TIME_EXIT"
                if trade.exit_reason is ExitReason.MAX_HOLD
                else (trade.exit_reason.value if trade.exit_reason else "EXIT")
            )
            rows.append(
                {
                    "timestamp": iso(trade.exit_ts),
                    "kind": kind,
                    "price": str(trade.exit_price),
                    "direction": trade.side.value,
                    "label": f"net {trade.net_pnl:.2f}",
                }
            )
    rows.sort(key=lambda row: (row["timestamp"], row["kind"]))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MARKER_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


# ----------------------------------------------------------------- rendering


def dash(value: Decimal | None, places: int = 2, suffix: str = "") -> str:
    """A number, or a dash where there is no number to print."""
    if value is None:
        return "-"
    return f"{value:,.{places}f}{suffix}"


def render_summary(summary: RunSummary) -> str:
    m = summary.metrics
    drawdown = m.max_drawdown
    lines = [
        f"{summary.label}",
        "-" * len(summary.label),
        f"  bars                {summary.bars:,}   over {summary.days:,} trading days",
        f"  setups considered   {summary.setups_seen:,}",
        f"  orders placed       {summary.orders_placed:,}"
        f"   (filled {summary.orders_filled:,}, expired {summary.orders_expired:,}, "
        f"unaffordable {summary.orders_unaffordable:,})",
        "",
        f"  trades              {summary.trades:,}"
        f"   ({summary.wins} W / {summary.losses} L)",
        f"  win rate            {dash(summary.win_rate, 1, '%')}",
        f"  gross profit        {dash(summary.gross_profit)}",
        f"  gross loss          {dash(summary.gross_loss)}",
        f"  net profit          {dash(summary.net_pnl)}",
        f"  costs               {dash(summary.total_costs)}",
        f"  profit factor       {dash(summary.profit_factor)}",
        f"  average R           {dash(summary.average_r, 3, 'R')}",
        f"  median R            {dash(summary.median_r, 3, 'R')}",
        f"  expectancy          {dash(summary.expectancy_r, 3, 'R')} a trade",
        f"  average win         {dash(summary.average_win)}",
        f"  average loss        {dash(summary.average_loss)}",
        f"  largest win         {dash(summary.largest_win)}",
        f"  largest loss        {dash(summary.largest_loss)}",
        f"  consecutive wins    {summary.consecutive_wins}",
        f"  consecutive losses  {summary.consecutive_losses}",
        f"  average duration    {_hm(summary.average_duration)}",
        f"  longest trade       {_hm(summary.max_duration)}",
        f"  trades a day        {dash(summary.trades_per_day, 2)}",
        f"  starting equity     {dash(summary.starting_equity)}",
        f"  final equity        {dash(summary.final_equity)}",
        f"  return              {dash(m.return_pct, 2, '%')}",
        f"  max drawdown        "
        + (
            f"{dash(drawdown.depth)} ({dash(drawdown.depth_pct, 2, '%')}), "
            f"{drawdown.duration.days}d"
            if drawdown is not None
            else "-"
        ),
        f"  Sharpe (daily)      {dash(m.sharpe, 2)}",
        f"  Sortino (daily)     {dash(m.sortino, 2)}",
        f"  exposure            {dash(m.exposure_pct, 1, '%')} of days",
        "  exits               "
        + ", ".join(f"{name} {count}" for name, count in sorted(summary.exits.items())),
    ]
    if summary.trades < 30:
        lines.append(
            f"\n  NOTE: {summary.trades} trades. Every ratio above is a small-sample "
            "figure and cannot separate skill from luck."
        )
    return "\n".join(lines)


def render_breakdown(title: str, rows: Sequence[GroupStats]) -> str:
    head = (
        f"{title}\n"
        f"{'group':<18}{'trades':>8}{'win%':>8}{'net':>12}"
        f"{'PF':>8}{'avg R':>9}{'med R':>9}"
    )
    body = [
        f"{row.label:<18}{row.trades:>8}{dash(row.win_rate, 1):>8}"
        f"{dash(row.net_pnl):>12}{dash(row.profit_factor):>8}"
        f"{dash(row.average_r, 3):>9}{dash(row.median_r, 3):>9}"
        for row in rows
    ]
    return "\n".join([head, "-" * 72, *body])


def render_r_distribution(summary: RunSummary) -> str:
    buckets = [Decimal(x) for x in ("-1.5", "-1.0", "-0.5", "0", "0.5", "1.0", "1.5", "2.0")]
    rows = summary.r_histogram(buckets)
    widest = max((count for _label, count in rows), default=0)
    lines = ["R-multiple distribution"]
    for label, count in rows:
        bar = "#" * int(count * 40 / widest) if widest else ""
        lines.append(f"  {label:>10}  {count:>5}  {bar}")
    return "\n".join(lines)


def _hm(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def session_key(trade: SweepTrade) -> str:
    return trade.session or "UNKNOWN"


def source_key(trade: SweepTrade) -> str:
    return trade.source.value


def direction_key(trade: SweepTrade) -> str:
    return "LONG" if trade.side is Side.BUY else "SHORT"


def year_key(trade: SweepTrade) -> str:
    return f"{trade.entry_ts:%Y}"


def month_key(trade: SweepTrade) -> str:
    return f"{trade.entry_ts:%Y-%m}"
