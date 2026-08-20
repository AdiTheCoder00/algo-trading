"""Performance metrics. Brief §10.

Two rules run through this module.

**Sample size travels with every ratio.** A Sharpe ratio printed on its own
implies a confidence the data cannot support, and at roughly twelve expiry cycles
a year this strategy will not have enough trades to distinguish skill from luck
for a long time. So `trade_count` is a field of the same object, and the summary
never renders a ratio without it.

**A metric that cannot be computed is `None`, not zero.** A Sharpe ratio of 0.0
reads as "no edge"; `None` reads as "not enough data", which is the truth when
there are three observations. Sortino with no losing periods is `None` rather
than infinity for the same reason.

R-multiples arrive with the trade log. R is the configured stop in rupees
(assumption 7.4), **not** the maximum possible loss — a short strangle's maximum
loss is unbounded, so an R measured against it would be meaningless. A trade with
no stop set carries no R-multiple, and the statistics that need one say how many
trades they could actually use rather than silently averaging a subset.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

from algo.core.trade import Trade
from algo.portfolio.book import EquityPoint

#: MCX non-agri sessions run 14.5 hours, five days a week — roughly 250 a year.
TRADING_DAYS_PER_YEAR = Decimal("250")


@dataclass(frozen=True, slots=True)
class Drawdown:
    depth: Decimal
    depth_pct: Decimal
    duration: timedelta
    peak_equity: Decimal
    trough_equity: Decimal


@dataclass(frozen=True, slots=True)
class TradeStats:
    """Brief §10, the half that needs completed round trips.

    Every field is `None` when it cannot be computed rather than 0. A profit
    factor of 0.0 reads as "loses everything"; `None` reads as "no losing trades
    to divide by", which is a different statement.
    """

    trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    largest_win: Decimal | None
    largest_loss: Decimal | None
    longest_losing_streak: int
    #: How many trades carried an R-multiple. The R statistics use only these.
    trades_with_r: int
    expectancy_r: Decimal | None
    average_win_r: Decimal | None
    average_loss_r: Decimal | None
    r_multiples: tuple[Decimal, ...]

    def histogram(self, buckets: int = 9) -> list[tuple[str, int]]:
        """R-multiple distribution, for the tearsheet.

        Brief §10 asks for the distribution rather than just a mean, because a
        premium-selling strategy's shape — many small wins, rare large losses —
        is invisible in an average and obvious in a histogram.
        """
        if not self.r_multiples:
            return []
        low = min(self.r_multiples)
        high = max(self.r_multiples)
        if high == low:
            return [(f"{low:+.2f}R", len(self.r_multiples))]
        width = (high - low) / Decimal(buckets)
        counts = [0] * buckets
        for value in self.r_multiples:
            index = int((value - low) / width)
            counts[min(index, buckets - 1)] += 1
        return [
            (f"{low + width * Decimal(i):+.2f}R", counts[i]) for i in range(buckets)
        ]


def trade_stats(trades: Sequence[Trade]) -> TradeStats:
    """Compute the round-trip statistics from a trade log."""
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))

    streak = worst_streak = 0
    for net in nets:
        streak = streak + 1 if net < 0 else 0
        worst_streak = max(worst_streak, streak)

    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    win_rs = [r for r in rs if r > 0]
    loss_rs = [r for r in rs if r <= 0]

    return TradeStats(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=(
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal("100") if trades else None
        ),
        # None, not zero: no losing trades means nothing to divide by, which is
        # not the same as "made nothing".
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        longest_losing_streak=worst_streak,
        trades_with_r=len(rs),
        expectancy_r=(sum(rs, Decimal("0")) / Decimal(len(rs)) if rs else None),
        average_win_r=(sum(win_rs, Decimal("0")) / Decimal(len(win_rs)) if win_rs else None),
        average_loss_r=(
            sum(loss_rs, Decimal("0")) / Decimal(len(loss_rs)) if loss_rs else None
        ),
        r_multiples=tuple(rs),
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    """The §10 metric set, as far as it can honestly be computed."""

    trade_count: int
    bars: int
    net_pnl: Decimal
    gross_pnl: Decimal
    total_cost: Decimal
    cost_drag_pct: Decimal | None
    starting_equity: Decimal
    final_equity: Decimal
    return_pct: Decimal
    max_drawdown: Drawdown | None
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    exposure_pct: Decimal
    periods_per_year: Decimal
    trade: TradeStats | None = None

    def summary(self) -> str:
        """One block of text, with the caveats attached rather than in a footnote."""
        lines = [
            f"trades            {self.trade_count}",
            f"bars              {self.bars}",
            f"net P&L           {self.net_pnl:,}",
            f"gross P&L         {self.gross_pnl:,}",
            f"total cost        {self.total_cost:,}",
            f"cost drag         {_or_dash(self.cost_drag_pct, '%')}",
            f"return            {self.return_pct:.4f}%",
            f"exposure          {self.exposure_pct:.1f}%",
        ]
        if self.max_drawdown is not None:
            lines.append(
                f"max drawdown      {self.max_drawdown.depth:,} "
                f"({self.max_drawdown.depth_pct:.4f}%), "
                f"{self.max_drawdown.duration.days}d"
            )
        lines.extend(
            [
                f"Sharpe            {_or_dash(self.sharpe)}",
                f"Sortino           {_or_dash(self.sortino)}",
                f"Calmar            {_or_dash(self.calmar)}",
            ]
        )
        if self.trade is not None and self.trade.trades:
            stats = self.trade
            lines.extend(
                [
                    "",
                    f"round trips       {stats.trades} "
                    f"({stats.wins}W / {stats.losses}L)",
                    f"win rate          {_or_dash(stats.win_rate, '%')}",
                    f"profit factor     {_or_dash(stats.profit_factor)}",
                    f"largest win       {_or_dash(stats.largest_win)}",
                    f"largest loss      {_or_dash(stats.largest_loss)}",
                    f"losing streak     {stats.longest_losing_streak}",
                    f"expectancy        {_or_dash(stats.expectancy_r, 'R')}"
                    f"   (from {stats.trades_with_r} trades with a stop)",
                    f"average win       {_or_dash(stats.average_win_r, 'R')}",
                    f"average loss      {_or_dash(stats.average_loss_r, 'R')}",
                ]
            )

        if self.trade_count < 30:
            lines.append(
                f"\n  NOTE: {self.trade_count} trades. Ratios above cannot distinguish "
                "skill from luck at this sample size."
            )
        return "\n".join(lines)


def compute(
    curve: tuple[EquityPoint, ...],
    *,
    trade_count: int,
    total_cost: Decimal,
    trades: Sequence[Trade] = (),
    periods_per_year: Decimal = TRADING_DAYS_PER_YEAR,
) -> Metrics:
    """Derive the metric set from an equity curve."""
    if not curve:
        raise ValueError("cannot compute metrics from an empty equity curve")

    start = curve[0].equity
    # The curve's first point is recorded before anything has traded, so the
    # opening equity is the account's starting balance.
    starting_equity = start
    final = curve[-1].equity
    net = final - starting_equity
    gross = net + total_cost

    returns = _period_returns(curve)
    exposure = _exposure_pct(curve)
    drawdown = _max_drawdown(curve)

    sharpe = _sharpe(returns, periods_per_year)
    sortino = _sortino(returns, periods_per_year)
    calmar = _calmar(net, starting_equity, drawdown)

    return Metrics(
        trade_count=trade_count,
        bars=len(curve),
        net_pnl=net,
        gross_pnl=gross,
        total_cost=total_cost,
        cost_drag_pct=(total_cost / abs(gross) * Decimal("100")) if gross != 0 else None,
        starting_equity=starting_equity,
        final_equity=final,
        return_pct=net / starting_equity * Decimal("100"),
        max_drawdown=drawdown,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        exposure_pct=exposure,
        periods_per_year=periods_per_year,
        trade=trade_stats(trades) if trades else None,
    )


def _period_returns(curve: tuple[EquityPoint, ...]) -> list[Decimal]:
    out: list[Decimal] = []
    for earlier, later in pairwise(curve):
        if earlier.equity == 0:
            continue
        out.append((later.equity - earlier.equity) / earlier.equity)
    return out


def _exposure_pct(curve: tuple[EquityPoint, ...]) -> Decimal:
    if not curve:
        return Decimal("0")
    exposed = sum(1 for point in curve if point.open_positions > 0)
    return Decimal(exposed) / Decimal(len(curve)) * Decimal("100")


def _max_drawdown(curve: tuple[EquityPoint, ...]) -> Drawdown | None:
    """Depth and duration. Brief §10 asks for both — a shallow drawdown lasting
    two years is a different experience from a deep one lasting a week."""
    peak = curve[0].equity
    peak_ts = curve[0].ts
    worst: Drawdown | None = None

    for point in curve:
        if point.equity > peak:
            peak = point.equity
            peak_ts = point.ts
            continue
        depth = peak - point.equity
        if depth <= 0:
            continue
        depth_pct = depth / peak * Decimal("100") if peak != 0 else Decimal("0")
        if worst is None or depth > worst.depth:
            worst = Drawdown(
                depth=depth,
                depth_pct=depth_pct,
                duration=point.ts - peak_ts,
                peak_equity=peak,
                trough_equity=point.equity,
            )
    return worst


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _stdev(values: list[Decimal], mean: Decimal) -> Decimal:
    """Sample standard deviation, via Decimal's own square root.

    `Decimal.sqrt` rather than `math.sqrt` so the result does not round-trip
    through a float on its way into a reported ratio.
    """
    variance = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(len(values) - 1)
    return variance.sqrt()


def _sharpe(returns: list[Decimal], periods_per_year: Decimal) -> Decimal | None:
    """Annualised, excess over zero. `None` below four observations.

    No risk-free rate is subtracted. For a strategy measured over weeks that
    choice changes the number materially, so it is stated rather than assumed —
    and revisited when there is enough data for the number to mean anything.
    """
    if len(returns) < 4:
        return None
    mean = _mean(returns)
    deviation = _stdev(returns, mean)
    if deviation == 0:
        return None
    return mean / deviation * periods_per_year.sqrt()


def _sortino(returns: list[Decimal], periods_per_year: Decimal) -> Decimal | None:
    """Like Sharpe but penalising only downside deviation."""
    if len(returns) < 4:
        return None
    mean = _mean(returns)
    downside = [r for r in returns if r < 0]
    if len(downside) < 2:
        # No losing periods is not an infinite Sortino, it is not enough data.
        return None
    deviation = _stdev(downside, Decimal("0"))
    if deviation == 0:
        return None
    return mean / deviation * periods_per_year.sqrt()


def _calmar(net: Decimal, starting_equity: Decimal, drawdown: Drawdown | None) -> Decimal | None:
    if drawdown is None or drawdown.depth == 0 or starting_equity == 0:
        return None
    return (net / starting_equity) / (drawdown.depth / starting_equity)


def _or_dash(value: Decimal | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}{suffix}"
