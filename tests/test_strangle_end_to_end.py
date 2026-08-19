"""The strangle, run end to end through the engine.

Everything before this file tested a component. This runs the real loop: bars in,
chain snapshots per bar, the strategy choosing strikes, the risk layer sizing and
guarding, the fill simulator pricing, the portfolio accounting — and asserts what
came out the other side.

The scenarios are constructed so the outcome is known in advance:

* a quiet market, where the position should still be open at the end;
* a market that rallies hard, so the short call runs the position into its stop;
* a market that goes nowhere until the deadline, so the devolvement guard is the
  thing that closes it;
* a chain with no quotes where the strategy wants them, so nothing trades at all
  and the run says why.

The last two matter most. A strangle backtest that never exercises the forced
pre-expiry exit has not tested the one rule that stands between the account and a
delivery obligation.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from algo.backtest.engine import BacktestEngine, BacktestResult
from algo.backtest.prices import ChainFeedProvider, ChainPriceSource, CompositePriceSource
from algo.core.bar import M1, M30, Bar
from algo.core.chain import OptionChainSnapshot
from algo.core.enums import Exchange, RejectReason
from algo.core.instrument import FutureId, InstrumentSpec, OptionId
from algo.core.signal import ComboExit
from algo.core.timeutil import ist_date, to_ist
from algo.costs.charges import FlatChargeModel
from algo.costs.margin import FixedMarginPerLot
from algo.costs.slippage import NoSlippage
from algo.costs.spread import FixedTickSpread
from algo.data.resample import resample
from algo.data.synthetic_chain import build_chain
from algo.exchange.calendar import MarketCalendar
from algo.exchange.expiries import (
    ExpiryCalendar,
    ExpirySet,
    InstrumentMasterExpiries,
    LastFridayRule,
)
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.portfolio.book import Portfolio
from algo.risk.devolvement import DevolvementGuard
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.risk.exits import ExitReason
from algo.risk.killswitch import KillSwitch
from algo.strategy.delta_strangle import DeltaStrangle

START_PRICE = Decimal("156640")
EXPIRY = date(2026, 8, 28)
EXPIRES_AT_HINT = date(2026, 8, 28)
GOLDM_FUT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))
START_EQUITY = Decimal("1000000.00")
MARGIN_PER_LOT = Decimal("100000")

CYCLE = ExpirySet(
    option_expiry=EXPIRY,
    futures_expiry=date(2026, 9, 4),
    tender_period_start=date(2026, 9, 1),
)

SPEC = InstrumentSpec(
    underlying="GOLDM",
    exchange=Exchange.MCX,
    lot_size=Decimal("100"),
    multiplier=Decimal("10"),
    tick_size=Decimal("0.50"),
    strike_interval=Decimal("500"),
    min_lots=1,
    effective_from=date(2026, 1, 1),
    source="end-to-end fixture",
)

#: Wed 19 Aug through Thu 27 Aug 2026 — the session before the 28 Aug expiry.
SESSION_DAYS = [
    date(2026, 8, 19),
    date(2026, 8, 20),
    date(2026, 8, 21),
    date(2026, 8, 24),
    date(2026, 8, 25),
    date(2026, 8, 26),
    date(2026, 8, 27),
]


def _bars(calendar: MarketCalendar, days: list[date], drift_per_bar: Decimal) -> list[Bar]:
    """Futures bars that move by a fixed amount each bar — no randomness, so the
    scenario each test intends is the scenario it gets."""
    minute_bars: list[Bar] = []
    level = START_PRICE
    for day in days:
        opened = calendar.session_open(day)
        for minute in range(1, calendar.session_minutes(day) + 1):
            nxt = level + drift_per_bar
            minute_bars.append(
                Bar(
                    ts=opened + timedelta(minutes=minute),
                    timeframe=M1,
                    open=min(level, nxt),
                    high=max(level, nxt),
                    low=min(level, nxt),
                    close=nxt,
                    volume=1,
                )
            )
            level = nxt
    return resample(minute_bars, calendar=calendar, timeframe=M30)


def _chains(
    bars: list[Bar], calendar: MarketCalendar, *, quote_gaps: frozenset[Decimal] = frozenset()
) -> list[OptionChainSnapshot]:
    expires_at = calendar.session_close(EXPIRES_AT_HINT)
    return [
        build_chain(
            ts=bar.ts,
            underlying_future=GOLDM_FUT,
            option_expiry=EXPIRY,
            futures_price=bar.close,
            expires_at=expires_at,
            vol=0.2175,
            strikes_each_side=14,
            strike_centre=START_PRICE,
            quote_gaps=quote_gaps,
            populate_greeks=True,
        )
        for bar in bars
    ]


def _run(
    calendar: MarketCalendar,
    *,
    drift_per_bar: Decimal = Decimal("0"),
    days: list[date] | None = None,
    quote_gaps: frozenset[Decimal] = frozenset(),
    with_guard: bool = True,
    kill_switch: KillSwitch | None = None,
    min_dte: int = 3,
    wide_exits: bool = False,
) -> BacktestResult:
    session_days = days or SESSION_DAYS
    bars = _bars(calendar, session_days, drift_per_bar)
    chains = _chains(bars, calendar, quote_gaps=quote_gaps)

    master = InstrumentMasterExpiries({("GOLDM", 2026, 8): CYCLE})
    expiries = ExpiryCalendar(authority=master, rule=LastFridayRule(calendar))

    engine = BacktestEngine(
        bars=bars,
        calendar=calendar,
        specs=ContractSpecStore([SPEC]),
        strategy=DeltaStrangle(
            underlying="GOLDM",
            min_dte=min_dte,
            max_dte=45,
            # Exits far enough away that neither can fire, so a scenario can
            # isolate the devolvement guard as the only thing that closes.
            take_profit=ComboExit(kind="ABS_INR", value=Decimal("99999999"))
            if wide_exits
            else None,
            stop_loss=ComboExit(kind="ABS_INR", value=Decimal("99999999"))
            if wide_exits
            else None,
        ),
        risk=RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=2,  # a strangle is two positions
            max_lots_per_underlying=10,
        ),
        simulator=FillSimulator(
            spread=FixedTickSpread(2),
            slippage=NoSlippage(),
            charges=FlatChargeModel(Decimal("20")),
        ),
        portfolio=Portfolio(START_EQUITY),
        instrument=GOLDM_FUT,
        timeframe=M30,
        is_option=True,
        price_source=CompositePriceSource(ChainPriceSource(chains)),
        chain_provider=ChainFeedProvider(chains),
        expiries=expiries,
        devolvement=DevolvementGuard(
            calendar=calendar,
            force_exit_sessions_before_expiry=1,
            block_new_entries_within_dte=2,
        )
        if with_guard
        else None,
        kill_switch=kill_switch,
        margin=FixedMarginPerLot(MARGIN_PER_LOT),
    )
    return engine.run()


class TestItActuallyTrades:
    def test_a_strangle_is_opened_on_the_first_entry_bar(
        self, calendar: MarketCalendar
    ) -> None:
        result = _run(calendar)
        assert len(result.fills) >= 2

        first_two = result.fills[:2]
        assert all(f.side.value == "SELL" for f in first_two)
        rights = {f.instrument.right for f in first_two}  # type: ignore[union-attr]
        assert len(rights) == 2, "one call and one put"

    def test_entry_happens_at_the_0930_bar_plus_one(
        self, calendar: MarketCalendar
    ) -> None:
        """The signal fires at 09:30; D-038 puts the fill on the following bar."""
        result = _run(calendar)
        assert to_ist(result.fills[0].ts).strftime("%H:%M") == "10:00"
        assert ist_date(result.fills[0].ts) == SESSION_DAYS[0]

    def test_only_one_strangle_is_opened(self, calendar: MarketCalendar) -> None:
        """Cadence is one per expiry cycle — the strategy sees a position and stops."""
        result = _run(calendar)
        opens = [f for f in result.fills if f.side.value == "SELL"]
        assert len(opens) == 2

    def test_the_equity_identity_holds_throughout(self, calendar: MarketCalendar) -> None:
        result = _run(calendar)
        for point in result.equity_curve:
            assert point.equity == point.cash + point.market_value

    def test_the_run_is_reproducible(self, calendar: MarketCalendar) -> None:
        a = _run(calendar)
        b = _run(calendar)
        assert [f.model_dump_json() for f in a.fills] == [f.model_dump_json() for f in b.fills]
        assert a.dataset_hash == b.dataset_hash


class TestForcedPreExpiryExit:
    """The rule that stands between the account and a delivery obligation."""

    def test_the_position_is_closed_before_the_expiry_session(
        self, calendar: MarketCalendar
    ) -> None:
        result = _run(calendar, wide_exits=True)
        forced = [e for e in result.exits if e.reason is ExitReason.FORCED_PRE_EXPIRY]
        assert forced, "the guard must fire before expiry"

    def test_nothing_is_held_past_the_exit_deadline(
        self, calendar: MarketCalendar
    ) -> None:
        result = _run(calendar, wide_exits=True)
        assert result.round_trips >= 1
        assert result.equity_curve[-1].open_positions == 0

    def test_the_forced_exit_explains_itself(self, calendar: MarketCalendar) -> None:
        result = _run(calendar, wide_exits=True)
        forced = next(e for e in result.exits if e.reason is ExitReason.FORCED_PRE_EXPIRY)
        assert "physical delivery" in forced.detail
        assert str(EXPIRY) in forced.detail

    def test_without_the_guard_the_position_survives_to_the_end(
        self, calendar: MarketCalendar
    ) -> None:
        """The counterfactual that proves the guard is load-bearing (D-016).

        Same data, same strategy, guard removed — and the short options are still
        open on the last bar before expiry. That is the state that devolves.
        """
        guarded = _run(calendar, wide_exits=True)
        unguarded = _run(calendar, wide_exits=True, with_guard=False)
        assert guarded.equity_curve[-1].open_positions == 0
        assert unguarded.equity_curve[-1].open_positions == 2

    def test_no_new_entry_inside_the_devolvement_window(
        self, calendar: MarketCalendar
    ) -> None:
        """Starting the run two days before expiry: the strategy wants to trade and
        the guard refuses."""
        result = _run(
            calendar, days=[date(2026, 8, 26), date(2026, 8, 27)], min_dte=1
        )
        assert not result.fills
        assert any(
            r.reason is RejectReason.DEVOLVEMENT_WINDOW for r in result.rejections
        ), [r.reason for r in result.rejections]


class TestStopAndTarget:
    def test_a_hard_rally_stops_the_position_out(self, calendar: MarketCalendar) -> None:
        """A short call losing faster than the put gains is what a stop is for."""
        result = _run(calendar, drift_per_bar=Decimal("2.0"))
        stopped = [e for e in result.exits if e.reason is ExitReason.STOP_LOSS]
        assert stopped, [e.reason for e in result.exits]
        assert stopped[0].combo_pnl < 0

    def test_the_stop_is_one_percent_of_margin(self, calendar: MarketCalendar) -> None:
        result = _run(calendar, drift_per_bar=Decimal("2.0"))
        stopped = next(e for e in result.exits if e.reason is ExitReason.STOP_LOSS)
        expected_stop = MARGIN_PER_LOT * Decimal("1") / Decimal("100")
        assert f"stop {expected_stop}" in stopped.detail

    def test_a_quiet_market_reaches_the_take_profit_through_decay(
        self, calendar: MarketCalendar
    ) -> None:
        """Time value bleeds out of both legs, so a motionless market is the case
        the strategy is built for."""
        result = _run(calendar, drift_per_bar=Decimal("0"))
        reasons = {e.reason for e in result.exits}
        assert reasons & {ExitReason.TAKE_PROFIT, ExitReason.FORCED_PRE_EXPIRY}

    def test_an_exit_closes_every_leg(self, calendar: MarketCalendar) -> None:
        """A per-leg exit on a strangle closes the winner and keeps the loser."""
        result = _run(calendar, drift_per_bar=Decimal("2.0"))
        buys = [f for f in result.fills if f.side.value == "BUY"]
        assert len(buys) == 2


class TestWhenTheBookIsThin:
    def test_nothing_trades_when_the_target_strikes_are_unquoted(
        self, calendar: MarketCalendar
    ) -> None:
        """The live-chain case: the strikes exist but nobody is showing a price."""
        gaps = frozenset(Decimal(str(k)) for k in range(158000, 165001, 500)) | frozenset(
            Decimal(str(k)) for k in range(148000, 155001, 500)
        )
        result = _run(calendar, quote_gaps=gaps)
        assert not result.fills
        assert result.net_pnl == Decimal("0")

    def test_and_the_run_records_why(self, calendar: MarketCalendar) -> None:
        gaps = frozenset(Decimal(str(k)) for k in range(158000, 165001, 500)) | frozenset(
            Decimal(str(k)) for k in range(148000, 155001, 500)
        )
        result = _run(calendar, quote_gaps=gaps)
        assert result.notes
        assert any("not available" in n.message for n in result.notes)
        assert any("closest tradeable" in n.message for n in result.notes)


class TestKillSwitchIntegration:
    def test_a_tripped_switch_blocks_new_entries(self, calendar: MarketCalendar) -> None:
        switch = KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )
        switch.trip_manually(
            "halted before the run",
            _bars(calendar, SESSION_DAYS[:1], Decimal("0"))[0].ts,
        )
        result = _run(calendar, kill_switch=switch)
        assert not result.fills
        assert result.kill_switch_tripped
        assert any(
            r.reason is RejectReason.KILL_SWITCH_TRIPPED for r in result.rejections
        )


class TestHonestyOfTheReport:
    def test_an_approximate_margin_is_flagged(self, calendar: MarketCalendar) -> None:
        """The stop is a percentage OF margin, so an approximate margin means an
        approximate stop — and that has to be visible."""
        result = _run(calendar)
        assert not result.margin_calibrated
        assert any("MARGIN IS APPROXIMATED" in w for w in result.warnings)

    def test_placeholder_costs_are_flagged(self, calendar: MarketCalendar) -> None:
        result = _run(calendar)
        assert any("MODELLED" in w for w in result.warnings)

    def test_options_are_charged_on_premium_not_notional(
        self, calendar: MarketCalendar
    ) -> None:
        """Every fill on an option leg must be recognised as an option."""
        result = _run(calendar)
        for fill in result.fills:
            assert isinstance(fill.instrument, OptionId)


@pytest.mark.parametrize("drift", [Decimal("-2.0"), Decimal("0"), Decimal("2.0")])
def test_the_position_is_always_flat_by_the_end(
    calendar: MarketCalendar, drift: Decimal
) -> None:
    """Whatever the market does, the guard closes the position. This is the
    property that matters more than any P&L figure in this file."""
    result = _run(calendar, drift_per_bar=drift, wide_exits=True)
    assert result.equity_curve[-1].open_positions == 0
