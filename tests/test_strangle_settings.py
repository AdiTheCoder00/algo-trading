"""The four strategy settings changed together: no stop, a 4% target, a
1000-multiple strike grid, and rolling into the next cycle on the front cycle's
expiry day.

Each is tested at the level it actually acts: strike selection and the roll on
the strategy, the missing stop on the exit levels and on the engine's warning
list, because that is where a silent revert would show up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from algo.core.enums import Exchange, Right
from algo.core.instrument import FutureId
from algo.core.signal import ComboExit
from algo.data.synthetic_chain import build_chain
from algo.risk.exits import ExitLevels, ExitReason, resolve_levels

if TYPE_CHECKING:
    from algo.backtest.engine import BacktestResult
    from algo.core.chain import OptionChainSnapshot
    from algo.strategy.context import BarContext

FUTURE = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX)
TS = datetime(2026, 8, 19, 4, 30, tzinfo=UTC)
EXPIRY = date(2026, 8, 28)
EXPIRES_AT = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)


def _chain(strike_centre: str = "156640", **kwargs: object) -> OptionChainSnapshot:
    return build_chain(
        ts=TS,
        underlying_future=FUTURE,
        option_expiry=EXPIRY,
        futures_price=Decimal(strike_centre),
        expires_at=EXPIRES_AT,
        vol=0.2175,
        strikes_each_side=14,
        strike_centre=Decimal(strike_centre),
        populate_greeks=True,
        **kwargs,  # type: ignore[arg-type]
    )


class TestStrikeMultiple:
    """D-103. A filter on the ladder, never a rounding of the chosen strike."""

    def test_without_it_a_500_strike_can_be_selected(self) -> None:
        chain = _chain()
        picked = {
            chain.nearest_delta(0.25, right, tolerance=0.20)
            for right in (Right.CE, Right.PE)
        }
        assert all(row is not None for row in picked)
        # The fixture ladder is on 500s, so at least one side lands off the
        # thousands - if this ever stops being true the test below proves nothing.
        assert any(row.strike % 1000 != 0 for row in picked if row is not None)

    def test_with_it_every_selection_is_on_the_grid(self) -> None:
        chain = _chain()
        for right in (Right.CE, Right.PE):
            row = chain.nearest_delta(
                0.25, right, tolerance=0.20, strike_multiple=Decimal("1000")
            )
            assert row is not None
            assert row.strike % 1000 == 0

    def test_it_filters_rather_than_rounds(self) -> None:
        """The delta reported must belong to the strike actually chosen.

        Snapping 160500 to 160000 and keeping 160500's delta would report a
        position the account does not hold - the same substitution D-005 forbids.
        """
        chain = _chain()
        row = chain.nearest_delta(
            0.25, Right.CE, tolerance=0.20, strike_multiple=Decimal("1000")
        )
        assert row is not None
        on_chain = next(
            r for r in chain.rows if r.strike == row.strike and r.right is Right.CE
        )
        assert row.delta == on_chain.delta

    def test_no_strike_on_the_grid_within_tolerance_selects_nothing(self) -> None:
        """Better to emit no signal than to fall off the grid quietly."""
        chain = _chain()
        assert (
            chain.nearest_delta(
                0.25, Right.CE, tolerance=0.0001, strike_multiple=Decimal("100000")
            )
            is None
        )


class TestExitLevelsWithoutAStop:
    """D-102."""

    def _levels(self, *, stop: ComboExit | None) -> ExitLevels:
        return resolve_levels(
            take_profit=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("4")),
            stop_loss=stop,
            margin=Decimal("100000"),
            equity=Decimal("1000000"),
            credit=Decimal("2000"),
        )

    def test_the_target_is_four_percent_of_margin(self) -> None:
        assert self._levels(stop=None).take_profit == Decimal("4000")

    def test_no_stop_resolves_to_none_not_to_zero(self) -> None:
        """Zero would read as "exit the moment it is down a rupee" - the exact
        opposite of what disabling the stop means."""
        levels = self._levels(stop=None)

        assert levels.stop_loss is None
        assert levels.check(Decimal("-1")) is None

    def test_an_arbitrarily_large_loss_does_not_exit(self) -> None:
        levels = self._levels(stop=None)

        assert levels.check(Decimal("-999999999")) is None

    def test_the_target_still_fires(self) -> None:
        levels = self._levels(stop=None)

        assert levels.check(Decimal("4000")) is ExitReason.TAKE_PROFIT

    def test_a_configured_stop_still_fires(self) -> None:
        """The capability is disabled, not removed."""
        levels = self._levels(
            stop=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("1"))
        )

        assert levels.stop_loss == Decimal("1000")
        assert levels.check(Decimal("-1000")) is ExitReason.STOP_LOSS


class TestTheStopMustBeDisabledOutLoud:
    """A safety level that could be removed by omission would be removable by
    accident. Only the explicit flag does it."""

    def test_omitting_the_stop_keeps_the_default(self) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(underlying="GOLDM")

        assert strategy._stop_loss is not None
        assert strategy.params()["stop_loss"] == "PCT_OF_MARGIN_AT_ENTRY:1"

    def test_passing_stop_loss_none_keeps_the_default(self) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(underlying="GOLDM", stop_loss=None)

        assert strategy._stop_loss is not None

    def test_only_the_flag_removes_it(self) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(underlying="GOLDM", no_stop=True)

        assert strategy._stop_loss is None
        assert strategy.params()["stop_loss"] == "NONE"


class TestParamsFingerprintTheNewSettings:
    """`params()` feeds the signal id. Two runs differing only in these settings
    must not share a fingerprint, or the run log cannot tell them apart."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"no_stop": True},
            {"strike_multiple": Decimal("1000")},
            {"roll_at_front_dte": 0},
            {"cycle_offset": 1},
        ],
    )
    def test_each_setting_changes_the_hash(self, kwargs: dict[str, object]) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        base = DeltaStrangle(underlying="GOLDM")
        changed = DeltaStrangle(underlying="GOLDM", **kwargs)  # type: ignore[arg-type]

        assert base.params_hash() != changed.params_hash()


