"""Overnight financing on the engine's shared decision path.

`SwapModel` was tested from the day it was written and wired into no engine
path for just as long (D-128 flagged it; `mt5_runner.py`'s docstring carried
the caveat). The consequence was narrow but real: `run_cfd_backtest` charged
carry, `BacktestEngine` did not - so the live loop, `algo backtest` and the
dashboard's backtest console all reported a P&L that ignored the largest cost
of holding a CFD overnight, while the walk-forward charged it. Two paths
disagreeing about what a position costs is the drift `decide` exists to stop.

The properties worth holding:

**One night per session roll, three on the triple day.** Hand-computable, and
asserted exactly rather than within a tolerance.

**A short is credited, not charged.** The sign convention flips exactly once
(`carry_for` returns a P&L contribution, `apply_financing` takes a cost), and a
test that only ever looked at longs would never catch it being flipped twice.

**MCX is untouched.** Margin there is blocked, not borrowed. An engine built
without a `SwapModel` must charge nothing at all, so no MCX result moves.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from algo.backtest.engine import BacktestEngine
from algo.core.bar import M30, Bar
from algo.core.enums import Exchange, Side
from algo.core.fill import Charges
from algo.core.instrument import FutureId, InstrumentSpec
from algo.core.position import Position
from algo.costs.cfd import SwapModel
from algo.costs.charges import ZeroChargeModel
from algo.costs.slippage import NoSlippage
from algo.costs.spread import NoSpread
from algo.data.resample import resample
from algo.data.synthetic import flat_session
from algo.exchange.calendar import MarketCalendar
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.portfolio.book import Portfolio
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.strategy.buy_and_hold import BuyAndHold

GOLDM = FutureId(underlying="GOLDM", expiry=date(2026, 9, 25))
SPEC = InstrumentSpec(
    underlying="GOLDM",
    exchange=Exchange.MCX,
    lot_size=Decimal("100"),
    multiplier=Decimal("10"),
    tick_size=Decimal("0.50"),
    min_lots=1,
    effective_from=date(2026, 1, 1),
    source="financing fixture",
)
START = Decimal("1000000")

#: 80.54 points x 0.01 per point x 1 lot: the nightly cost of one engine lot
#: held long, from the rates measured on the demo account (D-121).
NIGHT_PER_LOT = Decimal("0.8054")

TUESDAY = date(2026, 8, 18)
WEDNESDAY = date(2026, 8, 19)
THURSDAY = date(2026, 8, 20)
FRIDAY = date(2026, 8, 21)


def _engine(
    calendar: MarketCalendar,
    bars: list[Bar],
    *,
    swap: SwapModel | None,
    lots: int = 1,
) -> BacktestEngine:
    return BacktestEngine(
        bars=bars,
        calendar=calendar,
        specs=ContractSpecStore([SPEC]),
        strategy=BuyAndHold(GOLDM),
        risk=RiskEngine(
            sizer=FixedLotSizer(lots),
            spec_for=None,
            max_concurrent_positions=1,
            max_lots_per_underlying=lots,
        ),
        simulator=FillSimulator(
            spread=NoSpread(), slippage=NoSlippage(), charges=ZeroChargeModel()
        ),
        portfolio=Portfolio(START),
        instrument=GOLDM,
        timeframe=M30,
        is_option=False,
        config_hash="test",
        swap=swap,
    )


def _bars(calendar: MarketCalendar, days: list[date]) -> list[Bar]:
    out: list[Bar] = []
    for day in days:
        out.extend(
            resample(
                flat_session(calendar, day, price=Decimal("100000")),
                calendar=calendar,
                timeframe=M30,
            )
        )
    return out


def _hold(engine: BacktestEngine, *, side: Side, lots: int) -> None:
    """Put a position on the book directly.

    The fill machinery is exercised at length elsewhere; what is under test
    here is what financing does to a book that is already holding something.
    """
    signed = Decimal(lots) if side is Side.BUY else -Decimal(lots)
    engine._portfolio._positions[GOLDM.key] = Position(
        instrument=GOLDM,
        lots=int(signed),
        qty=signed,
        cost_basis=signed * Decimal("100000"),
    )


def _charged(engine: BacktestEngine) -> Decimal:
    return engine._portfolio.charges.swap


def _roll(engine: BacktestEngine, *days: date) -> None:
    for day in days:
        engine._charge_financing(day)


class TestOneNightPerSessionRoll:
    def test_the_first_session_charges_nothing(self, calendar: MarketCalendar) -> None:
        """There is no previous session to have held a position across."""
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.BUY, lots=1)
        _roll(engine, THURSDAY)
        assert _charged(engine) == Decimal("0")

    def test_a_roll_charges_exactly_one_night(self, calendar: MarketCalendar) -> None:
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.BUY, lots=1)
        _roll(engine, THURSDAY, FRIDAY)
        assert _charged(engine) == NIGHT_PER_LOT

    def test_the_same_session_seen_twice_charges_once(
        self, calendar: MarketCalendar
    ) -> None:
        """`decide` runs on every bar, not every session. Charging per bar would
        multiply the cost by the number of bars in a day."""
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.BUY, lots=1)
        _roll(engine, THURSDAY, *([FRIDAY] * 20))
        assert _charged(engine) == NIGHT_PER_LOT

    def test_wednesday_charges_three(self, calendar: MarketCalendar) -> None:
        """Spot metal settles T+2, so Wednesday's roll carries the weekend."""
        engine = _engine(
            calendar, _bars(calendar, [TUESDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.BUY, lots=1)
        _roll(engine, TUESDAY, WEDNESDAY)
        assert _charged(engine) == NIGHT_PER_LOT * 3

    def test_it_scales_with_position_size(self, calendar: MarketCalendar) -> None:
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.BUY, lots=100)
        _roll(engine, THURSDAY, FRIDAY)
        assert _charged(engine) == NIGHT_PER_LOT * 100

    def test_a_flat_book_is_charged_nothing(self, calendar: MarketCalendar) -> None:
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _roll(engine, THURSDAY, FRIDAY)
        assert _charged(engine) == Decimal("0")


class TestTheSignConvention:
    def test_a_long_pays(self, calendar: MarketCalendar) -> None:
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.BUY, lots=1)
        _roll(engine, THURSDAY, FRIDAY)
        assert _charged(engine) > 0
        assert engine._portfolio.cash < START

    def test_a_short_is_credited(self, calendar: MarketCalendar) -> None:
        """`carry_for` returns a signed P&L contribution and `apply_financing`
        takes a cost. Flip that twice and a credit silently becomes a charge."""
        engine = _engine(
            calendar, _bars(calendar, [THURSDAY]), swap=SwapModel.vantage_xauusd()
        )
        _hold(engine, side=Side.SELL, lots=1)
        _roll(engine, THURSDAY, FRIDAY)
        assert _charged(engine) == Decimal("-0.3267")
        assert engine._portfolio.cash > START


