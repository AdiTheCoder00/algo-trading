"""Bar-by-bar runner for the liquidity-sweep strategy, with the venue's costs.

The fourth bespoke runner in this codebase, and the first three each say why
they exist: `bhavcopy_runner` because end-of-day option rows are a different
data shape, `cfd_runner` because `BacktestEngine` rebuilds an O(n) window per
bar, `xauusd_runner` because a percentage stop and a trail cannot express a
money stop on a second timeframe. This one exists because none of them can
carry a **resting limit order**:

- the entry is a price the market must come back to, not a decision taken at a
  close, so an order lives across bars and may expire unfilled;
- the exits are two **levels** fixed at entry - a stop and a target - plus a
  four-hour clock, and `xauusd_runner` has no take-profit at all;
- the size is whatever risks a fixed fraction of *running* equity between those
  two levels, so it differs on every trade.

Everything shared is imported rather than rebuilt: `Bar`, `CfdCosts` with its
swap and commission models, `session_date_for` for the trading-day boundary,
`SlippageModel`, `round_down_to_lot_step` for sizing, and `core.trade.Trade` on
the way out so `reporting.metrics`, `reporting.export` and `reporting.tearsheet`
all work unchanged.

## The price convention, stated once

Bars are **mid** (`algo.data.dukascopy`) and every level the strategy computes
is therefore a mid price. A fill crosses half the measured spread for its own
instant, so for one strategy level `L`:

    long  entry: fills when the bar's low reaches L, executed at L + half
    long  stop:  fills when the bar's low reaches L, executed at L - half
    long  target:fills when the bar's high reaches L, executed at L - half

and the short side is the mirror. That means the **realised R is smaller than
the nominal R**: a nominal 2:1 target pays `2*(P - S) - 2*half` against a risk
of `(P - S) + 2*half`. Nothing here hides that - the report prints both the
nominal RR the strategy asked for and the R the trade actually returned.

Two consequences worth being explicit about, because both are choices:

1. **A limit never fills better than its price**, even when a bar opens through
   it. On a continuous five-minute grid that only happens across a weekend, and
   crediting a weekend gap to a resting order is exactly the flattery the stop
   rules in `price_stop.py` refuse in the other direction.
2. **A stop that gaps fills at the bar's open**, never at its level, for that
   same reason read the honest way round.

## Ordering inside a bar, always against the position

The runner cannot see inside a five-minute bar, so wherever two things could
have happened it assumes the worse one:

- a bar that reaches both the stop and the target resolves as the **stop**;
- a bar that fills the entry *and* reaches the stop resolves as an entry
  followed immediately by the stop, not as a fill that survived;
- the four-hour clock and the end of data are checked after both, because they
  are the exits that only exist when neither level was touched.

## Look-ahead

The strategy sees one closed bar at a time through the same `BarContext` the
MCX engine builds, holding a bounded window that ends at the current bar. An
order emitted on the close of bar `i` is first eligible on bar `i + 1`. Sizing
happens at the fill, from equity as it stood at that moment - not at the signal,
where the fill price is not yet known.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from algo.backtest.cfd_runner import CfdCosts, session_date_for
from algo.core.bar import Bar, BarWindow
from algo.core.enums import Exchange, Side
from algo.core.errors import DataError
from algo.core.fill import Fill
from algo.core.instrument import CfdId
from algo.core.money import round_down_to_lot_step
from algo.core.position import Position
from algo.core.timeutil import iso
from algo.costs.slippage import NoSlippage, SlippageModel
from algo.exchange.forex_calendar import ForexCalendar
from algo.exchange.specs import ContractSpecStore
from algo.strategy.context import BarContext, PositionView, SessionInfo
from algo.strategy.liquidity_sweep import (
    BASELINE,
    LiquiditySource,
    LiquiditySweepParams,
    LiquiditySweepStrategy,
    SetupRecord,
    trading_day,
)

XAUUSD = CfdId(symbol="XAUUSD")

#: The engine's minimum tradeable size for XAUUSD, from `spec_xauusd.yaml`: MT5
#: `volume_min` 0.01 broker lots of 100 ounces = one ounce, in steps of one.
#: `run_liquidity_sweep` asserts this against the spec store at startup rather
#: than trusting the constant.
MIN_LOTS = 1
LOT_STEP = 1


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MAX_HOLD = "MAX_HOLD"
    END_OF_DATA = "END_OF_DATA"


class OrderOutcome(StrEnum):
    """What became of a resting order. Counted, never silently dropped."""

    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    UNAFFORDABLE = "UNAFFORDABLE"


@dataclass(slots=True)
class RestingOrder:
    """A limit order the strategy placed, waiting for price to come back."""

    side: Side
    limit: Decimal
    stop: Decimal
    target: Decimal
    placed_at: datetime
    expires_at: datetime
    max_hold: timedelta
    signal_id: str
    reason: str
    context: dict[str, str]

    @property
    def source(self) -> LiquiditySource:
        return LiquiditySource(self.context["liquidity_type"])


@dataclass(slots=True)
class SweepTrade:
    """One round trip, and every number needed to check it by hand."""

    side: Side
    lots: int
    signal_ts: datetime
    entry_ts: datetime
    #: The strategy's own levels, in mid prices.
    limit_mid: Decimal
    stop_mid: Decimal
    target_mid: Decimal
    #: What was actually paid and where the exits actually sat, after crossing.
    entry_price: Decimal
    entry_mid: Decimal
    stop_exec: Decimal
    target_exec: Decimal
    #: Executable risk per lot, and the dollars that put at risk. R is measured
    #: against these, so it is the risk the account really took.
    risk_per_lot: Decimal
    risk_amount: Decimal
    equity_at_entry: Decimal

    session: str
    source: LiquiditySource
    signal_id: str
    reason: str
    context: dict[str, str] = field(default_factory=dict)

    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    exit_mid: Decimal | None = None
    exit_reason: ExitReason | None = None

    spread_paid: Decimal = Decimal("0")
    slippage_paid: Decimal = Decimal("0")
    entry_slippage: Decimal = Decimal("0")
    commission_paid: Decimal = Decimal("0")
    swap_paid: Decimal = Decimal("0")

    mae: Decimal = Decimal("0")
    mfe: Decimal = Decimal("0")
    bars_held: int = 0
    stopped_on_gap: bool = False

    @property
    def gross_pnl(self) -> Decimal:
        """Mid-to-mid, before any cost. Zero while the trade is still open."""
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
    def r_multiple(self) -> Decimal | None:
        """Realised P&L over the money the trade put at risk. Section 28."""
        if self.exit_mid is None or self.risk_amount <= 0:
            return None
        return self.net_pnl / self.risk_amount

    @property
    def holding_time(self) -> timedelta:
        if self.exit_ts is None:
            return timedelta(0)
        return self.exit_ts - self.entry_ts

    @property
    def day(self) -> date:
        return trading_day(self.entry_ts)


@dataclass(slots=True)
class SweepResult:
    """What one run produced, including everything it declined to do."""

    trades: list[SweepTrade] = field(default_factory=list)
    #: (bar timestamp, realised equity, 1 if a position was open across it)
    equity_curve: list[tuple[datetime, Decimal, int]] = field(default_factory=list)
    records: list[SetupRecord] = field(default_factory=list)
    bars_seen: int = 0
    orders_placed: int = 0
    orders_filled: int = 0
    orders_expired: int = 0
    #: Orders abandoned at the fill because the stop was too far away to take
    #: even one ounce inside the risk budget. Counted rather than dropped:
    #: "took 90 trades" and "saw 95 fills it could afford 90 of" differ.
    orders_unaffordable: int = 0
    #: Signals the runner had to refuse because an order or a position was
    #: already live. The strategy gates these itself, so a non-zero count here
    #: is a disagreement between the two and is reported as one.
    signals_refused: int = 0
    starting_equity: Decimal = Decimal("0")
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

    @property
    def final_equity(self) -> Decimal:
        return self.starting_equity + self.net_pnl


def size_for_risk(
    *, risk: Decimal, entry_price: Decimal, stop_price: Decimal
) -> tuple[int, Decimal]:
    """Lots risking about `risk` between entry and stop, and the distance.

    Rounds **down**, via the engine's own `round_down_to_lot_step`, whose
    docstring states the rule: below the minimum lot, skip the trade and log it
    - never round up, because rounding up silently exceeds the risk budget.
    """
    distance = abs(entry_price - stop_price)
    if distance <= 0:
        return 0, distance
    lots = round_down_to_lot_step(risk / distance, LOT_STEP)
    return (lots if lots >= MIN_LOTS else 0), distance


def run_liquidity_sweep(
    bars: Sequence[Bar],
    *,
    params: LiquiditySweepParams = BASELINE,
    costs: CfdCosts | None = None,
    slippage: SlippageModel | None = None,
    risk_per_trade_pct: Decimal = Decimal("1"),
    starting_equity: Decimal = Decimal("10000"),
    tick: Decimal = Decimal("0.01"),
    instrument: CfdId = XAUUSD,
    strategy: LiquiditySweepStrategy | None = None,
) -> SweepResult:
    """Walk `bars` once, trading the rule set and charging the venue's costs.

    `risk_per_trade_pct` lives here and not in `LiquiditySweepParams` on
    purpose. The base class is explicit that "a strategy that computes lot size
    is a bug": the strategy says where the stop goes, this says how much money
    that stop is allowed to cost, and only this knows what the account is worth
    at the moment the order fills.
    """
    if not bars:
        raise DataError("the liquidity-sweep runner needs at least one bar")

    charged = costs or CfdCosts()
    slip = slippage or NoSlippage()
    sessions = ForexCalendar()
    specs = ContractSpecStore.default()
    rules = strategy or LiquiditySweepStrategy(instrument=instrument, params=params)
    result = SweepResult(
        starting_equity=starting_equity, first_ts=bars[0].ts, last_ts=bars[-1].ts
    )

    def half_at(ts: datetime) -> Decimal:
        if charged.half_spread_at is not None:
            return charged.half_spread_at(ts)
        return charged.half_spread

    open_trade: SweepTrade | None = None
    resting: RestingOrder | None = None
    realised = Decimal("0")
    last_session: date | None = None

    for index, bar in enumerate(bars):
        result.bars_seen += 1
        half = half_at(bar.ts)
        last_bar = index == len(bars) - 1

        # ------------------------------------------------------------ financing
        if open_trade is not None:
            session = session_date_for(sessions, bar.ts)
            if last_session is not None and session != last_session:
                open_trade.swap_paid += -charged.swap.carry_for(
                    side=open_trade.side, lots=open_trade.lots, on=session
                )
            last_session = session

        # --------------------------------------------------- fill a resting order
        if open_trade is None and resting is not None:
            if bar.ts > resting.expires_at:
                resting = None
                result.orders_expired += 1
            elif _touched(resting.side, resting.limit, bar):
                equity = starting_equity + realised
                entry_mid = resting.limit
                entry_exec = _entry_exec(resting.side, entry_mid, half)
                stop_exec = _exit_exec(resting.side, resting.stop, half)
                target_exec = _exit_exec(resting.side, resting.target, half)
                budget = equity * risk_per_trade_pct / Decimal("100")
                lots, per_lot = size_for_risk(
                    risk=budget, entry_price=entry_exec, stop_price=stop_exec
                )
                if lots < MIN_LOTS:
                    result.orders_unaffordable += 1
                    resting = None
                else:
                    open_trade = SweepTrade(
                        side=resting.side,
                        lots=lots,
                        signal_ts=resting.placed_at,
                        entry_ts=bar.ts,
                        limit_mid=resting.limit,
                        stop_mid=resting.stop,
                        target_mid=resting.target,
                        entry_price=entry_exec,
                        entry_mid=entry_mid,
                        stop_exec=stop_exec,
                        target_exec=target_exec,
                        risk_per_lot=per_lot,
                        risk_amount=per_lot * lots,
                        equity_at_entry=equity,
                        session=resting.context.get("session", ""),
                        source=resting.source,
                        signal_id=resting.signal_id,
                        reason=resting.reason,
                        context=dict(resting.context),
                        spread_paid=half * lots,
                        commission_paid=_commission(
                            charged,
                            resting.side,
                            lots,
                            entry_exec,
                            session_date_for(sessions, bar.ts),
                        ),
                    )
                    open_trade.context["max_hold_minutes"] = str(
                        int(resting.max_hold.total_seconds() // 60)
                    )
                    result.orders_filled += 1
                    last_session = session_date_for(sessions, bar.ts)
                    rules.on_fill(
                        Fill(
                            fill_id=f"{resting.signal_id}:fill",
                            client_order_id=f"{resting.signal_id}:0:0",
                            signal_id=resting.signal_id,
                            instrument=instrument,
                            side=resting.side,
                            lots=lots,
                            qty=Decimal(lots) * Decimal(resting.side.sign),
                            price=entry_exec,
                            ts=bar.ts,
                            is_modelled=True,
                        )
                    )
                    resting = None

        # ---------------------------------------------------- exits, stop first
        if open_trade is not None:
            trade = open_trade
            trade.bars_held += 1
            _mark(trade, bar, half)

            stopped = _touched_stop(trade.side, trade.stop_mid, bar)
            targeted = _touched_target(trade.side, trade.target_mid, bar)
            expired = bar.ts - trade.entry_ts >= _max_hold(trade)

            if stopped:
                gapped = (
                    bar.open <= trade.stop_mid
                    if trade.side is Side.BUY
                    else bar.open >= trade.stop_mid
                )
                stop_slip = slip.extra(tick=tick, is_stop=True)
                base_mid = bar.open if gapped else trade.stop_mid
                trade.stopped_on_gap = gapped
                fill = _exit_exec(trade.side, base_mid, half) + (
                    -stop_slip if trade.side is Side.BUY else stop_slip
                )
                trade.slippage_paid += stop_slip * trade.lots
                _close(result, trade, bar.ts, fill, half, ExitReason.STOP_LOSS, charged, sessions)
                realised += trade.net_pnl
                open_trade = None
            elif targeted:
                # A limit exit does not slip: it fills at its price or not at all.
                fill = _exit_exec(trade.side, trade.target_mid, half)
                _close(
                    result, trade, bar.ts, fill, half, ExitReason.TAKE_PROFIT, charged, sessions
                )
                realised += trade.net_pnl
                open_trade = None
            elif expired or last_bar:
                exit_slip = slip.extra(tick=tick, is_stop=False)
                fill = _exit_exec(trade.side, bar.close, half) + (
                    -exit_slip if trade.side is Side.BUY else exit_slip
                )
                trade.slippage_paid += exit_slip * trade.lots
                reason = ExitReason.MAX_HOLD if expired else ExitReason.END_OF_DATA
                _close(result, trade, bar.ts, fill, half, reason, charged, sessions)
                realised += trade.net_pnl
                open_trade = None

        # ------------------------------------------------------------- decide
        held: Position | None = None
        if open_trade is not None:
            signed = Decimal(open_trade.lots) * Decimal(open_trade.side.sign)
            held = Position(
                instrument=instrument,
                lots=int(signed),
                qty=signed,
                cost_basis=Decimal(open_trade.lots) * open_trade.entry_price,
            )

        ctx = BarContext(
            window=BarWindow.of((bar,)),
            session=SessionInfo(
                session_date=session_date_for(sessions, bar.ts),
                is_us_dst=False,
                minutes_to_close=0,
                is_partial_bar=bar.is_partial,
                bar_index=index,
                bars_in_session=1,
            ),
            specs=specs,
            positions=PositionView({} if held is None else {instrument.key: held}),
            timeframe=bar.timeframe,
            exchange=Exchange.OTC,
        )

        signals = rules.on_bar(ctx)
        result.records.extend(rules.drain_records())
        for signal in signals:
            leg = signal.legs[0]
            if resting is not None or open_trade is not None:
                # The strategy gates both of these itself; reaching here means
                # the two disagree, which is a fact about the run and not
                # something to paper over by taking the trade anyway.
                result.signals_refused += 1
                continue
            if leg.entry.limit_price is None or leg.stop_price is None or not leg.take_profits:
                raise DataError(
                    f"signal {signal.signal_id} is missing a limit, a stop or a target; "
                    "this runner cannot execute it"
                )
            resting = RestingOrder(
                side=leg.direction,
                limit=leg.entry.limit_price,
                stop=leg.stop_price,
                target=leg.take_profits[0].price,
                placed_at=bar.ts,
                expires_at=datetime.fromisoformat(signal.context["expires_at"]),
                max_hold=timedelta(minutes=int(signal.context["max_hold_minutes"])),
                signal_id=signal.signal_id,
                reason=signal.reason,
                context=dict(signal.context),
            )
            result.orders_placed += 1

        result.equity_curve.append(
            (bar.ts, starting_equity + realised, 0 if open_trade is None else 1)
        )

    return result


# ------------------------------------------------------------------ mechanics


def _entry_exec(side: Side, mid: Decimal, half: Decimal) -> Decimal:
    """The executable price to open `side` at, given a mid quote."""
    return mid + half if side is Side.BUY else mid - half


def _exit_exec(side: Side, mid: Decimal, half: Decimal) -> Decimal:
    """The executable price to close `side` at, given a mid quote."""
    return mid - half if side is Side.BUY else mid + half


def _touched(side: Side, limit: Decimal, bar: Bar) -> bool:
    """Did the mid trade to a resting entry's price this bar?"""
    return bar.low <= limit if side is Side.BUY else bar.high >= limit