class TestTheRoll:
    """D-104: enter on the front cycle's expiry day, selling the cycle after it.

    Exercised through a real `BarContext` rather than by calling the gate
    directly - the interesting part is which cycle comes back from the calendar,
    and a hand-built stub would be free to answer whatever the test wanted.
    """

    AUG = date(2026, 8, 28)
    SEP = date(2026, 9, 25)

    def _ctx(self, on: date) -> BarContext:
        from algo.core.bar import Bar, BarWindow, Timeframe
        from algo.exchange.expiries import (
            ExpiryCalendar,
            ExpirySet,
            InstrumentMasterExpiries,
        )
        from algo.exchange.specs import ContractSpecStore
        from algo.strategy.context import BarContext, PositionView, SessionInfo

        ts = datetime(on.year, on.month, on.day, 4, 0, tzinfo=UTC)
        bar = Bar(
            ts=ts,
            open=Decimal("156640"),
            high=Decimal("156640"),
            low=Decimal("156640"),
            close=Decimal("156640"),
            volume=10,
            timeframe=Timeframe(minutes=30),
        )
        master = InstrumentMasterExpiries(
            {
                ("GOLDM", 2026, 8): ExpirySet(
                    option_expiry=self.AUG, futures_expiry=date(2026, 9, 4)
                ),
                ("GOLDM", 2026, 9): ExpirySet(
                    option_expiry=self.SEP, futures_expiry=date(2026, 10, 5)
                ),
            }
        )
        return BarContext(
            window=BarWindow((bar,)),
            session=SessionInfo(
                session_date=on,
                is_us_dst=True,
                minutes_to_close=600,
                is_partial_bar=False,
                bar_index=0,
                bars_in_session=29,
            ),
            specs=ContractSpecStore.default(),
            positions=PositionView({}),
            timeframe=Timeframe(minutes=30),
            expiries=ExpiryCalendar(authority=master, rule=None),
        )

    def test_expiry_after_returns_the_next_listed_cycle(self) -> None:
        ctx = self._ctx(date(2026, 8, 19))
        front = ctx.nearest_expiry("GOLDM")

        assert front.option_expiry == self.AUG
        assert ctx.expiry_after("GOLDM", front).option_expiry == self.SEP

    def test_on_front_expiry_day_the_front_cycle_is_still_the_august_one(self) -> None:
        """The premise of the roll: `nearest_expiry` returns a cycle expiring
        *on or after* today, so on expiry day itself it is still that cycle at
        zero DTE - not next month's."""
        ctx = self._ctx(self.AUG)
        front = ctx.nearest_expiry("GOLDM")

        assert front.option_expiry == self.AUG
        assert front.days_to_option_expiry(self.AUG) == 0

    def test_the_gate_is_shut_before_expiry_day(self) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(
            underlying="GOLDM", roll_at_front_dte=0, cycle_offset=1
        )
        # 9 days out: nothing should be emitted regardless of what the chain holds.
        assert strategy.on_bar(self._ctx(date(2026, 8, 19))) == []

    def test_the_cycle_sold_is_the_one_after_the_front(self) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(
            underlying="GOLDM", roll_at_front_dte=0, cycle_offset=1
        )
        ctx = self._ctx(self.AUG)

        assert strategy._cycle_to_trade(ctx, ctx.nearest_expiry("GOLDM")).option_expiry == self.SEP

    def test_offset_zero_keeps_the_front_cycle(self) -> None:
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(underlying="GOLDM", cycle_offset=0)
        ctx = self._ctx(self.AUG)

        assert strategy._cycle_to_trade(ctx, ctx.nearest_expiry("GOLDM")).option_expiry == self.AUG

    def test_the_rolled_cycle_lands_inside_the_dte_band(self) -> None:
        """The roll would be pointless if the cycle it selects were then refused
        by min_dte/max_dte. September's expiry is 28 days past August's."""
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(underlying="GOLDM", roll_at_front_dte=0, cycle_offset=1)
        ctx = self._ctx(self.AUG)
        cycle = strategy._cycle_to_trade(ctx, ctx.nearest_expiry("GOLDM"))
        dte = cycle.days_to_option_expiry(self.AUG)

        assert dte == 28
        assert strategy._min_dte <= dte <= strategy._max_dte

    def test_an_unlisted_next_cycle_is_a_note_not_a_crash(self) -> None:
        """Near the end of the master's horizon there is no cycle after the
        front one. That is a real state, not a bug."""
        from algo.strategy.delta_strangle import DeltaStrangle

        strategy = DeltaStrangle(underlying="GOLDM", roll_at_front_dte=0, cycle_offset=1)
        ctx = self._ctx(self.SEP)  # September is the last cycle the master lists

        assert strategy.on_bar(ctx) == []