class TestMcxIsUntouched:
    def test_no_swap_model_charges_nothing(self, calendar: MarketCalendar) -> None:
        """MCX blocks margin rather than lending it. Every result over the MCX
        path must be identical to what it was before financing existed."""
        engine = _engine(calendar, _bars(calendar, [THURSDAY]), swap=None, lots=100)
        _hold(engine, side=Side.BUY, lots=100)
        _roll(engine, THURSDAY, FRIDAY)
        assert _charged(engine) == Decimal("0")
        assert engine._portfolio.cash == START


class TestTheBooksStillBalance:
    def test_financing_preserves_the_accounting_identity(self) -> None:
        """cash + value == start + realised + unrealised - charges. Financing
        moves cash and charges together, so the identity is what proves the
        cost was booked rather than quietly leaking out of equity."""
        book = Portfolio(START)
        book.apply_financing(Decimal("80.54"))
        book.check_identity({})
        assert book.cash == START - Decimal("80.54")
        assert book.charges.swap == Decimal("80.54")

    def test_a_credit_balances_too(self) -> None:
        book = Portfolio(START)
        book.apply_financing(Decimal("-32.67"))
        book.check_identity({})
        assert book.cash == START + Decimal("32.67")


class TestChargesCarriesSwap:
    def test_it_counts_towards_the_total(self) -> None:
        assert Charges(swap=Decimal("80.54")).total == Decimal("80.54")

    def test_it_survives_addition(self) -> None:
        combined = Charges(swap=Decimal("80.54")) + Charges(swap=Decimal("241.62"))
        assert combined.swap == Decimal("322.16")

    def test_a_credit_reduces_the_total(self) -> None:
        """The one component that can be negative - a short receives financing,
        and reporting that as a cost of minus-something is truer than dropping
        a credit the account actually got."""
        assert Charges(
            brokerage=Decimal("10"), swap=Decimal("-32.67")
        ).total == Decimal("-22.67")


class TestItRunsOnTheRealPath:
    """The integration case: financing arriving through `decide` on a real
    `run()`, rather than by calling the hook by hand."""

    def test_a_multi_session_hold_is_charged(self, calendar: MarketCalendar) -> None:
        bars = _bars(calendar, [TUESDAY, WEDNESDAY, THURSDAY])
        result = _engine(calendar, bars, swap=SwapModel.vantage_xauusd()).run()
        # Tuesday -> Wednesday is the triple day, Wednesday -> Thursday one more.
        assert result.total_charges == NIGHT_PER_LOT * 4

    def test_the_same_run_without_a_swap_model_charges_nothing(
        self, calendar: MarketCalendar
    ) -> None:
        bars = _bars(calendar, [TUESDAY, WEDNESDAY, THURSDAY])
        result = _engine(calendar, bars, swap=None).run()
        assert result.total_charges == Decimal("0")
