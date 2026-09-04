"""What a scalper run produced, in the shape §15 and §16 of the brief ask for.

`reporting/metrics.py` computes the general set - Sharpe, Sortino, drawdown,
profit factor - and is used unchanged. This module adds the things that are
specific to *this* strategy and have nowhere else to live: the exit-reason split,
the hour-of-day and session tables, the excursion statistics, and the
translation from a `ScalperTrade` into the `core.trade.Trade` the exporter and
the tearsheet already know how to render.

## Two P&L bases, and why both are reported

`ScalperTrade.gross_pnl` is mid-to-mid: what the trade would have paid with no
costs at all. `Trade.gross_pnl` on the exported record is the **executed** round
trip, which already has the spread and slippage in it, with commission and swap
as itemised `Charges`. Both produce the same `net_pnl`, which is the number that
matters, and having both means "the costs took X" is a subtraction anyone can
check rather than an assertion.

## Gross profit and gross loss are net-of-cost sums

That is `trade_stats`' existing convention (it sums `net_pnl` over winners and
losers) and it is the one profit factor is normally quoted on. Stated here
because "gross profit" reads as though it should be before costs, and it is not.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from algo.backtest.xauusd_runner import XAUUSD, ScalperResult, ScalperTrade
from algo.core.enums import Side
from algo.core.fill import Charges
from algo.core.trade import Trade, TradeLeg
from algo.portfolio.book import EquityPoint
from algo.strategy.rsi_stoch_reversal import ScalperParams

#: Hour-of-day bands, in UTC, named for the desk that dominates them. Boundaries
#: are the broker's own clock rather than local market opens: the 21:00 UTC
#: rollover is where a CFD session ends (`ForexCalendar`), so a band that
#: straddled it would mix two trading days.
SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("Sydney/roll 21-00", 21, 24),
    ("Asia 00-07", 0, 7),
    ("London 07-12", 7, 12),
    ("London/NY 12-16", 12, 16),
    ("New York 16-21", 16, 21),
)


def session_of(ts: datetime) -> str:
    hour = ts.hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return SESSIONS[0][0]


@dataclass(frozen=True, slots=True)
class Bucket:
    """One slice of the trade population - a side, an hour, a month, a year."""

    label: str
    trades: int
    wins: int
    net: Decimal

    @property
    def win_rate(self) -> Decimal | None:
        if not self.trades:
            return None
        return Decimal(self.wins) / Decimal(self.trades) * Decimal("100")

    @property
    def expectancy(self) -> Decimal | None:
        if not self.trades:
            return None
        return self.net / Decimal(self.trades)


@dataclass(frozen=True, slots=True)
class Summary:
    """Every §15 figure, computed once so the text and the tables agree."""

    label: str
    params: ScalperParams
    first_ts: datetime | None
    last_ts: datetime | None
    bars: int

    trades: int
    buys: int
    sells: int
    wins: int
    losses: int
    scratches: int
    win_rate: Decimal | None

    net_pnl: Decimal
    gross_pnl_before_costs: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    median_pnl: Decimal | None
    largest_win: Decimal | None
    largest_loss: Decimal | None

    spread_cost: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    swap_cost: Decimal

    max_drawdown: Decimal
    max_drawdown_pct: Decimal | None
    starting_equity: Decimal

    worst_mae: Decimal | None
    best_mfe: Decimal | None
    median_mae: Decimal | None
    median_mfe: Decimal | None

    #: MONEY at risk between entry and stop - the price distance times the size,
    #: not the distance alone. The two are the same number only when the
    #: position is one lot, which is why the distinction is spelled out here:
    #: under risk-based sizing a $1.50 stop on six ounces and a $9 stop on one
    #: ounce are the same $9 of risk, and it is the $9 a report should quote.
    median_risk: Decimal | None
    widest_risk: Decimal | None
    #: The price distance itself, which is what the stop order would be placed at.
    median_stop_distance: Decimal | None

    average_hold: timedelta | None
    median_hold: timedelta | None
    max_hold: timedelta | None

    longest_win_streak: int
    longest_loss_streak: int

    exits: Counter[str]
    stops_gapped: int
    limit_days: int
    blocked_by_daily_limit: int
    blocked_by_news: int
    blocked_while_in_position: int
    signals: int

    by_side: tuple[Bucket, ...]
    by_hour: tuple[Bucket, ...]
    by_session: tuple[Bucket, ...]
    by_month: tuple[Bucket, ...]
    by_year: tuple[Bucket, ...]

    @property
    def exit_share(self) -> dict[str, Decimal]:
        if not self.trades:
            return {}
        return {
            name: Decimal(count) / Decimal(self.trades) * Decimal("100")
            for name, count in self.exits.items()
        }


def summarise(
    result: ScalperResult,
    params: ScalperParams,
    *,
    label: str,
    starting_equity: Decimal = Decimal("1000"),
) -> Summary:
    trades = result.trades
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    holds = [t.holding_time for t in trades]

    peak = starting_equity
    equity = starting_equity
    depth = Decimal("0")
    depth_pct = Decimal("0")
    for net in nets:
        equity += net
        peak = max(peak, equity)
        drop = peak - equity
        if drop > depth:
            depth = drop
            depth_pct = drop / peak * Decimal("100") if peak else Decimal("0")

    win_streak = loss_streak = best_win = best_loss = 0
    for net in nets:
        if net > 0:
            win_streak, loss_streak = win_streak + 1, 0
        elif net < 0:
            loss_streak, win_streak = loss_streak + 1, 0
        else:
            win_streak = loss_streak = 0
        best_win = max(best_win, win_streak)
        best_loss = max(best_loss, loss_streak)

    return Summary(
        label=label,
        params=params,
        first_ts=result.first_ts,
        last_ts=result.last_ts,
        bars=result.bars_seen,
        trades=len(trades),
        buys=sum(1 for t in trades if t.side is Side.BUY),
        sells=sum(1 for t in trades if t.side is Side.SELL),
        wins=len(wins),
        losses=len(losses),
        scratches=len(nets) - len(wins) - len(losses),
        win_rate=(
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal("100") if trades else None
        ),
        net_pnl=sum(nets, Decimal("0")),
        gross_pnl_before_costs=result.gross_pnl,
        gross_profit=sum(wins, Decimal("0")),
        gross_loss=-sum(losses, Decimal("0")),
        profit_factor=(
            sum(wins, Decimal("0")) / -sum(losses, Decimal("0")) if losses else None
        ),
        expectancy=(sum(nets, Decimal("0")) / Decimal(len(trades)) if trades else None),
        average_win=(sum(wins, Decimal("0")) / Decimal(len(wins)) if wins else None),
        average_loss=(sum(losses, Decimal("0")) / Decimal(len(losses)) if losses else None),
        median_pnl=_median(nets),
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        spread_cost=sum((t.spread_paid for t in trades), Decimal("0")),
        slippage_cost=sum((t.slippage_paid for t in trades), Decimal("0")),
        commission_cost=sum((t.commission_paid for t in trades), Decimal("0")),
        swap_cost=sum((t.swap_paid for t in trades), Decimal("0")),
        max_drawdown=depth,
        max_drawdown_pct=depth_pct if trades else None,
        starting_equity=starting_equity,
        worst_mae=min((t.mae for t in trades), default=None),
        best_mfe=max((t.mfe for t in trades), default=None),
        median_mae=_median([t.mae for t in trades]),
        median_mfe=_median([t.mfe for t in trades]),
        median_risk=_median([t.stop_distance * t.lots for t in trades]),
        widest_risk=max((t.stop_distance * t.lots for t in trades), default=None),
        median_stop_distance=_median([t.stop_distance for t in trades]),
        average_hold=(sum(holds, timedelta()) / len(holds) if holds else None),
        median_hold=(_median_timedelta(holds) if holds else None),
        max_hold=(max(holds) if holds else None),
        longest_win_streak=best_win,
        longest_loss_streak=best_loss,
        exits=Counter(t.exit_reason.value for t in trades if t.exit_reason),
        stops_gapped=sum(1 for t in trades if t.stopped_on_gap),
        limit_days=len(result.limit_days),
        blocked_by_daily_limit=result.blocked_by_daily_limit,
        blocked_by_news=result.blocked_by_news,
        blocked_while_in_position=result.blocked_while_in_position,
        signals=result.signals_seen,
        by_side=_bucket(trades, lambda t: t.side.value),
        by_hour=_bucket(trades, lambda t: f"{t.entry_ts.hour:02d}:00 UTC"),
        by_session=_bucket(trades, lambda t: session_of(t.entry_ts)),
        by_month=_bucket(trades, lambda t: f"{t.entry_ts:%Y-%m}"),
        by_year=_bucket(trades, lambda t: f"{t.entry_ts:%Y}"),
    )


def _bucket(trades: Sequence[ScalperTrade], key) -> tuple[Bucket, ...]:
    grouped: dict[str, list[ScalperTrade]] = defaultdict(list)
    for trade in trades:
        grouped[key(trade)].append(trade)
    return tuple(
        Bucket(
            label=label,
            trades=len(members),
            wins=sum(1 for t in members if t.net_pnl > 0),
            net=sum((t.net_pnl for t in members), Decimal("0")),
        )
        for label, members in sorted(grouped.items())
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _median_timedelta(values: Sequence[timedelta]) -> timedelta:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ------------------------------------------------- into the engine's own record types


def to_trades(result: ScalperResult, params: ScalperParams) -> list[Trade]:
    """`ScalperTrade` -> `core.trade.Trade`, so the existing exporter and
    tearsheet render this run without knowing anything about it.

    The leg carries the **executed** prices, so `Trade.gross_pnl` is the
    executed round trip and `Charges` holds commission and swap. That makes
    `Trade.net_pnl` identical to `ScalperTrade.net_pnl` - checked in the tests -
    while the mid-basis gross stays available on the runner's own record.

    `r_multiple` is the trade in units of **its own** stop, which is what
    `metrics.trade_stats` expects R to mean: "the configured stop, not the
    maximum possible loss". Per trade rather than from the parameters, because
    under an ATR-scaled stop every trade risks a different number of dollars -
    which is the point of that stop, and would make a single divisor wrong for
    all but one trade.
    """
    out: list[Trade] = []
    for index, trade in enumerate(result.trades, start=1):
        executed = Decimal("0")
        if trade.exit_price is not None:
            move = trade.exit_price - trade.entry_price
            executed = (move if trade.side is Side.BUY else -move) * trade.lots
        out.append(
            Trade(
                trade_id=f"xau-{index:05d}",
                strategy_id="rsi_stoch_reversal",
                signal_id=f"sig-{trade.signal_ts:%Y%m%dT%H%M}",
                legs=(
                    TradeLeg(
                        instrument=XAUUSD,
                        side=trade.side,
                        lots=trade.lots,
                        entry_price=trade.entry_price,
                        exit_price=trade.exit_price,
                        entry_ts=trade.entry_ts,
                        exit_ts=trade.exit_ts,
                    ),
                ),
                opened_at=trade.entry_ts,
                closed_at=trade.exit_ts,
                gross_pnl=executed,
                charges=Charges(brokerage=trade.commission_paid, swap=trade.swap_paid),
                r_multiple=(
                    trade.net_pnl / (trade.stop_distance * trade.lots)
                    if trade.stop_distance
                    else None
                ),
                exit_reason=trade.exit_reason.value if trade.exit_reason else "",
                reason=_reason(trade),
                context=_context(trade),
            )
        )
    return out


def _reason(trade: ScalperTrade) -> str:
    direction = "bullish" if trade.side is Side.BUY else "bearish"
    return (
        f"{direction} 1H trend; 5M RSI {trade.m5_rsi_previous:.1f} -> {trade.m5_rsi:.1f}, "
        f"stoch K {trade.stoch_k:.1f} D {trade.stoch_d:.1f}"
    )


def _context(trade: ScalperTrade) -> dict[str, str]:
    """Everything §16 asks a trade log to carry, as strings.

    Strings for `export.py`'s reason: a golden trade log that depends on a float
    repr is not a golden trade log.
    """
    return {
        "signal_ts": trade.signal_ts.isoformat(),
        "entry_mid": str(trade.entry_mid),
        "exit_mid": str(trade.exit_mid) if trade.exit_mid is not None else "",
        "lots_mt5": str(Decimal(trade.lots) / Decimal("100")),
        "gross_pnl_mid": str(trade.gross_pnl),
        "spread_paid": str(trade.spread_paid),
        "slippage_paid": str(trade.slippage_paid),
        "commission_paid": str(trade.commission_paid),
        "swap_paid": str(trade.swap_paid),
        "net_pnl": str(trade.net_pnl),
        "holding": str(trade.holding_time),
        "bars_held": str(trade.bars_held),
        "mae": str(trade.mae),
        "mfe": str(trade.mfe),
        "h1_ema20": f"{trade.h1_ema_fast:.4f}",
        "h1_ema50": f"{trade.h1_ema_slow:.4f}",
        "h1_macd": f"{trade.h1_macd:.6f}",
        "h1_macd_signal": f"{trade.h1_macd_signal:.6f}",
        "h1_macd_hist": f"{trade.h1_macd_hist:.6f}",
        "m5_rsi_previous": f"{trade.m5_rsi_previous:.4f}",
        "m5_rsi": f"{trade.m5_rsi:.4f}",
        "stoch_k": f"{trade.stoch_k:.4f}",
        "stoch_d": f"{trade.stoch_d:.4f}",
        "rsi_extreme_activated": str(trade.rsi_extreme_activated).lower(),
        "daily_realised_before": str(trade.daily_realised_before),
        "news_status": trade.news_status,
    }


#: The order §16's fields appear in on the exported log. Spelled out rather
#: than taken from a sample record's key order, so adding a field to `_context`
#: without deciding where it belongs is a test failure, not a silent reshuffle.
CONTEXT_COLUMNS: tuple[str, ...] = (
    "signal_ts",
    "entry_mid",
    "exit_mid",
    "lots_mt5",
    "gross_pnl_mid",
    "spread_paid",
    "slippage_paid",
    "commission_paid",
    "swap_paid",
    "net_pnl",
    "holding",
    "bars_held",
    "mae",
    "mfe",
    "h1_ema20",
    "h1_ema50",
    "h1_macd",
    "h1_macd_signal",
    "h1_macd_hist",
    "m5_rsi_previous",
    "m5_rsi",
    "stoch_k",
    "stoch_d",
    "rsi_extreme_activated",
    "daily_realised_before",
    "news_status",
)


def equity_points(
    result: ScalperResult, *, starting_equity: Decimal = Decimal("1000")
) -> list[EquityPoint]:
    """The realised-equity curve as the engine's own point type.

    Realised only, matching `run_cfd_backtest`: the curve steps at each close
    rather than floating with an open position. With a hard stop and a
    four-hour cap the unrealised excursion between steps is bounded by the stop,
    so the drawdown this understates is bounded by roughly one stop - stated
    rather than left for a reader to wonder about.
    """
    realised = Decimal("0")
    by_ts = {t.exit_ts: t for t in result.trades if t.exit_ts is not None}
    points: list[EquityPoint] = []
    for ts, _equity, open_positions in result.equity_curve:
        closed = by_ts.get(ts)
        if closed is not None:
            realised += closed.net_pnl
        points.append(
            EquityPoint(
                ts=ts,
                cash=starting_equity + realised,
                market_value=Decimal("0"),
                equity=starting_equity + realised,
                realised_pnl=realised,
                unrealised_pnl=Decimal("0"),
                charges=Decimal("0"),
                open_positions=open_positions,
            )
        )
    return points


def downsample(points: Sequence[EquityPoint], limit: int = 2000) -> list[EquityPoint]:
    """Thin an equity curve for the tearsheet's SVG.

    Ninety-five thousand points would be a two-megabyte path element that no
    browser draws usefully. Every metric is computed from the **full** curve;
    only the picture is thinned, and the trough of a drawdown can fall between
    two kept points, so the chart is indicative and the numbers above it are not.
    """
    if len(points) <= limit:
        return list(points)
    stride = len(points) // limit + 1
    thinned = list(points[::stride])
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


# ------------------------------------------------------------------------ rendering


def _costs(summary: Summary) -> Decimal:
    return (
        summary.spread_cost
        + summary.slippage_cost
        + summary.commission_cost
        + summary.swap_cost
    )


def render_text(summary: Summary) -> str:
    """The §15 report as plain text."""
    s = summary
    lines = [
        f"{s.label}",
        f"  window            {_stamp(s.first_ts)} .. {_stamp(s.last_ts)}  ({s.bars:,} M5 bars)",
        f"  rules             {s.params.label()}",
        "",
        "  TRADES",
        f"    total           {s.trades}   ({s.buys} BUY / {s.sells} SELL)",
        f"    setups seen     {s.signals}   "
        f"({s.blocked_while_in_position} while already in a position)",
        f"    winners         {s.wins}",
        f"    losers          {s.losses}"
        + (f"   (+{s.scratches} scratch)" if s.scratches else ""),
        f"    win rate        {_pct(s.win_rate)}",
        "",
        "  MONEY  (0.01 lot = 1 ounce; $1 of gold is $1)",
        f"    net P&L         {_money(s.net_pnl)}",
        f"    gross P&L       {_money(s.gross_pnl_before_costs)}   (mid to mid, before costs)",
        f"    gross profit    {_amount(s.gross_profit)}   (sum of winners, net of costs)",
        f"    gross loss      {_amount(s.gross_loss)}   (sum of losers, net of costs)",
        f"    profit factor   {_num(s.profit_factor)}",
        f"    expectancy      {_money(s.expectancy)} per trade",
        f"    average win     {_money(s.average_win)}",
        f"    average loss    {_money(s.average_loss)}",
        f"    median trade    {_money(s.median_pnl)}",
        f"    largest win     {_money(s.largest_win)}",
        f"    largest loss    {_money(s.largest_loss)}",
        "",
        "  COSTS",
        f"    spread          {_amount(s.spread_cost)}",
        f"    slippage        {_amount(s.slippage_cost)}",
        f"    commission      {_amount(s.commission_cost)}",
        f"    swap            {_amount(s.swap_cost)}",
        f"    total           {_amount(_costs(s))}",
        "",
        "  RISK",
        f"    max drawdown    {_amount(s.max_drawdown)}  "
        f"({_pct(s.max_drawdown_pct)} of a {_amount(s.starting_equity)} account)",
        f"    worst MAE       {_money(s.worst_mae)}      median {_money(s.median_mae)}",
        f"    best MFE        {_money(s.best_mfe)}      median {_money(s.median_mfe)}",
        f"    risk at stop    {_amount(s.median_risk)} median, "
        f"{_amount(s.widest_risk)} widest   (money, = distance x size)",
        f"    stop distance   {_amount(s.median_stop_distance)} median   (price)",
        f"    win streak      {s.longest_win_streak}",
        f"    loss streak     {s.longest_loss_streak}",
        "",
        "  HOLDING",
        f"    average         {_hold(s.average_hold)}",
        f"    median          {_hold(s.median_hold)}",
        f"    maximum         {_hold(s.max_hold)}",
    ]
    if s.max_hold is not None and s.max_hold > s.params.max_hold:
        lines.append(
            f"    ...longer than the {_hold(s.params.max_hold)} cap because the market was "
            "shut: the four-hour"
        )
        lines.append(
            "       exit fires at the next executable price, and over a weekend that is Sunday."
        )
    lines.extend([
        "",
        "  EXITS",
    ])
    share = s.exit_share
    for name in ("stop loss", "rsi reversal", "max hold", "end of data"):
        count = s.exits.get(name, 0)
        if count or name != "end of data":
            lines.append(f"    {name:<16}{count:>5}   {_pct(share.get(name))}")
    if s.stops_gapped:
        lines.append(
            f"    ...of which {s.stops_gapped} opened past the stop and filled at the bar's"
            " open, worse than the level"
        )
    lines.extend(
        [
            "",
            "  GATES",
            f"    blocked by news         {s.blocked_by_news}",
            f"    blocked by daily limit  {s.blocked_by_daily_limit}",
            f"    days hitting -$50       {s.limit_days}",
        ]
    )
    return "\n".join(lines)


def render_buckets(title: str, buckets: Sequence[Bucket]) -> str:
    lines = [f"  {title}", f"    {'':<20}{'trades':>7}{'wins':>7}{'win%':>8}{'net':>12}{'exp':>10}"]
    for bucket in buckets:
        lines.append(
            f"    {bucket.label:<20}{bucket.trades:>7}{bucket.wins:>7}"
            f"{_pct(bucket.win_rate):>8}{_money(bucket.net):>12}{_money(bucket.expectancy):>10}"
        )
    return "\n".join(lines)


def _stamp(ts: datetime | None) -> str:
    return f"{ts:%Y-%m-%d %H:%M}" if ts else "-"


def _money(value: Decimal | None) -> str:
    """A signed quantity - a P&L, an expectancy, an excursion."""
    if value is None:
        return "n/a"
    return f"{value:+,.2f}"


def _amount(value: Decimal | None) -> str:
    """A magnitude - a cost, a gross total, a drawdown depth. Forcing a `+` on
    "gross loss" or "spread paid" reads as though the account gained them."""
    if value is None:
        return "n/a"
    return f"{value:,.2f}"


def _num(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _hold(value: timedelta | None) -> str:
    if value is None:
        return "n/a"
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def month_key(value: date) -> str:
    return f"{value:%Y-%m}"
