"""The backtest event loop. Bar by bar, never vectorised.

Brief §12 rules out "vectorised backtests that compute signals across the whole
series at once and then shift by one", because that pattern hides look-ahead and
cannot model intrabar stops. This loop processes one bar at a time and hands the
strategy a context built from `[0..i]` only.

The order of operations within a bar is the part worth reading carefully, because
it is where a backtest becomes either honest or flattering:

    1. Fill orders queued on the previous bar, at **this** bar's open.
    2. Mark positions at this bar's close and record the equity point.
    3. Build the context from bars `[0..i]` and ask the strategy.
    4. Size the resulting signals and queue the orders for the *next* bar's open.

A signal produced from bar `i` therefore cannot execute inside bar `i`. That one
rule removes the most common and most flattering backtest bug — deciding from a
bar's close and then filling somewhere inside that same bar.

The portfolio identity is re-checked after every fill and every mark. It is cheap,
and drift caught on the bar it happened is a five-minute fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.enums import Exchange, RejectReason, SignalAction
from algo.core.errors import DomainError
from algo.core.fill import Fill
from algo.core.ids import stable_hash
from algo.core.instrument import InstrumentId
from algo.core.order import Order
from algo.core.timeutil import iso, ist_date
from algo.exchange.calendar import MarketCalendar
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.portfolio.book import EquityPoint, Portfolio
from algo.risk.engine import Accepted, Rejected, RiskEngine, RiskSnapshot
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext, PositionView, build_session_info


@dataclass(frozen=True, slots=True)
class Rejection:
    """A signal the risk layer declined. Never silent (brief §8)."""

    ts_iso: str
    signal_id: str
    reason: RejectReason
    detail: str


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
    """Single-instrument bar-by-bar backtest.

    Multi-instrument runs arrive with the chain feed at Milestone 4. Building the
    single-instrument case first is what makes the falsification strategies
    meaningful — their expected results are computable by hand.
    """

    __slots__ = (
        "_bars",
        "_calendar",
        "_config_hash",
        "_exchange",
        "_instrument",
        "_is_option",
        "_portfolio",
        "_risk",
        "_sim",
        "_specs",
        "_strategy",
        "_timeframe",
    )

    def __init__(
        self,
        *,
        bars: list[Bar],
        calendar: MarketCalendar,
        specs: ContractSpecStore,
        strategy: Strategy,
        risk: RiskEngine,
        simulator: FillSimulator,
        portfolio: Portfolio,
        instrument: InstrumentId,
        timeframe: Timeframe,
        is_option: bool = False,
        config_hash: str = "",
        exchange: Exchange = Exchange.MCX,
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
        self._config_hash = config_hash
        self._exchange = exchange

    def run(self) -> BacktestResult:
        pending: list[Order] = []
        fills: list[Fill] = []
        rejections: list[Rejection] = []
        round_trips = 0
        spread_cost = Decimal("0")

        seen_in_session: dict[object, int] = {}

        for index, bar in enumerate(self._bars):
            session_day = ist_date(bar.ts)
            spec = self._specs.spec_for(self._instrument.underlying, self._exchange, session_day)
            key = self._instrument.key

            # ---- 1. orders queued on the previous bar fill at THIS bar's open
            for order_index, order in enumerate(pending):
                before = self._portfolio.position(self._instrument)
                was_open = before is not None and not before.is_flat

                fill = self._sim.fill(
                    fill_id=f"{order.client_order_id}#{index}.{order_index}",
                    client_order_id=order.client_order_id,
                    signal_id=order.signal_id,
                    instrument_key=key,
                    instrument=order.instrument,
                    side=order.side,
                    lots=order.lots,
                    reference_price=bar.open,
                    spec=spec,
                    ts_utc=bar.ts,
                    session_day=session_day,
                    is_option=self._is_option,
                )
                self._portfolio.apply_fill(fill, multiplier=spec.multiplier)
                fills.append(fill)
                spread_cost += fill.slippage * fill.qty * spec.multiplier

                after = self._portfolio.position(self._instrument)
                if was_open and after is not None and after.is_flat:
                    round_trips += 1

                self._portfolio.check_identity({key: bar.open})
            pending = []

            # ---- 2. mark at the close and record the equity point
            marks = {key: bar.close}
            self._portfolio.check_identity(marks)
            self._portfolio.record(bar.ts, marks)

            # ---- 3. the strategy sees bars [0..i] and nothing else
            bar_index = seen_in_session.get(session_day, 0)
            seen_in_session[session_day] = bar_index + 1
            ctx = BarContext(
                window=BarWindow.of(tuple(self._bars[: index + 1])),
                session=build_session_info(
                    bar=bar,
                    session_close=self._calendar.session_close(session_day),
                    is_us_dst=self._calendar.is_us_dst_session(session_day),
                    bar_index=bar_index,
                    bars_in_session=len(
                        self._calendar.bar_boundaries(session_day, self._timeframe)
                    ),
                ),
                specs=self._specs,
                positions=PositionView(self._portfolio.positions_by_key()),
                timeframe=self._timeframe,
                exchange=self._exchange,
            )
            signals = self._strategy.on_bar(ctx)

            # ---- 4. size, and queue for the NEXT bar's open
            if index == len(self._bars) - 1:
                # Nothing can execute after the last bar; queuing would silently
                # drop the orders instead of saying so.
                continue

            position = self._portfolio.position(self._instrument)
            snapshot = RiskSnapshot(
                now=bar.ts,
                session_day=session_day,
                equity=self._portfolio.equity(marks),
                open_position_count=len(self._portfolio.open_positions()),
                lots_held=abs(position.lots) if position else 0,
            )
            for signal in signals:
                is_closing = signal.action is SignalAction.CLOSE
                decision = self._risk.evaluate(
                    signal,
                    RiskSnapshot(
                        now=snapshot.now,
                        session_day=snapshot.session_day,
                        equity=snapshot.equity,
                        # A closing signal must never be refused for a position
                        # cap it is itself reducing.
                        open_position_count=0 if is_closing else snapshot.open_position_count,
                        lots_held=0 if is_closing else snapshot.lots_held,
                    ),
                    spec=spec,
                )
                if isinstance(decision, Rejected):
                    rejections.append(
                        Rejection(
                            ts_iso=iso(bar.ts),
                            signal_id=signal.signal_id,
                            reason=decision.reason,
                            detail=decision.detail,
                        )
                    )
                    continue
                if isinstance(decision, Accepted):
                    pending.extend(decision.orders)

        final_marks = {self._instrument.key: self._bars[-1].close}
        final_equity = self._portfolio.equity(final_marks)

        spec = self._specs.spec_for(
            self._instrument.underlying, self._exchange, ist_date(self._bars[0].ts)
        )
        spread_predicted, charges_predicted = self._sim.predicted_round_trip_cost(
            price=self._bars[0].close,
            lots=1,
            spec=spec,
            is_option=self._is_option,
            on=ist_date(self._bars[0].ts),
        )

        warnings: list[str] = []
        costs_verified = self._sim.costs_verified
        if not costs_verified:
            warnings.append(
                "CHARGE RATES ARE PLACEHOLDERS — net P&L is not calibrated (D-011, Q6)"
            )
        spread_measured = self._sim.spread_measured
        if not spread_measured:
            warnings.append(
                "SPREAD IS MODELLED, NOT MEASURED — the recorder replaces this at M1.5"
            )

        return BacktestResult(
            equity_curve=self._portfolio.curve,
            fills=tuple(fills),
            rejections=tuple(rejections),
            starting_equity=self._portfolio.starting_equity,
            final_equity=final_equity,
            net_pnl=final_equity - self._portfolio.starting_equity,
            total_charges=self._portfolio.charges.total,
            spread_cost=spread_cost,
            round_trips=round_trips,
            predicted_cost=(spread_predicted + charges_predicted.total) * Decimal(round_trips),
            dataset_hash=dataset_hash(self._bars),
            config_hash=self._config_hash,
            costs_verified=costs_verified,
            spread_measured=spread_measured,
            warnings=tuple(warnings),
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
