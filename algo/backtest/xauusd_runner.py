"""Bar-by-bar runner for the M5/H1 XAUUSD scalper, with the venue's real costs.

Third bespoke runner in this codebase, and for the same reason as the first two.
`bhavcopy_runner` exists because end-of-day option rows are a different data
shape; `cfd_runner` exists because `BacktestEngine` rebuilds an O(n) window per
bar and a CFD study runs fifty thousand of them. This one exists because
`cfd_runner` decides on one timeframe and manages its exit with a percentage
stop and a trail, and this strategy needs three things it has no way to express:

- a **second timeframe** - the trend gate reads the last completed H1 candle;
- **entry at the next candle's open**, not at the signal candle's close;
- **per-position state** (the RSI extreme activation), a **money** stop, a
  four-hour clock, a news gate and a daily realised-loss gate.

Bending the percentage runner into that shape would have meant lying to it about
what the stop was, which is the objection `measure_asia_value_area_xauusd` states
in the same situation. Everything that *is* shared is imported and not
reimplemented: `Bar`, `CfdCosts` and its swap and commission models,
`session_date_for` for the trading-day boundary, the measured `SpreadProfile`,
`EconomicCalendar`, and `core.trade.Trade` on the way out so `reporting.metrics`,
`reporting.export` and `reporting.tearsheet` all work unchanged.

## The price convention, stated once

Bars are **mid** (`algo.data.dukascopy`). A fill crosses half the measured spread
for its own instant:

    BUY  enters at  mid + half,  exits at  mid - half
    SELL enters at  mid - half,  exits at  mid + half

So a trade's P&L is reported two ways that agree by construction:

    gross_pnl  = (mid out - mid in) * lots        # what zero costs would pay
    spread_paid = (half_in + half_out) * lots     # what crossing cost
    net_pnl    = gross_pnl - spread_paid - commission - swap

and `net_pnl` is exactly the executed-price round trip less commission and swap.
Nothing is charged twice and nothing is netted away silently.

## The $10 stop is a price, not a P&L threshold

It is placed at the executable exit price `entry_exec -/+ $10` and triggers when
the bar's own executable price reaches it. That is what a broker's stop order
does. A bar that **gaps** through the level fills at the bar's open, never at the
level - filling the one order that exists to bound a loss at a price the market
skipped is exactly the flattery `price_stop.py` was written to refuse.

Because a stop can fire at any instant inside a bar while the RSI and four-hour
exits are decided at its close, the stop is checked first on every bar. Where a
bar contains both, the stop wins - the pessimistic reading, and the same one
`run_cfd_backtest` makes about ordering inside a bar it cannot see into.

## Look-ahead, concretely

Indicators are precomputed over the whole series, which is safe only because
every one of them is causal: `ema`, `rsi` and `stoch_rsi` at index `i` read
`values[:i+1]` and nothing else. The H1 index for an M5 bar is the last H1 bar
whose close timestamp is at or before the M5 bar's own - the *completed* candle,
never the forming one. A signal on bar `i` fills at bar `i+1`'s open. There is
no path by which bar `i+1` reaches a decision made on bar `i`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from algo.backtest.cfd_runner import CfdCosts, session_date_for
from algo.core.bar import Bar
from algo.core.enums import Side
from algo.core.errors import DataError
from algo.core.instrument import CfdId
from algo.costs.slippage import NoSlippage, SlippageModel
from algo.data.econ_calendar import EconomicCalendar
from algo.exchange.forex_calendar import ForexCalendar
from algo.pricing.indicators import ema, macd, rsi, stoch_rsi
from algo.strategy.rsi_stoch_reversal import (
    BASELINE,
    ExitReason,
    ExitState,
    ScalperParams,
    Trend,
    entry_side,
    rsi_reversal_exit,
    stop_level,
    trend_of,
)

XAUUSD = CfdId(symbol="XAUUSD")

#: H1 bars to see before the trend filter is trusted. The EMAs here are seeded
#: on the first value (`indicators.ema`, matching MT5 and TradingView), so their
#: error decays rather than vanishing; 300 hours puts the residue on a 50-period
#: EMA far below a tick. `indicators.warmup_bars` gives 37 for MACD alone, which
#: is the shorter of the two requirements and not the binding one.
H1_WARMUP = 300

#: M5 bars before the RSI and stochastic RSI are trusted. RSI(14) needs 14
#: changes, the stochastic needs 14 RSI values on top and the two smoothings
#: another 5 - about 33. 100 is comfortably past it.
M5_WARMUP = 100


@dataclass(slots=True)
class ScalperTrade:
    """One round trip, with everything §16 asks a trade log to record."""

    side: Side
    lots: int
    signal_ts: datetime
    entry_ts: datetime
    entry_price: Decimal
    entry_mid: Decimal
    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    exit_mid: Decimal | None = None
    exit_reason: ExitReason | None = None

    spread_paid: Decimal = Decimal("0")
    slippage_paid: Decimal = Decimal("0")
    #: The entry leg's share of `slippage_paid`, kept so the exit leg's share is
    #: recoverable when backing the fill out to a mid price.
    entry_slippage: Decimal = Decimal("0")
    commission_paid: Decimal = Decimal("0")
    swap_paid: Decimal = Decimal("0")

    #: Worst and best mark-to-market the open position ever showed, in dollars,
    #: on executable prices - what the trade actually felt, not a mid-price
    #: idealisation.
    mae: Decimal = Decimal("0")
    mfe: Decimal = Decimal("0")

    #: The indicator readings on the confirmation candle, for the log.
    h1_ema_fast: float = float("nan")
    h1_ema_slow: float = float("nan")
    h1_macd: float = float("nan")
    h1_macd_signal: float = float("nan")
    h1_macd_hist: float = float("nan")
    m5_rsi: float = float("nan")
    m5_rsi_previous: float = float("nan")
    stoch_k: float = float("nan")
    stoch_d: float = float("nan")

    daily_realised_before: Decimal = Decimal("0")
    news_status: str = "clear"
    rsi_extreme_activated: bool = False
    bars_held: int = 0
    #: The bar opened past the stop, so the fill was its open rather than the
    #: level. Recorded here rather than inferred from the P&L afterwards: with
    #: slippage charged on every stop, "filled worse than the level" is true of
    #: all of them and says nothing about which ones gapped.
    stopped_on_gap: bool = False

    @property
    def gross_pnl(self) -> Decimal:
        """Mid-to-mid, before any cost. `0` while the trade is still open."""
        if self.exit_mid is None:
            return Decimal("0")
        move = self.exit_mid - self.entry_mid
        signed = move if self.side is Side.BUY else -move
        return signed * self.lots

    @property
    def costs(self) -> Decimal:
        return self.spread_paid + self.slippage_paid + self.commission_paid + self.swap_paid

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.costs

    @property
    def holding_time(self) -> timedelta:
        if self.exit_ts is None:
            return timedelta(0)
        return self.exit_ts - self.entry_ts


@dataclass(slots=True)
class ScalperResult:
    """What one run produced, including what it refused to do and why."""

    trades: list[ScalperTrade] = field(default_factory=list)
    #: (bar timestamp, realised equity, 1 if a position was open across it)
    equity_curve: list[tuple[datetime, Decimal, int]] = field(default_factory=list)
    bars_seen: int = 0
    signals_seen: int = 0
    blocked_by_news: int = 0
    blocked_by_daily_limit: int = 0
    blocked_while_in_position: int = 0
    #: Trading days on which realised P&L reached the limit, and the day's total.
    limit_days: dict[date, Decimal] = field(default_factory=dict)
    daily_realised: dict[date, Decimal] = field(default_factory=dict)
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def net_pnl(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal("0"))

    @property
    def gross_pnl(self) -> Decimal:
        return sum((t.gross_pnl for t in self.trades), Decimal("0"))

    @property
    def total_costs(self) -> Decimal:
        return sum((t.costs for t in self.trades), Decimal("0"))


@dataclass(frozen=True, slots=True)
class Indicators:
    """Precomputed causal series, and the M5 -> H1 index map."""

    h1_trend: list[Trend]
    h1_ema_fast: list[float]
    h1_ema_slow: list[float]
    h1_macd: list[float]
    h1_signal: list[float]
    h1_hist: list[float]
    m5_rsi: list[float]
    stoch_k: list[float]
    stoch_d: list[float]
    #: For M5 bar i, the index of the last **completed** H1 bar, or -1.
    h1_index: list[int]


def compute_indicators(
    m5: Sequence[Bar], h1: Sequence[Bar], params: ScalperParams = BASELINE
) -> Indicators:
    """Every series the rules need, computed once over the whole history.

    Safe against look-ahead only because each of these is causal - see the
    module docstring. The H1 map is built with a forward pointer rather than a
    search per bar, which keeps a 100,000-bar run linear.
    """
    h1_close = [float(b.close) for b in h1]
    fast = ema(h1_close, params.ema_fast)
    slow = ema(h1_close, params.ema_slow)
    lines = macd(
        h1_close, fast=params.macd_fast, slow=params.macd_slow, signal=params.macd_signal
    )
    trends = [
        trend_of(
            ema_fast=fast[i],
            ema_slow=slow[i],
            macd_line=lines.macd[i],
            macd_signal=lines.signal[i],
        )
        for i in range(len(h1))
    ]

    m5_close = [float(b.close) for b in m5]
    strength = rsi(m5_close, params.rsi_period)
    stoch = stoch_rsi(
        m5_close,
        rsi_period=params.rsi_period,
        stoch_period=params.stoch_rsi_period,
        smooth_k=params.stoch_smooth_k,
        smooth_d=params.stoch_smooth_d,
    )

    mapping: list[int] = []
    pointer = -1
    for bar in m5:
        while pointer + 1 < len(h1) and h1[pointer + 1].ts <= bar.ts:
            pointer += 1
        mapping.append(pointer)

    return Indicators(
        h1_trend=trends,
        h1_ema_fast=fast,
        h1_ema_slow=slow,
        h1_macd=lines.macd,
        h1_signal=lines.signal,
        h1_hist=lines.histogram,
        m5_rsi=strength,
        stoch_k=stoch.k,
        stoch_d=stoch.d,
        h1_index=mapping,
    )


def run_scalper(
    m5: Sequence[Bar],
    h1: Sequence[Bar],
    *,
    params: ScalperParams = BASELINE,
    costs: CfdCosts | None = None,
    calendar: EconomicCalendar | None = None,
    starting_equity: Decimal = Decimal("10000"),
    indicators: Indicators | None = None,
    slippage: SlippageModel | None = None,
    tick: Decimal = Decimal("0.001"),
) -> ScalperResult:
    """Walk `m5` once, trading the baseline rules and charging real costs.

    `indicators` can be passed in when several variants share one parameter set
    for the indicators themselves - recomputing RSI over 100,000 bars for each
    of a dozen stop-loss variants is pure waste, and the series are identical
    by construction. Pass `None` and they are computed here.
    """
    if not m5 or not h1:
        raise DataError("the scalper needs both M5 and H1 bars")
    if params.news_filter and calendar is None:
        raise DataError(
            "news_filter is on but no economic calendar was supplied. Running "
            "without one would report zero blocked entries as though the filter "
            "had been applied and found nothing."
        )

    charged = costs or CfdCosts()
    # Slippage is charged *on top of* the spread and reported apart from it, for
    # the reason `algo/costs/slippage.py` states: a run that lumps them cannot
    # say which of the two is eating the edge. A stop slips more than a market
    # order because it is a market order fired into a move already going against
    # the position.
    slip = slippage or NoSlippage()
    sessions = ForexCalendar()
    ind = indicators or compute_indicators(m5, h1, params)
    result = ScalperResult(first_ts=m5[0].ts, last_ts=m5[-1].ts)

    def half_at(ts: datetime) -> Decimal:
        if charged.half_spread_at is not None:
            return charged.half_spread_at(ts)
        return charged.half_spread

    open_trade: ScalperTrade | None = None
    state: ExitState | None = None
    pending: tuple[Side, int] | None = None  # (side, index of the signal bar)
    last_session: date | None = None
    realised = Decimal("0")

    for i, bar in enumerate(m5):
        result.bars_seen += 1
        half = half_at(bar.ts)

        # ---------------------------------------------------- fill a pending entry
        if pending is not None:
            side, signal_index = pending
            pending = None
            entry_mid = bar.open
            entry_slip = slip.extra(tick=tick, is_stop=False)
            entry_exec = (
                entry_mid + half + entry_slip
                if side is Side.BUY
                else entry_mid - half - entry_slip
            )
            signal_bar = m5[signal_index]
            h1_index = ind.h1_index[signal_index]
            entry_day = session_date_for(sessions, signal_bar.ts)
            open_trade = ScalperTrade(
                side=side,
                lots=params.lots,
                signal_ts=signal_bar.ts,
                # The signal candle's close and the next candle's open are the
                # same instant on a continuous 5-minute grid. Both are recorded
                # because they answer different questions in a trade log, and
                # their being equal is the proof the entry was not intrabar.
                entry_ts=signal_bar.ts,
                entry_price=entry_exec,
                entry_mid=entry_mid,
                spread_paid=half * params.lots,
                slippage_paid=entry_slip * params.lots,
                entry_slippage=entry_slip * params.lots,
                commission_paid=_commission(charged, side, params.lots, entry_exec, entry_day),
                h1_ema_fast=ind.h1_ema_fast[h1_index],
                h1_ema_slow=ind.h1_ema_slow[h1_index],
                h1_macd=ind.h1_macd[h1_index],
                h1_macd_signal=ind.h1_signal[h1_index],
                h1_macd_hist=ind.h1_hist[h1_index],
                m5_rsi=ind.m5_rsi[signal_index],
                m5_rsi_previous=ind.m5_rsi[signal_index - 1],
                stoch_k=ind.stoch_k[signal_index],
                stoch_d=ind.stoch_d[signal_index],
                daily_realised_before=result.daily_realised.get(entry_day, Decimal("0")),
                # Recorded whether or not the filter is on, so a run with it off
                # can still be asked how many of its trades were news trades.
                news_status=_news_status(calendar, signal_bar.ts, params),
            )
            state = ExitState(side=side)
            last_session = session_date_for(sessions, bar.ts)

        held = open_trade is not None

        # ------------------------------------------------------------ financing
        if open_trade is not None:
            session = session_date_for(sessions, bar.ts)
            if last_session is not None and session != last_session:
                open_trade.swap_paid += -charged.swap.carry_for(
                    side=open_trade.side, lots=open_trade.lots, on=session
                )
            last_session = session

        # ------------------------------------------------- exits, stop checked first
        if open_trade is not None and state is not None:
            trade = open_trade
            trade.bars_held += 1
            _mark(trade, bar, half)

            level = stop_level(trade.side, trade.entry_price, params)
            open_exec = _exit_exec(trade.side, bar.open, half)
            worst_exec = _exit_exec(
                trade.side, bar.low if trade.side is Side.BUY else bar.high, half
            )
            stopped = (
                worst_exec <= level if trade.side is Side.BUY else worst_exec >= level
            )
            gapped = open_exec <= level if trade.side is Side.BUY else open_exec >= level

            if stopped:
                # The stop level is where it triggers; the slip is what the
                # market takes on the way out. A gapped bar fills at its open
                # and slips from there - never at a level the market skipped.
                stop_slip = slip.extra(tick=tick, is_stop=True)
                base = open_exec if gapped else level
                trade.stopped_on_gap = gapped
                fill = base - stop_slip if trade.side is Side.BUY else base + stop_slip
                trade.slippage_paid += stop_slip * trade.lots
                _close(
                    result,
                    trade,
                    bar.ts,
                    fill,
                    half,
                    ExitReason.STOP_LOSS,
                    charged,
                    sessions,
                    params,
                )
                realised += trade.net_pnl
                open_trade, state = None, None
            else:
                previous_rsi = ind.m5_rsi[i - 1] if i else float("nan")
                reversal = rsi_reversal_exit(
                    state, previous_rsi=previous_rsi, rsi=ind.m5_rsi[i], params=params
                )
                state.observe(ind.m5_rsi[i], params)
                trade.rsi_extreme_activated = state.extreme_activated
                expired = bar.ts - trade.entry_ts >= params.max_hold
                last_bar = i == len(m5) - 1

                if reversal or expired or last_bar:
                    reason = (
                        ExitReason.RSI_REVERSAL
                        if reversal
                        else ExitReason.MAX_HOLD
                        if expired
                        else ExitReason.END_OF_DATA
                    )
                    exit_slip = slip.extra(tick=tick, is_stop=False)
                    fill = _exit_exec(trade.side, bar.close, half + exit_slip)
                    trade.slippage_paid += exit_slip * trade.lots
                    _close(
                        result,
                        trade,
                        bar.ts,
                        fill,
                        half,
                        reason,
                        charged,
                        sessions,
                        params,
                    )
                    realised += trade.net_pnl
                    open_trade, state = None, None

        # ----------------------------------------------------------- entry search
        if open_trade is None and pending is None and i + 1 < len(m5):
            h1_index = ind.h1_index[i]
            if i >= M5_WARMUP and h1_index >= H1_WARMUP:
                side = entry_side(
                    trend=ind.h1_trend[h1_index],
                    previous_rsi=ind.m5_rsi[i - 1],
                    rsi=ind.m5_rsi[i],
                    stoch_k=ind.stoch_k[i],
                    stoch_d=ind.stoch_d[i],
                    params=params,
                )
                if side is not None:
                    result.signals_seen += 1
                    day = session_date_for(sessions, bar.ts)
                    event = (
                        calendar.blocking_event(
                            bar.ts, before=params.news_before, after=params.news_after
                        )
                        if params.news_filter and calendar is not None
                        else None
                    )
                    if event is not None:
                        result.blocked_by_news += 1
                    elif day in result.limit_days:
                        # A latch, not a running comparison. "Block all new
                        # entries for the rest of that trading day" stays true
                        # even if the day's realised P&L later climbs back above
                        # the limit as an already-open position closes.
                        result.blocked_by_daily_limit += 1
                    else:
                        pending = (side, i)
        elif open_trade is not None:
            # A setup that appeared while already in a position. Counted rather
            # than dropped: "the strategy took 300 trades" and "the strategy saw
            # 900 setups and could act on 300" are different claims about it.
            h1_index = ind.h1_index[i]
            if i >= M5_WARMUP and h1_index >= H1_WARMUP:
                blocked = entry_side(
                    trend=ind.h1_trend[h1_index],
                    previous_rsi=ind.m5_rsi[i - 1],
                    rsi=ind.m5_rsi[i],
                    stoch_k=ind.stoch_k[i],
                    stoch_d=ind.stoch_d[i],
                    params=params,
                )
                if blocked is not None:
                    result.signals_seen += 1
                    result.blocked_while_in_position += 1

        result.equity_curve.append((bar.ts, starting_equity + realised, 1 if held else 0))

    return result


def _commission(
    charged: CfdCosts, side: Side, lots: int, price: Decimal, on: date
) -> Decimal:
    return charged.commission.charges_for(
        side=side, lots=lots, price=price, multiplier=Decimal("1"), is_option=False, on=on
    ).total


def _exit_exec(side: Side, mid: Decimal, half: Decimal) -> Decimal:
    """The executable price to close `side` at, given a mid quote."""
    return mid - half if side is Side.BUY else mid + half


def _mark(trade: ScalperTrade, bar: Bar, half: Decimal) -> None:
    """Update MAE and MFE from this bar's range, on executable prices.

    Both extremes of the bar are used, which means one of them is credited even
    though the order within the bar is unknowable. That is correct for
    excursion statistics - MAE asks how far the trade went against you at any
    point, not in what order - and it is not used to decide anything.
    """
    low_exec = _exit_exec(trade.side, bar.low, half)
    high_exec = _exit_exec(trade.side, bar.high, half)
    if trade.side is Side.BUY:
        worst = (low_exec - trade.entry_price) * trade.lots
        best = (high_exec - trade.entry_price) * trade.lots
    else:
        worst = (trade.entry_price - high_exec) * trade.lots
        best = (trade.entry_price - low_exec) * trade.lots
    trade.mae = min(trade.mae, worst)
    trade.mfe = max(trade.mfe, best)


def _close(
    result: ScalperResult,
    trade: ScalperTrade,
    ts: datetime,
    fill: Decimal,
    half: Decimal,
    reason: ExitReason,
    charged: CfdCosts,
    sessions: ForexCalendar,
    params: ScalperParams,
) -> None:
    """Book the exit, and roll the day's realised P&L forward."""
    day = session_date_for(sessions, ts)
    trade.exit_ts = ts
    trade.exit_price = fill
    # The mid the fill implies, so `gross_pnl` stays a mid-to-mid measure even
    # when the fill was a stop level rather than a bar price.
    # `gross_pnl` stays a mid-to-mid measure even when the fill was a stop
    # level rather than a bar price: back out the half spread and whatever
    # slippage this exit paid, both of which are charged as costs in their own
    # right and must not also be baked into the gross figure.
    exit_slippage = trade.slippage_paid - trade.entry_slippage
    back_out = half + exit_slippage / trade.lots
    trade.exit_mid = fill + back_out if trade.side is Side.BUY else fill - back_out
    trade.exit_reason = reason
    trade.spread_paid += half * trade.lots
    trade.commission_paid += _commission(charged, _flip(trade.side), trade.lots, fill, day)

    result.trades.append(trade)
    running = result.daily_realised.get(day, Decimal("0")) + trade.net_pnl
    result.daily_realised[day] = running
    if running <= params.daily_loss_limit and day not in result.limit_days:
        result.limit_days[day] = running


def _news_status(
    calendar: EconomicCalendar | None, ts: datetime, params: ScalperParams
) -> str:
    if calendar is None:
        return "no calendar"
    event = calendar.blocking_event(ts, before=params.news_before, after=params.news_after)
    if event is None:
        return "clear"
    # Reachable only with the filter off - with it on, this entry was blocked.
    return f"in window: {event.name} (filter off)"


def _flip(side: Side) -> Side:
    return Side.SELL if side is Side.BUY else Side.BUY
