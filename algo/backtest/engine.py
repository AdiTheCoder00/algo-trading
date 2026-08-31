"""The backtest event loop. Bar by bar, never vectorised.

Brief §12 rules out "vectorised backtests that compute signals across the whole
series at once and then shift by one", because that pattern hides look-ahead and
cannot model intrabar stops. This loop processes one bar at a time and hands the
strategy a context built from `[0..i]` only.

The order of operations within a bar is where a backtest becomes either honest or
flattering:

    1. Fill orders queued on the previous bar, priced at **this** bar.
    2. Apply the risk layer's own exits — forced pre-expiry closes first, then the
       combo stop and target.
    3. Mark every open position and record the equity point.
    4. Build the context from bars `[0..i]` and ask the strategy.
    5. Size the resulting signals and queue the orders for the *next* bar.

A signal produced from bar `i` therefore cannot execute inside bar `i` (D-038).
That one rule removes the most common and most flattering backtest bug.

Step 2 runs before step 4 deliberately. **The risk layer outranks the strategy.**
A forced pre-expiry exit is not a suggestion the strategy may decline, and a
tripped kill switch has to stop new orders on the bar it trips rather than the
one after.

One engine serves both the single-instrument futures case and the multi-leg
options case. The difference lives entirely in the `PriceSource` (brief §4).

The portfolio identity is re-checked after every fill and every mark.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from algo.backtest.prices import BarPriceSource, PriceSource, require_mark
from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.enums import Exchange, RejectReason, Side, SignalAction
from algo.core.errors import AlgoError, CalendarError, DomainError
from algo.core.fill import Fill
from algo.core.ids import stable_hash
from algo.core.instrument import InstrumentId, OptionId
from algo.core.order import Order
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso, ist_date
from algo.core.trade import Trade
from algo.costs.cfd import SwapModel
from algo.costs.margin import MarginModel
from algo.exchange.calendar import SessionCalendar
from algo.exchange.expiries import ExpiryCalendar, ExpirySet
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.persistence.state import EquityRow, PositionRow, SignalRow, StateStore
from algo.portfolio.book import EquityPoint, Portfolio
from algo.portfolio.trades import TradeBuilder
from algo.risk.devolvement import DevolvementGuard
from algo.risk.engine import Accepted, Rejected, RiskEngine, RiskSnapshot
from algo.risk.exits import ExitLevels, ExitReason, check_viability, resolve_levels
from algo.risk.killswitch import KillSwitch
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext, ChainProvider, PositionView, build_session_info


@dataclass(frozen=True, slots=True)
class Rejection:
    """A signal the risk layer declined. Never silent (brief §8)."""

    ts_iso: str
    signal_id: str
    reason: RejectReason
    detail: str


@dataclass(frozen=True, slots=True)
class BarDecision:
    """What the engine decided on one bar, before anything is executed.

    The return type of the seam between deciding and executing (`decide`). It
    carries intentions, never results: `orders` have not been sent, and on a
    live loop may never be - the router can still refuse them.
    """

    orders: tuple[Order, ...]
    exit_event: ExitEvent | None
    notes: tuple[str, ...]
    rejections: tuple[Rejection, ...]
    marks: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class Note:
    """A diagnostic from the strategy — usually why it did *not* trade."""

    ts_iso: str
    message: str


@dataclass(frozen=True, slots=True)
class ExitEvent:
    """A close driven by the risk layer rather than by the strategy."""

    ts_iso: str
    reason: ExitReason
    detail: str
    combo_pnl: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything one run produced, plus what it is and is not evidence of."""

    equity_curve: tuple[EquityPoint, ...]
    fills: tuple[Fill, ...]
    rejections: tuple[Rejection, ...]
    starting_equity: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    total_charges: Decimal
    spread_cost: Decimal
    round_trips: int
    predicted_cost: Decimal
    dataset_hash: str
    config_hash: str
    costs_verified: bool
    spread_measured: bool
    notes: tuple[Note, ...] = field(default_factory=tuple)
    exits: tuple[ExitEvent, ...] = field(default_factory=tuple)
    trades: tuple[Trade, ...] = field(default_factory=tuple)
    margin_calibrated: bool = True
    kill_switch_tripped: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def gross_pnl(self) -> Decimal:
        """P&L before any cost. net + charges + spread paid."""
        return self.net_pnl + self.total_charges + self.spread_cost

    @property
    def realised_cost(self) -> Decimal:
        return self.total_charges + self.spread_cost

    @property
    def cost_drag_pct(self) -> Decimal | None:
        """Total cost as a percentage of gross P&L. None when gross is zero."""
        if self.gross_pnl == 0:
            return None
        return self.realised_cost / abs(self.gross_pnl) * Decimal("100")