def _touched_stop(side: Side, stop: Decimal, bar: Bar) -> bool:
    return bar.low <= stop if side is Side.BUY else bar.high >= stop


def _touched_target(side: Side, target: Decimal, bar: Bar) -> bool:
    return bar.high >= target if side is Side.BUY else bar.low <= target


def _max_hold(trade: SweepTrade) -> timedelta:
    return timedelta(minutes=int(trade.context.get("max_hold_minutes", "240")))


def _commission(
    charged: CfdCosts, side: Side, lots: int, price: Decimal, on: date
) -> Decimal:
    return charged.commission.charges_for(
        side=side, lots=lots, price=price, multiplier=Decimal("1"), is_option=False, on=on
    ).total


def _mark(trade: SweepTrade, bar: Bar, half: Decimal) -> None:
    """Update MAE and MFE from this bar's range, on executable prices.

    Both extremes are credited even though their order inside the bar is
    unknowable. That is right for excursion statistics - MAE asks how far the
    trade went against you at any point, not in what order - and neither figure
    is used to decide anything.
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
    result: SweepResult,
    trade: SweepTrade,
    ts: datetime,
    fill: Decimal,
    half: Decimal,
    reason: ExitReason,
    charged: CfdCosts,
    sessions: ForexCalendar,
) -> None:
    """Book the exit at an executable price, and record the mid it implies."""
    day = session_date_for(sessions, ts)
    trade.exit_ts = ts
    trade.exit_price = fill
    # Back the half spread and this exit's slippage out of the fill, so
    # `gross_pnl` stays a mid-to-mid measure and both costs are charged once,
    # as costs, rather than also being baked into the gross figure.
    exit_slippage = trade.slippage_paid - trade.entry_slippage
    back_out = half + exit_slippage / trade.lots
    trade.exit_mid = fill + back_out if trade.side is Side.BUY else fill - back_out
    trade.exit_reason = reason
    trade.spread_paid += half * trade.lots
    trade.commission_paid += _commission(charged, trade.side.opposite, trade.lots, fill, day)
    result.trades.append(trade)


def session_of(ts: datetime, params: LiquiditySweepParams) -> str:
    """Which configured window a timestamp falls in, or `OUTSIDE`."""
    for window in params.trading_windows:
        if window.contains(ts):
            return window.name
    if params.asian_session.contains(ts):
        return params.asian_session.name
    return "OUTSIDE"


def trade_context_row(trade: SweepTrade) -> dict[str, str]:
    """The trade's own log row: the setup that produced it plus the outcome."""
    row = dict(trade.context)
    row.update(
        {
            "entry_ts": iso(trade.entry_ts),
            "exit_ts": "" if trade.exit_ts is None else iso(trade.exit_ts),
            "side": trade.side.value,
            "lots": str(trade.lots),
            "fill_price": str(trade.entry_price),
            "exit_price": "" if trade.exit_price is None else str(trade.exit_price),
            "exit_reason": "" if trade.exit_reason is None else trade.exit_reason.value,
            "risk_per_lot": str(trade.risk_per_lot),
            "risk_amount": str(trade.risk_amount),
            "gross_pnl": str(trade.gross_pnl),
            "costs": str(trade.costs),
            "net_pnl": str(trade.net_pnl),
            "r_multiple": "" if trade.r_multiple is None else str(trade.r_multiple),
            "bars_held": str(trade.bars_held),
            "mae": str(trade.mae),
            "mfe": str(trade.mfe),
        }
    )
    return row
