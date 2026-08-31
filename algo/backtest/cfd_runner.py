"""Bar-by-bar backtest for a CFD strategy, with the venue's real costs.

Extracted from `scripts/measure_macd_xauusd.py`, which grew this logic while
producing D-123 through D-127 and was the only place it existed. The dashboard's
research console needs the same arithmetic, and two implementations of "what did
this strategy actually earn after costs" is exactly the drift this codebase
refuses elsewhere (`strategy_for` in `mt5_runner.py` makes the same argument for
strategy lookup). The script now imports this; nothing was reimplemented.

## Why not `BacktestEngine`

`BacktestEngine` is the honest engine for MCX and is shared verbatim with the
live loop - but it builds `BarWindow.of(self._bars[:index + 1])` on every bar,
which is O(n) per bar and fine for the few hundred bars an MCX cycle runs. A
CFD study runs 50,000, where that is O(n^2) and impractically slow for no
benefit: neither CFD strategy reads further back than `warmup_bars()`. This uses
a bounded sliding window instead, and is otherwise the same order of operations
- decide on the closed bar, fill at the next price, charge the real costs.

`algo/backtest/bhavcopy_runner.py` set the precedent for a bespoke runner over a
different data shape; this is the CFD one.

## The costs are the point

Spread on every leg, financing on every night carried, commission as measured
(zero on the Vantage account, verified against 54 real deals - D-121). A stop or
trail exit fills at its own level, or the bar's open on a gap, never at
`bar.close +/- spread`: filling the one signal that exists to bound a loss at
wherever the bar happened to close is the flattery D-125 and D-127 were written
to avoid.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.enums import Exchange, Side, SignalAction
from algo.core.instrument import CfdId
from algo.core.position import Position
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.exchange.forex_calendar import ForexCalendar
from algo.exchange.specs import ContractSpecStore
from algo.strategy.context import BarContext, PositionView, SessionInfo
from algo.strategy.price_stop import stop_fill_price
from algo.strategy.trailing_profit_stop import (
    TrailState,
    advance_trail,
    start_trail,
    trail_fill_price,
)


@dataclass
class CfdTrade:
    """One round trip, with every cost that was actually charged against it."""

    side: Side
    lots: int
    entry_ts: datetime
    entry_price: Decimal
    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str = ""
    swap_paid: Decimal = Decimal("0")
    spread_paid: Decimal = Decimal("0")
    commission_paid: Decimal = Decimal("0")

    @property
    def gross_pnl(self) -> Decimal:
        if self.exit_price is None:
            return Decimal("0")
        move = self.exit_price - self.entry_price
        signed = move if self.side is Side.BUY else -move
        return signed * self.lots

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.spread_paid - self.swap_paid - self.commission_paid


@dataclass
class CfdResult:
    """What one run produced. Costs are reported separately, never netted away."""

    trades: list[CfdTrade] = field(default_factory=list)
    bars_seen: int = 0
    #: (timestamp, equity, positions open on that bar). The third field is
    #: carried so exposure is *measured* rather than assumed - `metrics.compute`
    #: derives it from whether a position was actually held, and defaulting it
    #: would report 0% or 100% exposure for every run alike.
    equity_curve: list[tuple[datetime, Decimal, int]] = field(default_factory=list)

    @property
    def gross_pnl(self) -> Decimal:
        return sum((t.gross_pnl for t in self.trades), Decimal("0"))

    @property
    def net_pnl(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal("0"))

    @property
    def spread_paid(self) -> Decimal:
        return sum((t.spread_paid for t in self.trades), Decimal("0"))

    @property
    def swap_paid(self) -> Decimal:
        return sum((t.swap_paid for t in self.trades), Decimal("0"))

    @property
    def commission_paid(self) -> Decimal:
        return sum((t.commission_paid for t in self.trades), Decimal("0"))

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def win_rate(self) -> Decimal | None:
        """`None`, not 0, with no trades - the same distinction the tearsheet
        and the dashboard's own stats panel already draw."""
        if not self.trades:
            return None
        return Decimal(self.wins) / Decimal(len(self.trades)) * Decimal("100")

    @property
    def max_drawdown_pct(self) -> Decimal | None:
        if not self.equity_curve:
            return None
        peak = self.equity_curve[0][1]
        worst = Decimal("0")
        for _, equity, _open in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak * Decimal("100"))
        return worst


@dataclass(frozen=True, slots=True)
class CfdCosts:
    """The venue's charges. Defaults are the measured Vantage XAUUSD terms."""

    half_spread: Decimal = Decimal("0.145")
    #: Optional per-instant spread, from real tick history
    #: (`algo/data/mt5_spread.py`). When set it replaces the flat
    #: `half_spread`, so a fill at the 21:00 rollover is charged what the
    #: rollover actually costs rather than the same number as a fill in the
    #: London/New York overlap. `None` keeps the constant, which is what every
    #: run before tick sampling existed used.
    half_spread_at: Callable[[datetime], Decimal] | None = None
    swap: SwapModel = field(default_factory=SwapModel.vantage_xauusd)
    commission: CfdChargeModel = field(default_factory=CfdChargeModel.vantage_standard)


def session_date_for(calendar: ForexCalendar, ts: datetime) -> date:
    """Which session `ts` belongs to, tolerating a non-session instant.

    `ForexCalendar.session_day_for` raises past its verified range; a backtest
    over years of history will cross that, and refusing to price a whole study
    over a calendar edge would be the wrong trade-off for a research tool. Falls
    back to the bar's own date, which only shifts which night a swap is charged
    on, never whether one is.
    """
    for candidate in (ts.date(), ts.date() + timedelta(days=1)):
        try:
            if calendar.session_open(candidate) <= ts < calendar.session_close(candidate):
                return candidate
        except Exception:  # noqa: BLE001 - candidate may simply not be a session
            continue
    return ts.date()