class BacktestEngine:
    """Bar-by-bar backtest, single instrument or multi-leg options."""

    __slots__ = (
        "_bar_counts",
        "_bars",
        "_broker",
        "_calendar",
        "_chain_provider",
        "_config_hash",
        "_devolvement",
        "_entry_credit",
        "_exchange",
        "_expiries",
        "_flatten_on_trip",
        "_flatten_requested",
        "_instrument",
        "_is_option",
        "_kill_switch",
        "_last_financed_session",
        "_levels",
        "_margin",
        "_mode",
        "_on_stop_viability_breach",
        "_pending_exit_reason",
        "_portfolio",
        "_prices",
        "_risk",
        "_session_day",
        "_session_started",
        "_signal_meta",
        "_sim",
        "_specs",
        "_state",
        "_stop_viability_threshold",
        "_strategy",
        "_swap",
        "_timeframe",
        "_trade_builder",
    )

    def __init__(
        self,
        *,
        bars: list[Bar],
        calendar: SessionCalendar,
        specs: ContractSpecStore,
        strategy: Strategy,
        risk: RiskEngine,
        simulator: FillSimulator,
        portfolio: Portfolio,
        instrument: InstrumentId,
        timeframe: Timeframe,
        is_option: bool = False,
        session_day_for: Callable[[datetime], date] = ist_date,
        swap: SwapModel | None = None,
        config_hash: str = "",
        exchange: Exchange = Exchange.MCX,
        price_source: PriceSource | None = None,
        chain_provider: ChainProvider | None = None,
        expiries: ExpiryCalendar | None = None,
        devolvement: DevolvementGuard | None = None,
        kill_switch: KillSwitch | None = None,
        flatten_on_trip: bool = False,
        margin: MarginModel | None = None,
        state: StateStore | None = None,
        mode: str = "backtest",
        broker: str = "backtest",
        stop_viability_threshold: Decimal | None = None,
        on_stop_viability_breach: str = "warn",
    ) -> None:
        if not bars:
            raise DomainError("cannot run a backtest with no bars")
        self._bars = bars
        self._calendar = calendar
        self._specs = specs
        self._strategy = strategy
        self._risk = risk
        self._sim = simulator
        self._portfolio = portfolio
        self._instrument = instrument
        self._is_option = is_option
        self._timeframe = timeframe
        self._session_day = session_day_for
        self._config_hash = config_hash
        self._exchange = exchange
        self._prices = price_source or BarPriceSource(instrument, bars)
        self._chain_provider = chain_provider
        self._expiries = expiries
        self._devolvement = devolvement
        self._kill_switch = kill_switch
        self._margin = margin
        self._state = state
        self._mode = mode
        self._broker = broker
        self._stop_viability_threshold = stop_viability_threshold
        self._on_stop_viability_breach = on_stop_viability_breach
        self._levels: ExitLevels | None = None
        self._entry_credit = Decimal("0")
        self._flatten_requested = False
        self._flatten_on_trip = flatten_on_trip
        self._bar_counts: dict[date, int] = {}
        self._trade_builder = TradeBuilder(strategy.strategy_id)
        self._signal_meta: dict[str, tuple[str, dict[str, str]]] = {}
        self._pending_exit_reason = ""
        self._session_started: set[date] = set()
        self._swap = swap
        self._last_financed_session: date | None = None

    # ------------------------------------------------------------------- live
    def append_bar(self, bar: Bar) -> int:
        """Add a newly closed bar and return its index.

        The look-ahead firewall is unaffected: `_ask_strategy` still builds the
        context from `_bars[:index+1]`, and in live there is nothing after the
        current bar to leak because it has not happened yet.
        """
        if self._bars and bar.ts <= self._bars[-1].ts:
            raise DomainError(
                f"bar at {bar.ts} is not after the last bar at {self._bars[-1].ts}; "
                "a live feed must deliver closed bars in order"
            )
        self._bars.append(bar)
        # The price source indexed the bars it was given at construction, so a
        # bar appended afterwards has to be handed over explicitly or the first
        # mark against it raises. Sources that carry their own data - a chain
        # feed, for instance - have no `add` and need nothing here.
        add = getattr(self._prices, "add", None)
        if callable(add):
            add(bar)
        return len(self._bars) - 1

    def mark_for(self, instrument_key: str) -> Decimal | None:
        """What one instrument is worth as of the latest bar, or None.

        The paper broker needs a price to fill against and has no price source of
        its own. Routing it through the engine's means paper fills and backtest
        fills are marked from the same data, so a disagreement between them is
        never just two price lookups that diverged.
        """
        if not self._bars:
            return None
        return self._prices.mark(instrument_key, self._bars[-1].ts)

    def apply_fill(self, fill: Fill, *, session_day: date) -> None:
        """Book a fill that happened elsewhere - a real broker's, not ours.

        The same two effects `_execute` applies after simulating one, so a paper
        or live fill reaches the portfolio and the strategy by the identical
        path. `_execute` is *simulate then apply*; this is the apply half on its
        own, for fills the engine did not invent.
        """
        spec = self._spec_for(fill.instrument, session_day)
        self._portfolio.apply_fill(fill, multiplier=spec.multiplier)
        # The strategy learns what actually happened, never what it asked for
        # (D-041). Cycle-cadence state depends on this being a fill.
        self._strategy.on_fill(fill)
        # Persisted here rather than at the end of a run, because a live process
        # that dies has no end of run. A fill is the only thing that changes this
        # state, so writing it on each one is both sufficient and cheap.
        self._save_strategy_state()

    def _save_strategy_state(self) -> None:
        if self._state is None:
            return
        state = self._strategy.state()
        if not state:
            return
        self._state.record_strategy_state(
            strategy_id=self._strategy.strategy_id,
            params_hash=self._strategy.params_hash(),
            state=state,
        )

    def restore_strategy_state(self) -> bool:
        """Reload the strategy's own state from the store. True if anything was.

        Not called automatically from `__init__`: a backtest starts from nothing
        by definition, and silently inheriting a previous run's cadence would
        make results depend on what happened to be in the state file. A live
        session opts in - see `algo live`.
        """
        if self._state is None:
            return False
        saved = self._state.strategy_state(
            strategy_id=self._strategy.strategy_id,
            params_hash=self._strategy.params_hash(),
        )
        if saved is None:
            return False
        self._strategy.restore(saved)
        return True

    # ---------------------------------------------------------------- decision
    def decide(self, bar: Bar, index: int, *, is_last: bool = False) -> BarDecision:
        """Everything the engine decides on one bar, executing nothing.

        Extracted from `run` so that a **live** loop can reach the identical
        decision path rather than growing a second copy of it. The paper broker
        already states this principle for fills - "not a similar one, the same
        `FillSimulator` object" - and it matters more here, because these are the
        steps that choose what to trade and how much.

        The seam sits exactly where execution begins. `_risk_exits` and `_size`
        already *return* orders instead of executing them, so what comes back is
        an intention: the backtest hands it to `FillSimulator` on the next bar, a
        live loop hands it to `Router.place_all`. Neither can change what was
        decided.

        What is deliberately **not** in here: settling fills and building trades.
        A backtest settles against the next bar's prices; a live loop learns its
        fills from the broker asynchronously. Those are genuinely different and
        pretending otherwise would be the drift this method exists to prevent.

        `is_last` suppresses order emission on a final bar - nothing can execute
        after it, and queuing would silently drop the orders instead of saying so.
        The strategy is still consulted and its notes still recorded, because
        "what would it have done" is worth keeping.
        """
        session_day = self._session_day(bar.ts)
        marks = self._marks(bar)
        self._portfolio.check_identity(marks)

        if self._kill_switch is not None and session_day not in self._session_started:
            self._session_started.add(session_day)
            self._kill_switch.start_session(session_day, self._portfolio.equity(marks))

        # Charged *after* the session's opening equity is recorded, so a daily
        # loss limit counts the financing against the day it is booked. A risk
        # limit that silently excludes a known, certain cost is the weaker of
        # the two readings.
        self._charge_financing(session_day)

        # ---- the risk layer acts before the strategy is consulted
        exit_orders, exit_event = self._risk_exits(bar, session_day, marks)
        if exit_event is not None:
            self._pending_exit_reason = exit_event.reason.value

        # ---- mark and record
        self._portfolio.record(bar.ts, marks)
        if self._kill_switch is not None:
            self._kill_switch.observe_equity(self._portfolio.equity(marks), bar.ts)
        self._record_bar_state(bar, marks, session_day)

        # ---- the strategy sees bars [0..i] and nothing else
        signals = self._ask_strategy(bar, index, session_day)
        notes = tuple(self._strategy.drain_notes())
        if self._state is not None:
            for message in notes:
                self._state.record_note(bar.ts, message)

        rejections: list[Rejection] = []
        if is_last:
            orders: tuple[Order, ...] = ()
        elif exit_orders:
            # A risk-layer exit outranks anything the strategy asked for.
            orders = tuple(exit_orders)
        else:
            orders = tuple(self._size(signals, bar, session_day, rejections))

        return BarDecision(
            orders=orders,
            exit_event=exit_event,
            notes=notes,
            rejections=tuple(rejections),
            marks=marks,
        )

    # -------------------------------------------------------------------- run
    def _charge_financing(self, session_day: date) -> None:
        """Book one night's financing on anything held into a new session.

        Placed in `decide` rather than in `run` so the backtest and the live
        loop charge it on the identical path - the whole point of `decide`
        being shared. Without this the live loop reported a P&L that ignored
        the largest cost of holding a CFD overnight, which for a strategy that
        carries positions for days is not a rounding item (D-121: a long pays
        about 6.6% of notional a year).

        Two things this deliberately does not attempt. It charges whatever is
        held when the first bar of the new session arrives, so a position
        opened on that same bar is charged one night it did not strictly hold -
        the same one-bar boundary `cfd_runner.py` has always had, and an error
        bounded by a single night. And there is no historical rate series to
        apply (see `SwapModel.is_verified`), so today's rate is charged to the
        whole backtest; the model says so rather than implying precision it has
        no source for.
        """
        previous = self._last_financed_session
        self._last_financed_session = session_day
        if self._swap is None or previous is None or session_day == previous:
            return
        for position in self._portfolio.open_positions():
            if position.qty == 0:
                continue
            side = Side.BUY if position.qty > 0 else Side.SELL
            # `carry_for` returns a signed P&L contribution; `apply_financing`
            # takes a cost. The sign flips once, here.
            self._portfolio.apply_financing(
                -self._swap.carry_for(
                    side=side, lots=abs(int(position.lots)), on=session_day
                )
            )

    def run(self) -> BacktestResult:
        pending: list[Order] = []
        fills: list[Fill] = []
        rejections: list[Rejection] = []
        notes: list[Note] = []
        exits: list[ExitEvent] = []
        trades: list[Trade] = []
        round_trips = 0
        spread_cost = Decimal("0")
        self._begin_health()

        for index, bar in enumerate(self._bars):
            session_day = self._session_day(bar.ts)
            was_open = not self._portfolio.is_flat

            # ---- 0. the dashboard's halt requests act on the next bar
            if self._state is not None and self._kill_switch is not None:
                self._consume_kill_switch_requests(bar.ts)

            # ---- 1. orders queued on the previous bar transact against this one
            bar_fills: list[Fill] = []
            for order_index, order in enumerate(pending):
                if not self._trade_builder.is_open:
                    self._begin_trade(order, bar)
                fill = self._execute(order, bar, index, order_index, session_day)
                bar_fills.append(fill)
                self._trade_builder.add(fill)
                spread_cost += fill.slippage * fill.qty * self._spec_for(
                    order.instrument, session_day
                ).multiplier

                # Checked per fill, not per bar: a close and a re-open landing on
                # the same bar are two trades, and grouping them would report one.
                if self._portfolio.is_flat and self._trade_builder.is_open:
                    trade = self._trade_builder.close(
                        at=bar.ts,
                        realised_now=self._portfolio.realised_pnl,
                        exit_reason=self._pending_exit_reason or "strategy close",
                    )
                    trades.append(trade)
                    round_trips += 1
                    self._record_trade(trade)
                    self._pending_exit_reason = ""
                    self._levels = None
                    self._entry_credit = Decimal("0")
            fills.extend(bar_fills)
            pending = []

            if not was_open and not self._portfolio.is_flat and bar_fills:
                self._open_levels(bar_fills, bar, session_day)
                self._trade_builder.set_risk(
                    self._levels.stop_loss if self._levels is not None else None
                )

            # ---- 2-5. decide. Shared verbatim with the live loop - see `decide`.
            decision = self.decide(bar, index, is_last=index == len(self._bars) - 1)
            if decision.exit_event is not None:
                exits.append(decision.exit_event)
            notes.extend(
                Note(ts_iso=iso(bar.ts), message=message) for message in decision.notes
            )
            rejections.extend(decision.rejections)
            pending.extend(decision.orders)

        return self._finish(
            fills, rejections, notes, exits, round_trips, spread_cost, trades
        )

    # --------------------------------------------------------------- internals
    def _spec_for(self, instrument: InstrumentId, on: date) -> Any:
        return self._specs.spec_for(instrument.underlying, self._exchange, on)

    def _marks(self, bar: Bar) -> dict[str, Decimal]:
        return {
            position.instrument.key: require_mark(self._prices, position.instrument.key, bar.ts)
            for position in self._portfolio.open_positions()
        }

    def _execute(
        self, order: Order, bar: Bar, index: int, order_index: int, session_day: date
    ) -> Fill:
        reference = self._prices.fill_reference(order.instrument.key, bar.ts)
        if reference is None:
            raise DomainError(
                f"no price available to fill {order.instrument.key} at {bar.ts} - "
                "an order cannot be filled against a price nobody quoted"
            )
        spec = self._spec_for(order.instrument, session_day)
        fill = self._sim.fill(
            fill_id=f"{order.client_order_id}#{index}.{order_index}",
            client_order_id=order.client_order_id,
            signal_id=order.signal_id,
            instrument_key=order.instrument.key,
            instrument=order.instrument,
            side=order.side,
            lots=order.lots,
            reference_price=reference,
            spec=spec,
            ts_utc=bar.ts,
            session_day=session_day,
            is_option=isinstance(order.instrument, OptionId),
        )
        self.apply_fill(fill, session_day=session_day)
        return fill

    def _open_levels(self, entry_fills: list[Fill], bar: Bar, session_day: date) -> None:
        """Freeze take-profit and stop levels at entry (D-025).

        The margin figure comes from the margin model, and the configured exits
        are a percentage *of that margin* — so an approximate margin makes an
        approximate stop. The run reports that rather than hiding it.
        """
        take_profit = getattr(self._strategy, "_take_profit", None)
        # Not part of the guard: a stop of None is now a legitimate configuration
        # (D-102), not a strategy that has no exit policy. `_take_profit` alone
        # decides whether this strategy manages combo exits at all - the two are
        # always set together.
        stop_loss = getattr(self._strategy, "_stop_loss", None)
        if self._margin is None or take_profit is None:
            return

        notional = Decimal("0")
        credit = Decimal("0")
        for fill in entry_fills:
            spec = self._spec_for(fill.instrument, session_day)
            notional += fill.price * fill.qty * spec.multiplier
            if fill.side is Side.SELL:
                credit += fill.price * fill.qty * spec.multiplier

        # Margin is taken for the COMBO, not summed across legs. SPAN nets a
        # strangle's two legs against each other — they cannot both go wrong at
        # once — so summing per-leg margin roughly doubles it. That matters far
        # more than it sounds: the configured stop is 1% OF MARGIN, so an
        # overstated margin silently doubles the stop distance and turns the
        # strategy into a different one. Found by the end-to-end test asserting
        # a 1,000 stop and getting 2,000.
        lots = max((abs(p.lots) for p in self._portfolio.open_positions()), default=1)
        margin = self._margin.margin_for(
            notional=notional, lots=max(lots, 1), is_short_option=True
        )
        self._entry_credit = credit
        self._levels = resolve_levels(
            take_profit=take_profit,
            stop_loss=stop_loss,
            margin=margin,
            equity=self._portfolio.equity(self._marks(bar)),
            credit=credit,
        )
        self._check_stop_viability(entry_fills, session_day)

    def _check_stop_viability(self, entry_fills: list[Fill], session_day: date) -> None:
        """D-024: a position that opens at its own stop is not a strategy.

        The stop was just frozen at entry; this compares it against what a round
        trip in these legs costs. `warn` records a note the dashboard shows;
        `refuse` fails the run, because the fills have already executed and the
        only honest form of refusal left is to stop.
        """
        if self._levels is None or self._stop_viability_threshold is None:
            return
        if self._levels.stop_loss is None:
            # No stop to compare against the cost of trading. The absence is
            # reported once for the whole run by `_warn_no_stop`, not per entry.
            return
        cost = Decimal("0")
        for fill in entry_fills:
            spec = self._spec_for(fill.instrument, session_day)
            spread, charges = self._sim.predicted_round_trip_cost(
                price=fill.price,
                lots=abs(fill.lots),
                spec=spec,
                is_option=True,
                on=session_day,
            )
            cost += spread + charges.total
        check = check_viability(
            stop=self._levels.stop_loss,
            round_trip_cost=cost,
            threshold=self._stop_viability_threshold,
        )
        if check.passes:
            return
        message = check.message()
        if self._on_stop_viability_breach == "refuse":
            raise DomainError(
                f"refusing to continue: {message} (on_stop_viability_breach=refuse)"
            )
        self._strategy.note(f"stop viability: {message}")

    def _book_margin(self, session_day: date, marks: dict[str, Decimal]) -> Decimal:
        """Margin currently blocked, on the same combo basis as _open_levels."""
        if self._margin is None:
            return Decimal("0")
        notional = Decimal("0")
        lots = 1
        for position in self._portfolio.open_positions():
            notional += abs(position.qty) * marks[position.instrument.key] * position.multiplier
            lots = max(lots, abs(position.lots))
        return self._margin.margin_for(
            notional=notional,
            lots=lots,
            is_short_option=any(
                isinstance(position.instrument, OptionId)
                for position in self._portfolio.open_positions()
            ),
        )

    def _proposed_margin(
        self, signal: Signal, lots: int, session_day: date, bar: Bar
    ) -> Decimal:
        """What this signal would block, sized to `lots`. Zero without a model."""
        if self._margin is None:
            return Decimal("0")
        notional = Decimal("0")
        for leg in signal.legs:
            price = self._prices.fill_reference(leg.instrument.key, bar.ts) or bar.close
            spec = self._spec_for(leg.instrument, session_day)
            notional += price * Decimal(lots * leg.ratio) * spec.multiplier
        return self._margin.margin_for(
            notional=notional,
            lots=max(lots, 1),
            is_short_option=any(isinstance(leg.instrument, OptionId) for leg in signal.legs),
        )

    def _begin_trade(self, order: Order, bar: Bar) -> None:
        reason, context = self._signal_meta.get(
            order.signal_id, ("no reason recorded", {})
        )
        self._trade_builder.open(
            signal_id=order.signal_id,
            reason=reason,
            context=context,
            risk_r=None,
            at=bar.ts,
            realised_so_far=self._portfolio.realised_pnl,
        )

    def _risk_exits(
        self, bar: Bar, session_day: date, marks: dict[str, Decimal]
    ) -> tuple[list[Order], ExitEvent | None]:
        """Forced pre-expiry closes first, then the combo stop and target."""
        if self._portfolio.is_flat:
            return [], None

        reason: ExitReason | None = None
        detail = ""

        if self._flatten_requested:
            # The halt arrived with flatten explicitly requested (D-012): halting
            # alone stops new orders; flattening is the caller's decision.
            reason = ExitReason.KILL_SWITCH
            detail = "flatten requested with the halt"
        elif self._flatten_on_trip and self._kill_switch is not None and (
            self._kill_switch.is_tripped
        ):
            # The switch tripped on its *own* limits rather than on a dashboard
            # request. Until D-115 this branch did not exist and the configured
            # `flatten_on_trip` decided nothing at all - the engine halted new
            # entries and left an open position running whatever the file said.
            state = self._kill_switch.state
            reason = ExitReason.KILL_SWITCH
            detail = f"flatten_on_trip: {state.reason} - {state.detail}"

        cycle = self._current_cycle()
        if self._devolvement is not None and cycle is not None:
            verdict = self._devolvement.requires_option_exit(cycle, session_day)
            if verdict is not None:
                reason, detail = ExitReason.FORCED_PRE_EXPIRY, verdict.detail

        combo = self._portfolio.unrealised_pnl(marks)
        if reason is None and self._levels is not None:
            fired = self._levels.check(combo)
            if fired is not None:
                reason = fired
                detail = (
                    f"combo P&L {combo} against take profit {self._levels.take_profit} "
                    + (
                        f"and stop {self._levels.stop_loss}"
                        if self._levels.stop_loss is not None
                        else "with no stop configured"
                    )
                )

        if reason is None:
            return [], None
        return self._flatten_orders(bar), ExitEvent(
            ts_iso=iso(bar.ts), reason=reason, detail=detail, combo_pnl=combo
        )

    def _current_cycle(self) -> ExpirySet | None:
        if self._expiries is None:
            return None
        for position in self._portfolio.open_positions():
            option = position.instrument
            if isinstance(option, OptionId):
                try:
                    return self._expiries.expiry_set(
                        option.underlying,
                        option.option_expiry.year,
                        option.option_expiry.month,
                    )
                except AlgoError as exc:
                    # Brief §12: never swallow. Without the cycle the devolvement
                    # guard cannot act, and that has to be visible.
                    self._strategy.note(
                        f"expiry cycle for {option.key} could not be resolved ({exc}); "
                        "the devolvement guard is blind for this position"
                    )
                    return None
        return None

    def _flatten_orders(self, bar: Bar) -> list[Order]:
        """Closing orders for every open position, sized to what is actually held."""
        orders: list[Order] = []
        for position in self._portfolio.open_positions():
            closing = Side.SELL if position.qty > 0 else Side.BUY
            signal = Signal(
                signal_id=stable_hash({"flatten": position.instrument.key, "at": iso(bar.ts)}),
                strategy_id=self._strategy.strategy_id,
                ts=bar.ts,
                action=SignalAction.CLOSE,
                legs=(
                    SignalLeg(
                        instrument=position.instrument,
                        direction=closing,
                        entry=PriceIntent.market(),
                    ),
                ),
                reason="risk layer flattening the position",
            )
            decision = self._risk.evaluate(
                signal,
                RiskSnapshot(
                    now=bar.ts,
                    session_day=self._session_day(bar.ts),
                    equity=self._portfolio.starting_equity,
                    open_position_count=0,
                    lots_held=0,
                ),
                spec=self._spec_for(position.instrument, self._session_day(bar.ts)),
            )
            if isinstance(decision, Accepted):
                orders.extend(
                    order.model_copy(
                        update={"lots": abs(position.lots), "qty": abs(position.qty)}
                    )
                    for order in decision.orders
                )
        return orders

    def _ask_strategy(self, bar: Bar, index: int, session_day: date) -> list[Signal]:
        bar_index = self._bar_counts.get(session_day, 0)
        self._bar_counts[session_day] = bar_index + 1
        ctx = BarContext(
            window=BarWindow.of(tuple(self._bars[: index + 1])),
            session=build_session_info(
                bar=bar,
                session_close=self._calendar.session_close(session_day),
                is_us_dst=self._calendar.is_us_dst_session(session_day),
                bar_index=bar_index,
                bars_in_session=len(self._calendar.bar_boundaries(session_day, self._timeframe)),
            ),
            specs=self._specs,
            positions=PositionView(self._portfolio.positions_by_key()),
            timeframe=self._timeframe,
            exchange=self._exchange,
            chain_provider=self._chain_provider,
            expiries=self._expiries,
        )
        return self._strategy.on_bar(ctx)

    def _size(
        self,
        signals: list[Signal],
        bar: Bar,
        session_day: date,
        rejections: list[Rejection],
    ) -> list[Order]:
        if not signals:
            return []

        marks = self._marks(bar)
        position = self._portfolio.position(self._instrument)
        equity = self._portfolio.equity(marks)
        open_count = len(self._portfolio.open_positions())
        lots_held = abs(position.lots) if position else 0
        margin_used = self._book_margin(session_day, marks)

        queued: list[Order] = []
        for signal in signals:
            blocked = self._pre_trade_block(signal, session_day)
            if blocked is not None:
                rejection = Rejection(
                    ts_iso=iso(bar.ts),
                    signal_id=signal.signal_id,
                    reason=blocked[0],
                    detail=blocked[1],
                )
                rejections.append(rejection)
                self._record_rejection(bar, rejection)
                continue

            is_closing = signal.action is SignalAction.CLOSE

            def _propose(sized: int, signal: Signal = signal) -> Decimal:
                return self._proposed_margin(signal, sized, session_day, bar)

            decision = self._risk.evaluate(
                signal,
                RiskSnapshot(
                    now=bar.ts,
                    session_day=session_day,
                    equity=equity,
                    open_position_count=0 if is_closing else open_count,
                    lots_held=0 if is_closing else lots_held,
                    margin_used=Decimal("0") if is_closing else margin_used,
                    propose_margin=None if is_closing else _propose,
                ),
                spec=self._spec_for(signal.legs[0].instrument, session_day),
            )
            if isinstance(decision, Rejected):
                rejection = Rejection(
                    ts_iso=iso(bar.ts),
                    signal_id=signal.signal_id,
                    reason=decision.reason,
                    detail=decision.detail,
                )
                rejections.append(rejection)
                self._record_rejection(bar, rejection)
                continue
            if isinstance(decision, Accepted):
                self._signal_meta[signal.signal_id] = (signal.reason, dict(signal.context))
                if self._state is not None:
                    self._state.record_signal(
                        SignalRow(
                            signal_id=signal.signal_id,
                            ts=signal.ts,
                            strategy=signal.strategy_id,
                            action=signal.action.value,
                            reason=signal.reason,
                            context=dict(signal.context),
                        )
                    )
                queued.extend(decision.orders)
        return queued

    def _pre_trade_block(
        self, signal: Signal, session_day: date
    ) -> tuple[RejectReason, str] | None:
        """Checks that outrank sizing entirely."""
        if signal.action is not SignalAction.OPEN:
            return None

        if self._kill_switch is not None and not self._kill_switch.allows_new_orders():
            state = self._kill_switch.state
            return (
                RejectReason.KILL_SWITCH_TRIPPED,
                f"kill switch tripped: {state.reason} - {state.detail}",
            )

        if self._devolvement is None or self._expiries is None:
            return None

        for leg in signal.legs:
            option = leg.instrument
            if not isinstance(option, OptionId):
                continue
            try:
                cycle = self._expiries.expiry_set(
                    option.underlying, option.option_expiry.year, option.option_expiry.month
                )
            except AlgoError as exc:
                return (
                    RejectReason.DEVOLVEMENT_WINDOW,
                    f"cannot verify the expiry cycle for {option.key} ({exc}); refusing "
                    "to open a short option the devolvement guard cannot see",
                )
            verdict = self._devolvement.blocks_entry(cycle, session_day)
            if verdict is not None:
                return verdict.reason, verdict.detail
        return None

    def _finish(
        self,
        fills: list[Fill],
        rejections: list[Rejection],
        notes: list[Note],
        exits: list[ExitEvent],
        round_trips: int,
        spread_cost: Decimal,
        trades: list[Trade],
    ) -> BacktestResult:
        # A position still open when the data ends is not a completed round trip.
        # Counting it as one would put an unrealised figure into a realised
        # statistic; the open position is reported separately instead.
        self._trade_builder.abandon()
        self._end_health()
        last = self._bars[-1]
        final_marks = {
            position.instrument.key: require_mark(self._prices, position.instrument.key, last.ts)
            for position in self._portfolio.open_positions()
        }
        final_equity = self._portfolio.equity(final_marks)

        spec = self._spec_for(self._instrument, self._session_day(self._bars[0].ts))
        spread_predicted, charges_predicted = self._sim.predicted_round_trip_cost(
            price=self._bars[0].close,
            lots=1,
            spec=spec,
            is_option=self._is_option,
            on=self._session_day(self._bars[0].ts),
        )

        warnings: list[str] = []
        if not self._sim.costs_verified:
            warnings.append(
                "CHARGE RATES ARE SOURCED, NOT CONTRACT-NOTE VERIFIED - "
                "net P&L is not calibrated to the paisa (D-011, Q6)"
            )
        if not self._sim.spread_measured:
            warnings.append(
                "SPREAD IS MODELLED, NOT MEASURED - the recorder replaces this at M1.5"
            )
        margin_calibrated = self._margin is None or self._margin.is_calibrated
        if not margin_calibrated:
            warnings.append(
                "MARGIN IS APPROXIMATED - and the stop is a percentage of margin, so "
                "the stop level is approximate too (Q18)"
            )
        # Deliberately not conditioned on `is_calibrated` or on any data-quality
        # flag: this one is about the configuration itself, and it is the only
        # warning here describing a loss that has no bound rather than a number
        # that is imprecise.
        if getattr(self._strategy, "_stop_loss", "unset") is None:
            warnings.append(
                "NO STOP LOSS IS CONFIGURED - a short strangle's call side has no "
                "bounded loss, and the kill switch halts new entries without closing "
                "an open position (flatten_on_trip). The only exits left are take "
                "profit, the forced pre-expiry exit, and the end of the run (D-102)"
            )

        return BacktestResult(
            equity_curve=self._portfolio.curve,
            fills=tuple(fills),
            rejections=tuple(rejections),
            notes=tuple(notes),
            exits=tuple(exits),
            trades=tuple(trades),
            starting_equity=self._portfolio.starting_equity,
            final_equity=final_equity,
            net_pnl=final_equity - self._portfolio.starting_equity,
            total_charges=self._portfolio.charges.total,
            spread_cost=spread_cost,
            round_trips=round_trips,
            predicted_cost=(spread_predicted + charges_predicted.total) * Decimal(round_trips),
            dataset_hash=dataset_hash(self._bars),
            config_hash=self._config_hash,
            costs_verified=self._sim.costs_verified,
            spread_measured=self._sim.spread_measured,
            margin_calibrated=margin_calibrated,
            kill_switch_tripped=(
                self._kill_switch.is_tripped if self._kill_switch is not None else False
            ),
            warnings=tuple(warnings),
        )

    # --------------------------------------------------- dashboard state (opt-in)
    def _begin_health(self) -> None:
        """Stamp what this run is before the first bar (D-088)."""
        if self._state is None:
            return
        at = self._bars[0].ts
        self._state.set_health("engine", "running", at=at)
        self._state.set_health("mode", self._mode, at=at)
        self._state.set_health("broker", self._broker, at=at)
        self._state.set_health(
            "kill_switch",
            "tripped"
            if self._kill_switch is not None and self._kill_switch.is_tripped
            else "armed",
            at=at,
        )

    def _end_health(self) -> None:
        """The API turns these flags into warnings; they must reflect the run."""
        if self._state is None:
            return
        at = self._bars[-1].ts
        self._state.set_health("engine", "stopped", at=at)
        self._state.set_health(
            "costs_verified", "true" if self._sim.costs_verified else "false", at=at
        )
        self._state.set_health(
            "spread_measured", "true" if self._sim.spread_measured else "false", at=at
        )
        self._state.set_health(
            "margin_calibrated",
            "true" if self._margin is None or self._margin.is_calibrated else "false",
            at=at,
        )

    def _consume_kill_switch_requests(self, at: datetime) -> None:
        """Act on halt requests the dashboard or CLI recorded (D-066, D-088).

        The request is a request: the engine trips its own switch here, at its
        own bar, and marks the request acted. A request is acted on at the first
        bar at or after it was recorded, so one recorded mid-session does not
        cut short the session before it existed. A flatten request queues closes
        the way any other risk exit does — the halt takes effect immediately, the
        flatten fills on the next bar, exactly like a stop.
        """
        if self._state is None or self._kill_switch is None:
            return
        for request in self._state.pending_kill_switch_requests():
            if request.requested_at > at:
                continue
            if not self._kill_switch.is_tripped:
                self._kill_switch.trip_manually(
                    f"requested by {request.requested_by}: {request.reason}", at
                )
                if request.flatten:
                    self._flatten_requested = True
            self._state.mark_kill_switch_acted(request.id, at=at)

    def _record_bar_state(self, bar: Bar, marks: dict[str, Decimal], session_day: date) -> None:
        """One equity point and one positions snapshot per bar."""
        if self._state is None:
            return
        point = self._portfolio.curve[-1]
        self._state.record_equity(
            EquityRow(
                ts=point.ts,
                equity=point.equity,
                cash=point.cash,
                realised=point.realised_pnl,
                unrealised=point.unrealised_pnl,
                charges=point.charges,
                open_positions=point.open_positions,
            )
        )
        rows = [
            PositionRow(
                instrument_key=position.instrument.key,
                lots=position.lots,
                qty=position.qty,
                average_price=position.average_price,
                mark=marks[position.instrument.key],
                unrealised=position.unrealised_pnl(marks[position.instrument.key]),
                updated_at=bar.ts,
            )
            for position in self._portfolio.open_positions()
        ]
        self._state.replace_positions(rows)
        if self._kill_switch is not None:
            self._state.set_health(
                "kill_switch",
                "tripped" if self._kill_switch.is_tripped else "armed",
                at=bar.ts,
            )
        self._record_margin_utilisation(bar, marks, session_day)
        self._record_chain_snapshot(bar)

    def _record_margin_utilisation(
        self, bar: Bar, marks: dict[str, Decimal], session_day: date
    ) -> None:
        """How much of the configured margin cap is actually in use - the
        number that decides whether the *next* signal can size at all
        (RiskEngine's own cap check), surfaced so an operator does not have to
        infer it from whether entries are quietly being rejected."""
        if self._state is None or self._risk.margin_cap_pct is None:
            return
        used = self._book_margin(session_day, marks)
        equity = self._portfolio.equity(marks)
        cap = equity * self._risk.margin_cap_pct / Decimal("100")
        self._state.set_health("margin_used", str(used), at=bar.ts)
        self._state.set_health("margin_cap", str(cap), at=bar.ts)
        self._state.set_health("margin_cap_pct", str(self._risk.margin_cap_pct), at=bar.ts)

    def _record_chain_snapshot(self, bar: Bar) -> None:
        """The chain as of this bar, for the dashboard's chain panel - every
        strike, its delta and IV, and which two legs (if any) the strategy is
        actually holding. Only possible when the run has both a chain provider
        and an expiry calendar wired in (M4 onward); the M3 falsification and
        anything trading the underlying directly has neither, and skips this
        silently rather than raising over a feature that does not apply."""
        if self._state is None or self._chain_provider is None or self._expiries is None:
            return
        underlying = self._instrument.underlying
        try:
            cycle = self._expiries.nearest_expiry_on_or_after(underlying, self._session_day(bar.ts))
        except CalendarError:
            return
        snapshot = self._chain_provider.chain_at(underlying, cycle.option_expiry, bar.ts)
        if snapshot is None:
            return

        held = {
            position.instrument.key
            for position in self._portfolio.open_positions()
            if isinstance(position.instrument, OptionId)
        }
        session_day = self._session_day(bar.ts)
        self._state.record_chain_snapshot(
            {
                "ts": iso(snapshot.ts),
                "underlying": snapshot.underlying,
                "option_expiry": snapshot.option_expiry.isoformat(),
                "futures_price": str(snapshot.futures_price),
                "dte": cycle.days_to_option_expiry(session_day),
                # None when there is no devolvement guard wired in at all - a
                # run that never fought this rule should not claim to be
                # tracking a deadline it does not enforce.
                "forced_exit_in_sessions": (
                    self._devolvement.days_until_forced_exit(cycle, session_day)
                    if self._devolvement is not None
                    else None
                ),
                "rows": [
                    {
                        "strike": str(row.strike),
                        "right": row.right.value,
                        "bid": str(row.quote.bid) if row.quote.bid is not None else None,
                        "ask": str(row.quote.ask) if row.quote.ask is not None else None,
                        "ltp": str(row.quote.ltp) if row.quote.ltp is not None else None,
                        "volume": row.quote.volume,
                        "iv": row.iv,
                        "delta": row.delta,
                        "tradeable": row.is_tradeable,
                        # Why, not just whether: a strike rejected for a
                        # blown-out spread (Q17) is a different fact from one
                        # nobody quoted at all, and the panel should not blur
                        # them into one grey row.
                        "flag": row.quote.status().value,
                        "held": row.option.key in held,
                    }
                    for row in snapshot.rows
                ],
            }
        )

    def _record_trade(self, trade: Trade) -> None:
        if self._state is None:
            return
        self._state.record_trade(
            trade.trade_id, trade.opened_at, trade.closed_at, trade.to_log_row()
        )

    def _record_rejection(self, bar: Bar, rejection: Rejection) -> None:
        if self._state is None:
            return
        self._state.record_note(
            bar.ts,
            f"rejected {rejection.signal_id}: {rejection.reason} - {rejection.detail}",
        )


def dataset_hash(bars: list[Bar]) -> str:
    """Hash of the exact bars a run consumed.

    Stamped into every result so a number can always be tied back to the bytes
    that produced it — and so two runs that claim to be on the same data can be
    proved to have been.
    """
    return stable_hash(
        {
            "n": len(bars),
            "first": iso(bars[0].ts) if bars else "",
            "last": iso(bars[-1].ts) if bars else "",
            "closes": [str(b.close) for b in bars],
        }
    )