class TestTheMissingStopIsAnnounced:
    """The run must say it has no loss exit, every time, in the same place the
    other uncalibrated-model warnings appear. A silent run without a stop is the
    failure mode this whole warning list exists to prevent.
    """

    def _result(self, *, no_stop: bool) -> BacktestResult:
        from algo.backtest.engine import BacktestEngine
        from algo.backtest.prices import ChainFeedProvider, ChainPriceSource, CompositePriceSource
        from algo.core.bar import Timeframe
        from algo.costs.charges import McxChargeModel
        from algo.costs.slippage import TickSlippage
        from algo.costs.spread import FixedTickSpread
        from algo.data.resample import resample
        from algo.data.synthetic import one_minute_session
        from algo.exchange.calendar import synthetic_calendar
        from algo.exchange.expiries import (
            ExpiryCalendar,
            ExpirySet,
            InstrumentMasterExpiries,
        )
        from algo.exchange.specs import ContractSpecStore
        from algo.execution.fills import FillSimulator
        from algo.portfolio.book import Portfolio
        from algo.risk.engine import FixedLotSizer, RiskEngine
        from algo.strategy.delta_strangle import DeltaStrangle

        calendar = synthetic_calendar()
        bars = resample(
            one_minute_session(calendar, date(2026, 8, 19), seed=20260819),
            calendar=calendar,
            timeframe=Timeframe(minutes=30),
        )
        chains = [
            build_chain(
                ts=bar.ts,
                underlying_future=FUTURE,
                option_expiry=EXPIRY,
                futures_price=bar.close,
                expires_at=calendar.session_close(EXPIRY),
                vol=0.2175,
                strikes_each_side=14,
                strike_centre=Decimal("156640"),
                populate_greeks=True,
            )
            for bar in bars
        ]
        master = InstrumentMasterExpiries(
            {
                ("GOLDM", 2026, 8): ExpirySet(
                    option_expiry=EXPIRY, futures_expiry=date(2026, 9, 4)
                )
            }
        )
        engine = BacktestEngine(
            bars=bars,
            calendar=calendar,
            specs=ContractSpecStore.default(),
            strategy=DeltaStrangle(
                underlying="GOLDM", min_dte=3, max_dte=45, no_stop=no_stop
            ),
            risk=RiskEngine(
                sizer=FixedLotSizer(1),
                spec_for=None,
                max_concurrent_positions=2,
                max_lots_per_underlying=10,
            ),
            simulator=FillSimulator(
                spread=FixedTickSpread(2),
                slippage=TickSlippage(market_ticks=0, stop_ticks=2),
                charges=McxChargeModel.default(),
            ),
            portfolio=Portfolio(Decimal("1000000")),
            instrument=FUTURE,
            timeframe=Timeframe(minutes=30),
            is_option=True,
            price_source=CompositePriceSource(ChainPriceSource(chains)),
            chain_provider=ChainFeedProvider(chains),
            expiries=ExpiryCalendar(authority=master, rule=None),
        )
        return engine.run()

    def test_a_run_without_a_stop_says_so(self) -> None:
        warnings = " ".join(self._result(no_stop=True).warnings)

        assert "NO STOP LOSS IS CONFIGURED" in warnings

    def test_a_run_with_a_stop_does_not(self) -> None:
        """The other half - otherwise a warning that is always present would
        pass this test while telling an operator nothing."""
        warnings = " ".join(self._result(no_stop=False).warnings)

        assert "NO STOP LOSS IS CONFIGURED" not in warnings