def run_cfd_backtest(
    bars: list[Bar],
    *,
    instrument: CfdId,
    timeframe: Timeframe,
    strategy_factory: Callable[[], object],
    stop_loss_pct: Decimal,
    trail_activation_pct: Decimal,
    trail_pct: Decimal,
    lots: int = 100,
    starting_equity: Decimal = Decimal("100000"),
    costs: CfdCosts | None = None,
) -> CfdResult:
    """Feed `bars` through one strategy instance, charging real costs.

    `strategy_factory` builds a fresh strategy per call so an incremental one
    (`MacdCrossover`, which carries EMAs) is never reused across two series -
    the harness does not need to know which kind it was handed.

    The stop and trail percentages are passed in *as well as* being set on the
    strategy, because this function computes the fill price for a stop-triggered
    exit and must use the same level the strategy triggered on. A caller that
    let those disagree would report a fill at a level nothing crossed.
    """
    charged = costs or CfdCosts()
    calendar = ForexCalendar()
    specs = ContractSpecStore.default()
    strategy = strategy_factory()
    result = CfdResult()

    bars_in_session = max(int(23 * 60 / timeframe.minutes), 1)
    window_size = max(strategy.warmup_bars() + 5, 10)  # type: ignore[attr-defined]
    recent: deque[Bar] = deque(maxlen=window_size)

    open_trade: CfdTrade | None = None
    last_session: date | None = None
    trail_state: TrailState | None = None
    realised = Decimal("0")

    for index, bar in enumerate(bars):
        recent.append(bar)

        held: Position | None = None
        if open_trade is not None:
            signed = (
                Decimal(open_trade.lots)
                if open_trade.side is Side.BUY
                else -Decimal(open_trade.lots)
            )
            held = Position(
                instrument=instrument,
                lots=int(signed),
                qty=signed,
                cost_basis=Decimal(open_trade.lots) * open_trade.entry_price,
            )

        # The trail is mirrored here only so a trail-triggered exit can be
        # priced at the level it actually locked in; the strategy keeps its own.
        if held is not None:
            side = Side.BUY if held.qty > 0 else Side.SELL
            if trail_state is None or trail_state.side is not side:
                trail_state = start_trail(held.average_price, side)
            trail_state = advance_trail(trail_state, bar)
        else:
            trail_state = None

        session = session_date_for(calendar, bar.ts)
        # One night's financing each time a held position crosses into a new
        # session - the same instant the venue charges it. `carry_for` returns a
        # signed P&L contribution; `swap_paid` is a positive cost subtracted in
        # `net_pnl`, so the sign flips once, here, and nowhere else.
        if open_trade is not None and last_session is not None and session != last_session:
            open_trade.swap_paid += -charged.swap.carry_for(
                side=open_trade.side, lots=open_trade.lots, on=session
            )
        last_session = session

        ctx = BarContext(
            window=BarWindow.of(tuple(recent)),
            session=SessionInfo(
                session_date=session,
                is_us_dst=False,
                minutes_to_close=0,
                is_partial_bar=False,
                bar_index=index,
                bars_in_session=bars_in_session,
            ),
            specs=specs,
            positions=PositionView({} if held is None else {instrument.key: held}),
            timeframe=timeframe,
            exchange=Exchange.OTC,
        )

        signals = strategy.on_bar(ctx)  # type: ignore[attr-defined]
        result.bars_seen += 1

        for signal in signals:
            leg = signal.legs[0]
            closing = signal.action is SignalAction.CLOSE
            is_stop = closing and signal.reason.startswith("stop loss")
            is_trail = closing and signal.reason.startswith("trailing stop")

            if is_stop and held is not None:
                fill = stop_fill_price(bar, held, stop_loss_pct)
                extra_spread = Decimal("0")
            elif is_trail and trail_state is not None:
                fill = trail_fill_price(trail_state, bar, trail_pct)
                extra_spread = Decimal("0")
            else:
                # Measured at this bar's own instant when tick history supplied
                # a profile; the flat constant otherwise.
                half = (
                    charged.half_spread_at(bar.ts)
                    if charged.half_spread_at is not None
                    else charged.half_spread
                )
                fill = bar.close + (half if leg.direction is Side.BUY else -half)
                extra_spread = half * lots

            commission = charged.commission.charges_for(
                side=leg.direction,
                lots=lots,
                price=fill,
                multiplier=Decimal("1"),
                is_option=False,
                on=session,
            ).total

            if signal.action is SignalAction.OPEN:
                open_trade = CfdTrade(
                    side=leg.direction,
                    lots=lots,
                    entry_ts=bar.ts,
                    entry_price=fill,
                    spread_paid=extra_spread,
                    commission_paid=commission,
                )
            elif closing and open_trade is not None:
                open_trade.exit_ts = bar.ts
                open_trade.exit_price = fill
                open_trade.exit_reason = signal.reason
                open_trade.spread_paid += extra_spread
                open_trade.commission_paid += commission
                result.trades.append(open_trade)
                realised += open_trade.net_pnl
                open_trade = None
                trail_state = None

        # `held` is this bar's position as the strategy saw it, before any
        # signal above was applied - which is the honest reading of whether
        # the account was exposed across that bar.
        result.equity_curve.append(
            (bar.ts, starting_equity + realised, 0 if held is None else 1)
        )

    return result
