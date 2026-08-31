"""Wire an MT5 feed to a CFD strategy and run it against the paper broker.

Everything downstream of a decision already existed - `LiveLoop`, `OrderRouter`,
`PaperBroker`, `OrderJournal`, `Reconciler`, `StateStore` - and so did
everything upstream: `Mt5BarFeed` (D-121), `TrendlineBreakout` and
`MacdCrossover` (D-123/D-124), `ForexCalendar`, `CfdChargeModel`. What was
missing was the wiring between them, and `Mt5Broker` was never referenced
outside its own tests. This module is that wiring, for the paper path only.

## Real data, simulated fills, no order reaches the broker

Bars come from a live MT5 terminal. Fills come from `PaperBroker`, using the
same `FillSimulator` the backtest uses - so this exercises the whole loop
(feed, strategy, risk, routing, state, dashboard) without `Mt5Broker.place`
ever being called. `Mt5Broker` has never placed an order in its life and its
write path is untested against a live endpoint; pointing it at an account for
the first time from inside an unattended loop is not the way to find out what
it does. The paper path proves everything except the fill, and the fill is the
one thing a demo account was going to approximate anyway.

`build_mt5_paper_loop` deliberately takes a `Broker` it does not construct, so
the caller decides what fills. Swapping in `Mt5Broker` is then a caller's
change with the caller's eyes on it, not a flag this module flips.

## Session day had to be made pluggable, and that was not optional

`LiveLoop` and `BacktestEngine` grouped every unit of work by `ist_date(ts)` -
correct for MCX's one-session-a-day shape, and the reason the measurement
script (D-123) bypassed the engine entirely. The first end-to-end run of this
loop showed that is not a tolerable approximation for FX but a hard failure: a
bar closing 20:00 UTC on a Friday is IST *Saturday*, and asking `ForexCalendar`
for Saturday's session raises `CalendarError` rather than shifting a boundary.
Half the bars in a normal week land that way.

So both take `session_day_for`, defaulting to `ist_date` so every MCX path is
byte-identical, and this loop passes `ForexCalendar.session_day_for` - which
names a session by its 21:00 close, the same instant financing is charged. The
kill switch's *daily* loss limit therefore also resets on the venue's own
rollover here rather than at IST midnight, which is the correct boundary for it
rather than an accident of the default.

**Swap is not charged.** `SwapModel` exists and is tested (D-121) but is wired
into no engine path (D-128 flagged this). A position held overnight in this loop
pays spread but not financing, so P&L here is optimistic for anything carried
past the rollover, in exactly the direction `cfd.py`'s own docstring warns
about. The measurement script models it; this loop does not, and says so rather
than letting a flattering number pass as the whole cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from algo.backtest.engine import BacktestEngine
from algo.backtest.prices import BarPriceSource
from algo.core.bar import Bar, Timeframe
from algo.core.clock import Clock
from algo.core.enums import Exchange
from algo.core.errors import DataError
from algo.core.instrument import CfdId
from algo.costs.cfd import CfdChargeModel
from algo.costs.slippage import NoSlippage
from algo.costs.spread import FixedTickSpread
from algo.exchange.forex_calendar import ForexCalendar
from algo.exchange.specs import ContractSpecStore
from algo.execution.broker import Broker
from algo.execution.fills import FillSimulator
from algo.execution.router import OrderRouter
from algo.live.feeds import BrokerFillFeed
from algo.live.loop import LiveLoop
from algo.persistence.journal import OrderJournal
from algo.persistence.state import StateStore
from algo.portfolio.book import Portfolio
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.risk.killswitch import KillSwitch
from algo.strategy.base import Strategy
from algo.strategy.macd_crossover import MacdCrossover
from algo.strategy.trendline_breakout import TrendlineBreakout

#: The round-trip spread measured live on the Vantage demo account (D-121):
#: $0.29 on a $0.01 tick, so 29 ticks, half of it charged per side. Modelled,
#: not measured per-fill - `health` reports `spread_measured: false` for this.
XAUUSD_SPREAD_TICKS = 29

#: 1 engine lot = 1 troy ounce = 0.01 MT5 lots (D-121). A "lot" here is
#: therefore much smaller than an MT5 lot, and sizing below is in ounces.
DEFAULT_LOTS = 100


@dataclass(frozen=True, slots=True)
class Mt5PaperRun:
    """Everything a caller needs to drive and inspect one paper run."""

    loop: LiveLoop
    engine: BacktestEngine
    broker: Broker
    journal: OrderJournal
    state: StateStore | None
    instrument: CfdId
    strategy: Strategy


def strategy_for(
    name: str,
    *,
    instrument: CfdId,
    stop_loss_pct: Decimal,
    trail_activation_pct: Decimal,
    trail_pct: Decimal,
    lookback: int = 20,
) -> Strategy:
    """Build one of the CFD strategies by name.

    Kept here rather than in the CLI so the dashboard's backtest console and the
    live loop resolve a name to a strategy the same way - two lookup tables that
    could disagree about what "breakout" means is exactly the drift this
    codebase keeps refusing elsewhere.
    """
    if name == "breakout":
        return TrendlineBreakout(
            instrument=instrument,
            lookback=lookback,
            stop_loss_pct=stop_loss_pct,
            trail_activation_pct=trail_activation_pct,
            trail_pct=trail_pct,
        )
    if name == "macd":
        return MacdCrossover(
            instrument=instrument,
            stop_loss_pct=stop_loss_pct,
            trail_activation_pct=trail_activation_pct,
            trail_pct=trail_pct,
        )
    raise DataError(f"unknown strategy {name!r}; available: breakout, macd")


def build_mt5_paper_loop(
    *,
    bars: object,
    broker: Broker,
    clock: Clock,
    strategy: Strategy,
    instrument: CfdId,
    timeframe: Timeframe,
    journal: OrderJournal,
    seed_bars: list[Bar],
    starting_equity: Decimal = Decimal("100000"),
    lots: int = DEFAULT_LOTS,
    max_lots: int = DEFAULT_LOTS,
    state: StateStore | None = None,
    kill_switch: KillSwitch | None = None,
) -> Mt5PaperRun:
    """Assemble the loop. Nothing here polls, sleeps, or decides when to stop."""
    if not seed_bars:
        raise DataError(
            "the loop needs at least one closed bar to start from - the terminal "
            "may still be downloading history, or the market may be shut"
        )

    calendar = ForexCalendar()
    specs = ContractSpecStore.default()
    simulator = FillSimulator(
        spread=FixedTickSpread(XAUUSD_SPREAD_TICKS),
        # A market order on a liquid CFD crosses the spread and no more; the
        # spread model above already carries that cost, and stacking a modelled
        # slippage on top would charge it twice.
        slippage=NoSlippage(),
        charges=CfdChargeModel.vantage_standard(),
    )

    # All but the newest: `LiveLoop.pass_once` appends that one itself, and
    # `append_bar` refuses a bar that is not strictly after the last. Seeding
    # only the first bar (what the MCX loop does, where the chain rather than
    # bar history drives the strategy) starves a rolling-window strategy - a
    # 20-bar channel would sit in warmup forever, silently never trading.
    engine = BacktestEngine(
        bars=list(seed_bars[:-1]) or list(seed_bars),
        calendar=calendar,
        specs=specs,
        strategy=strategy,
        risk=RiskEngine(
            sizer=FixedLotSizer(lots),
            spec_for=None,
            # One instrument, one direction at a time - a CFD strategy that is
            # long or short or flat can never want a second concurrent position.
            max_concurrent_positions=1,
            max_lots_per_underlying=max_lots,
            # No margin cap: the engine's margin models are SPAN approximations
            # for MCX options and mean nothing for a CFD. A cap computed from
            # the wrong model would refuse or permit for the wrong reason.
            margin_cap_pct=None,
        ),
        simulator=simulator,
        portfolio=Portfolio(starting_equity),
        instrument=instrument,
        timeframe=timeframe,
        is_option=False,
        exchange=Exchange.OTC,
        # Not `ist_date` - see the module docstring. An FX bar at 20:00 UTC
        # on Friday is IST Saturday, a day this venue has no session on.
        session_day_for=calendar.session_day_for,
        price_source=BarPriceSource(instrument, list(seed_bars)),
        kill_switch=kill_switch,
        state=state,
        mode="paper",
        broker="paper",
    )

    router = OrderRouter(broker=broker, journal=journal, clock=clock)
    router.reconcile()

    loop = LiveLoop(
        engine=engine,
        bars=bars,  # type: ignore[arg-type]
        fills=BrokerFillFeed(
            broker=broker,
            clock=clock,
            instruments={instrument.key: instrument},
            since=clock.now() - timedelta(days=1),
        ),
        place=lambda orders: [router.place(order) for order in orders],
        clock=clock,
        state=state,
        session_day_for=calendar.session_day_for,
    )
    return Mt5PaperRun(
        loop=loop,
        engine=engine,
        broker=broker,
        journal=journal,
        state=state,
        instrument=instrument,
        strategy=strategy,
    )
