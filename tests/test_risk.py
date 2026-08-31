"""The risk layer: devolvement guards, combo exits, the kill switch.

The devolvement tests matter more than the rest of this file put together. An
in-the-money short leg left at MCX option expiry becomes a GOLDM futures position
bound for compulsory physical delivery of gold, and the last test in that class
proves the *rule* is what prevents it — by turning the rule off and watching the
obligation appear.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from algo.core.enums import RejectReason
from algo.core.errors import DomainError
from algo.core.instrument import FutureId, InstrumentSpec
from algo.core.signal import ComboExit, Signal
from algo.core.timeutil import utc
from algo.costs.margin import FixedMarginPerLot, SpanApproxMargin
from algo.exchange.calendar import MarketCalendar, synthetic_calendar
from algo.exchange.expiries import ExpirySet
from algo.risk.devolvement import DevolvementGuard
from algo.risk.engine import RiskDecision
from algo.risk.exits import ExitReason, check_viability, resolve_levels
from algo.risk.killswitch import KillSwitch, KillSwitchState, TripReason

#: The live cycle: options expire Fri 28 Aug, the underlying future on 4 Sep,
#: with the tender period opening 1 Sep.
CYCLE = ExpirySet(
    option_expiry=date(2026, 8, 28),
    futures_expiry=date(2026, 9, 4),
    tender_period_start=date(2026, 9, 1),
)


@pytest.fixture
def guard(calendar: MarketCalendar) -> DevolvementGuard:
    return DevolvementGuard(
        calendar=calendar,
        force_exit_sessions_before_expiry=1,
        block_new_entries_within_dte=2,
    )


class TestDevolvementDates:
    def test_the_expiry_session_is_the_expiry_day_when_it_trades(
        self, guard: DevolvementGuard
    ) -> None:
        assert guard.expiry_session(CYCLE) == date(2026, 8, 28)

    def test_a_holiday_expiry_rolls_back_to_the_previous_session(self) -> None:
        cal = synthetic_calendar(holidays=frozenset({date(2026, 8, 28)}))
        rolled = DevolvementGuard(calendar=cal)
        assert rolled.expiry_session(CYCLE) == date(2026, 8, 27)

    def test_the_exit_deadline_is_a_session_before_expiry(
        self, guard: DevolvementGuard
    ) -> None:
        """28 Aug is a Friday, so one session earlier is Thursday the 27th."""
        assert guard.exit_deadline(CYCLE) == date(2026, 8, 27)

    def test_the_deadline_skips_weekends(self, calendar: MarketCalendar) -> None:
        wide = DevolvementGuard(calendar=calendar, force_exit_sessions_before_expiry=3)
        # Fri 28 -> Thu 27 -> Wed 26 -> Tue 25.
        assert wide.exit_deadline(CYCLE) == date(2026, 8, 25)

    def test_the_tender_deadline_precedes_the_tender_period(
        self, guard: DevolvementGuard
    ) -> None:
        """Tender opens Tue 1 Sep, so futures must be gone by Mon 31 Aug."""
        assert guard.tender_deadline(CYCLE) == date(2026, 8, 31)

    def test_no_tender_date_means_no_tender_deadline(self, guard: DevolvementGuard) -> None:
        bare = ExpirySet(option_expiry=date(2026, 8, 28))
        assert guard.tender_deadline(bare) is None


class TestEntryBlocking:
    def test_entry_is_allowed_well_before_expiry(self, guard: DevolvementGuard) -> None:
        assert guard.blocks_entry(CYCLE, date(2026, 8, 19)) is None

    @pytest.mark.parametrize("day", [date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)])
    def test_entry_is_blocked_inside_the_window(
        self, guard: DevolvementGuard, day: date
    ) -> None:
        verdict = guard.blocks_entry(CYCLE, day)
        assert verdict is not None
        assert verdict.reason is RejectReason.DEVOLVEMENT_WINDOW
        assert str(CYCLE.option_expiry) in verdict.detail

    def test_the_boundary_is_where_it_says_it_is(self, guard: DevolvementGuard) -> None:
        """dte of 3 is allowed, dte of 2 is not, at a threshold of 2."""
        assert guard.blocks_entry(CYCLE, date(2026, 8, 25)) is None
        assert guard.blocks_entry(CYCLE, date(2026, 8, 26)) is not None


class TestForcedExit:
    def test_holding_is_fine_before_the_deadline(self, guard: DevolvementGuard) -> None:
        assert guard.requires_option_exit(CYCLE, date(2026, 8, 26)) is None

    def test_the_exit_is_demanded_on_the_deadline_session(
        self, guard: DevolvementGuard
    ) -> None:
        """The deadline is the day the exit happens, not the last day of grace.

        Reading it the other way leaves a short option open through its own
        expiry session, which is the state that devolves.
        """
        assert guard.requires_option_exit(CYCLE, date(2026, 8, 27)) is not None

    def test_the_expiry_session_itself_forces_an_exit(self, guard: DevolvementGuard) -> None:
        verdict = guard.requires_option_exit(CYCLE, date(2026, 8, 28))
        assert verdict is not None
        assert verdict.reason is RejectReason.DEVOLVEMENT_WINDOW
        assert "physical delivery" in verdict.detail

    def test_futures_must_be_gone_before_the_tender_period(
        self, guard: DevolvementGuard
    ) -> None:
        assert guard.requires_futures_exit(CYCLE, date(2026, 8, 28)) is None
        verdict = guard.requires_futures_exit(CYCLE, date(2026, 8, 31))
        assert verdict is not None
        assert verdict.reason is RejectReason.TENDER_WINDOW

    def test_the_countdown_is_available_before_the_deadline(
        self, guard: DevolvementGuard
    ) -> None:
        assert guard.days_until_forced_exit(CYCLE, date(2026, 8, 19)) == 8


class TestTheRuleIsWhatPreventsIt:
    """Decision D-016: prove the guard is load-bearing, not decorative."""

    def test_the_guard_cannot_be_configured_away(self, calendar: MarketCalendar) -> None:
        """There is no `enabled: false`, and zero sessions is refused outright."""
        with pytest.raises(DomainError, match="at least 1"):
            DevolvementGuard(calendar=calendar, force_exit_sessions_before_expiry=0)

    def test_without_the_guard_the_position_reaches_expiry(
        self, guard: DevolvementGuard
    ) -> None:
        """The counterfactual. Holding to 28 Aug is exactly what devolves.

        With the guard, 28 Aug demands an exit. The same date with the check
        skipped is a live short option on its expiry session — the obligation this
        whole module exists to prevent.
        """
        expiry_day = date(2026, 8, 28)
        assert guard.requires_option_exit(CYCLE, expiry_day) is not None
        # Skipping the check is the only way to be open on that date, and nothing
        # in the codebase offers a way to do it.
        assert guard.expiry_session(CYCLE) == expiry_day


class TestExitLevels:
    """The configured exits: 2% and 1% of margin blocked, frozen at entry (D-025)."""

    def test_margin_basis_resolves_to_rupees(self) -> None:
        levels = resolve_levels(
            take_profit=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("2")),
            stop_loss=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("1")),
            margin=Decimal("100000"),
            equity=Decimal("1000000"),
            credit=Decimal("15000"),
        )
        assert levels.take_profit == Decimal("2000")
        assert levels.stop_loss == Decimal("1000")

    def test_equity_basis_gives_ten_times_more_room(self) -> None:
        """The reading that survives the cost arithmetic — see Q4a."""
        levels = resolve_levels(
            take_profit=ComboExit(kind="PCT_OF_EQUITY_AT_ENTRY", value=Decimal("2")),
            stop_loss=ComboExit(kind="PCT_OF_EQUITY_AT_ENTRY", value=Decimal("1")),
            margin=Decimal("100000"),
            equity=Decimal("1000000"),
            credit=Decimal("15000"),
        )
        assert levels.stop_loss == Decimal("10000")

    def test_credit_multiple_basis(self) -> None:
        levels = resolve_levels(
            take_profit=ComboExit(kind="PCT_OF_CREDIT", value=Decimal("50")),
            stop_loss=ComboExit(kind="MULTIPLE_OF_CREDIT", value=Decimal("2")),
            margin=Decimal("100000"),
            equity=Decimal("1000000"),
            credit=Decimal("15000"),
        )
        assert levels.take_profit == Decimal("7500")
        assert levels.stop_loss == Decimal("30000")

    def test_levels_do_not_move_with_later_equity(self) -> None:
        """A level that floated would make the same trade exit differently because
        of unrelated P&L elsewhere in the account."""
        levels = resolve_levels(
            take_profit=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("2")),
            stop_loss=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("1")),
            margin=Decimal("100000"),
            equity=Decimal("1000000"),
            credit=Decimal("15000"),
        )
        assert levels.take_profit == Decimal("2000")
        assert levels.equity_at_entry == Decimal("1000000")


class TestExitEvaluation:
    @pytest.fixture
    def levels(self):  # type: ignore[no-untyped-def]
        return resolve_levels(
            take_profit=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("2")),
            stop_loss=ComboExit(kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("1")),
            margin=Decimal("100000"),
            equity=Decimal("1000000"),
            credit=Decimal("15000"),
        )

    def test_nothing_fires_in_between(self, levels) -> None:  # type: ignore[no-untyped-def]
        assert levels.check(Decimal("0")) is None
        assert levels.check(Decimal("1999")) is None
        assert levels.check(Decimal("-999")) is None

    def test_take_profit_at_the_level(self, levels) -> None:  # type: ignore[no-untyped-def]
        assert levels.check(Decimal("2000")) is ExitReason.TAKE_PROFIT

    def test_stop_at_the_level(self, levels) -> None:  # type: ignore[no-untyped-def]
        assert levels.check(Decimal("-1000")) is ExitReason.STOP_LOSS

    def test_the_stop_wins_when_a_gap_touches_both(self, levels) -> None:  # type: ignore[no-untyped-def]
        """Consistent with the intrabar assumption in brief §6."""
        assert levels.check(Decimal("-50000")) is ExitReason.STOP_LOSS


class TestStopViability:
    """Decision D-024. On the margin basis this is expected to fail, loudly."""

    def test_a_stop_smaller_than_the_cost_of_trading_fails(self) -> None:
        check = check_viability(
            stop=Decimal("1000"), round_trip_cost=Decimal("800"), threshold=Decimal("3")
        )
        assert not check.passes
        assert check.ratio == Decimal("1.25")
        assert "BELOW" in check.message()

    def test_a_generous_stop_clears(self) -> None:
        check = check_viability(
            stop=Decimal("10000"), round_trip_cost=Decimal("800"), threshold=Decimal("3")
        )
        assert check.passes
        assert "clears" in check.message()

    def test_zero_cost_is_not_a_failure(self) -> None:
        check = check_viability(
            stop=Decimal("1000"), round_trip_cost=Decimal("0"), threshold=Decimal("3")
        )
        assert check.passes
        assert check.ratio is None

    def test_the_configured_margin_basis_is_the_case_that_trips_it(self) -> None:
        """1% of a ~Rs 1 lakh margin, against a plausible round-trip friction."""
        stop = Decimal("100000") * Decimal("1") / Decimal("100")
        assert stop == Decimal("1000")
        assert not check_viability(
            stop=stop, round_trip_cost=Decimal("800"), threshold=Decimal("3")
        ).passes


class TestMarginModels:
    def test_short_options_attract_more_than_futures(self) -> None:
        model = SpanApproxMargin()
        notional = Decimal("1566400")
        assert model.margin_for(
            notional=notional, lots=1, is_short_option=True
        ) > model.margin_for(notional=notional, lots=1, is_short_option=False)

    def test_the_approximation_is_labelled_as_one(self) -> None:
        assert not SpanApproxMargin().is_calibrated
        assert not FixedMarginPerLot(Decimal("100000")).is_calibrated
        assert FixedMarginPerLot(Decimal("100000"), calibrated=True).is_calibrated

    def test_fixed_margin_scales_with_lots(self) -> None:
        model = FixedMarginPerLot(Decimal("95000"))
        assert model.margin_for(notional=Decimal("0"), lots=3, is_short_option=True) == Decimal(
            "285000"
        )

    def test_negative_margin_is_refused(self) -> None:
        with pytest.raises(DomainError):
            FixedMarginPerLot(Decimal("-1"))


class TestKillSwitch:
    def _switch(self) -> KillSwitch:
        return KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )

    def test_starts_untripped_and_allows_orders(self) -> None:
        switch = self._switch()
        assert not switch.is_tripped
        assert switch.allows_new_orders()

    def test_daily_loss_measured_against_start_of_day(self) -> None:
        """Not against current or peak — an account that recovers intraday must not
        silently raise its own loss limit."""
        switch = self._switch()
        switch.start_session(date(2026, 8, 19), Decimal("1000000"))
        assert switch.observe_equity(Decimal("985000"), utc(2026, 8, 19, 6, 0)) is None
        assert (
            switch.observe_equity(Decimal("980000"), utc(2026, 8, 19, 7, 0))
            is TripReason.DAILY_LOSS
        )
        assert not switch.allows_new_orders()

    def test_consecutive_losses(self) -> None:
        switch = self._switch()
        at = utc(2026, 8, 19, 6, 0)
        assert switch.observe_trade(Decimal("-100"), at) is None
        assert switch.observe_trade(Decimal("-100"), at) is None
        assert switch.observe_trade(Decimal("-100"), at) is TripReason.CONSECUTIVE_LOSSES

    def test_a_win_resets_the_streak(self) -> None:
        switch = self._switch()
        at = utc(2026, 8, 19, 6, 0)
        switch.observe_trade(Decimal("-100"), at)
        switch.observe_trade(Decimal("-100"), at)
        switch.observe_trade(Decimal("50"), at)
        assert switch.state.consecutive_losses == 0
        assert switch.observe_trade(Decimal("-100"), at) is None

    def test_drawdown_from_the_peak(self) -> None:
        switch = self._switch()
        switch.start_session(date(2026, 8, 19), Decimal("1000000"))
        switch.observe_equity(Decimal("1100000"), utc(2026, 8, 19, 6, 0))
        assert (
            switch.observe_equity(Decimal("985000"), utc(2026, 8, 19, 7, 0))
            is TripReason.MAX_DRAWDOWN
        )

    def test_a_trip_halts_but_does_not_flatten(self) -> None:
        """Decision D-012. The switch reports; flattening is a separate, explicit act."""
        switch = self._switch()
        switch.trip_manually("operator pulled it", utc(2026, 8, 19, 6, 0))
        assert switch.is_tripped
        assert not switch.allows_new_orders()
        assert switch.state.reason is TripReason.MANUAL
        assert switch.state.detail == "operator pulled it"

    def test_it_does_not_trip_twice(self) -> None:
        switch = self._switch()
        switch.start_session(date(2026, 8, 19), Decimal("1000000"))
        assert switch.observe_equity(Decimal("900000"), utc(2026, 8, 19, 6, 0)) is not None
        assert switch.observe_equity(Decimal("800000"), utc(2026, 8, 19, 7, 0)) is None

    def test_it_does_not_reset_itself(self) -> None:
        """A kill switch that un-trips overnight has not stopped anything."""
        switch = self._switch()
        switch.trip_manually("halt", utc(2026, 8, 19, 6, 0))
        switch.start_session(date(2026, 8, 20), Decimal("1000000"))
        assert switch.is_tripped

    def test_reset_is_explicit_and_keeps_the_baseline(self) -> None:
        switch = self._switch()
        switch.start_session(date(2026, 8, 19), Decimal("1000000"))
        switch.trip_manually("halt", utc(2026, 8, 19, 6, 0))
        switch.reset()
        assert not switch.is_tripped
        assert switch.state.peak_equity == Decimal("1000000")

    def test_state_round_trips_through_json(self) -> None:
        """It has to survive a crash to be worth anything."""
        switch = self._switch()
        switch.start_session(date(2026, 8, 19), Decimal("1000000"))
        switch.trip_manually("halt", utc(2026, 8, 19, 6, 0))
        restored = KillSwitchState.model_validate_json(switch.state.model_dump_json())
        assert restored == switch.state
        resumed = KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
            state=restored,
        )
        assert resumed.is_tripped


class TestRiskEngineCaps:
    """The caps that go beyond lot counts: the margin cap and the entry intent."""

    def _open_signal(
        self, goldm_future: FutureId, *, limit: Decimal | None = None, ratio: int = 1
    ) -> Signal:
        from algo.core.enums import Side, SignalAction
        from algo.core.signal import PriceIntent, SignalLeg

        entry = PriceIntent.limit(limit) if limit is not None else PriceIntent.market()
        return Signal(
            signal_id="cap-test",
            strategy_id="test",
            ts=utc(2026, 8, 19, 6, 0),
            action=SignalAction.OPEN,
            legs=(
                SignalLeg(instrument=goldm_future, direction=Side.SELL, entry=entry, ratio=ratio),
            ),
            reason="margin-cap test",
        )

    def _evaluate(
        self,
        signal: Signal,
        goldm_spec: InstrumentSpec,
        *,
        margin_cap_pct: Decimal | None = Decimal("10"),
        margin_used: Decimal = Decimal("0"),
        proposed_margin: Decimal = Decimal("200000"),
    ) -> RiskDecision:
        from algo.risk.engine import FixedLotSizer, RiskEngine, RiskSnapshot

        risk = RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=5,
            max_lots_per_underlying=10,
            margin_cap_pct=margin_cap_pct,
        )
        return risk.evaluate(
            signal,
            RiskSnapshot(
                now=signal.ts,
                session_day=date(2026, 8, 19),
                equity=Decimal("1000000"),
                open_position_count=0,
                lots_held=0,
                margin_used=margin_used,
                propose_margin=lambda lots: proposed_margin,
            ),
            spec=goldm_spec,
        )

    def test_the_margin_cap_rejects_an_opening_that_breaks_it(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        from algo.risk.engine import Rejected

        decision = self._evaluate(
            self._open_signal(goldm_future), goldm_spec, proposed_margin=Decimal("150000")
        )
        assert isinstance(decision, Rejected)
        assert decision.reason is RejectReason.MARGIN_CAP

    def test_an_opening_inside_the_cap_is_accepted(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        from algo.risk.engine import Accepted

        decision = self._evaluate(
            self._open_signal(goldm_future), goldm_spec, proposed_margin=Decimal("50000")
        )
        assert isinstance(decision, Accepted)

    def test_the_lots_cap_accounts_for_a_legs_ratio(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        """`FixedLotSizer(1)` sizes 1 lot; `max_lots_per_underlying=10`
        (`_evaluate`'s own setup). A leg with `ratio=15` actually places
        `1 * 15 = 15` lots (`RiskEngine.evaluate` scales every order by
        `leg.ratio`, same as `backtest/engine.py`'s margin notional) - well
        past the cap the un-scaled `lots=1` alone would have passed."""
        from algo.risk.engine import Rejected

        decision = self._evaluate(
            self._open_signal(goldm_future, ratio=15), goldm_spec, proposed_margin=Decimal("0")
        )

        assert isinstance(decision, Rejected)
        assert decision.reason is RejectReason.ABOVE_MAX_LOTS

    def test_a_ratio_that_still_fits_under_the_cap_is_accepted(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        """The other direction: a ratio of 2 makes 2 lots, still under the cap
        of 10 - the fix must not reject every ratio > 1 leg outright."""
        from algo.risk.engine import Accepted

        decision = self._evaluate(
            self._open_signal(goldm_future, ratio=2), goldm_spec, proposed_margin=Decimal("50000")
        )

        assert isinstance(decision, Accepted)

    def test_margin_already_blocked_counts_towards_the_cap(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        from algo.risk.engine import Rejected

        decision = self._evaluate(
            self._open_signal(goldm_future),
            goldm_spec,
            proposed_margin=Decimal("50000"),
            margin_used=Decimal("60000"),
        )
        assert isinstance(decision, Rejected)
        assert decision.reason is RejectReason.MARGIN_CAP

    def test_no_margin_model_means_no_cap(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        from algo.risk.engine import Accepted

        decision = self._evaluate(
            self._open_signal(goldm_future),
            goldm_spec,
            margin_cap_pct=None,
            proposed_margin=Decimal("999999"),
        )
        assert isinstance(decision, Accepted)

    def test_a_limit_intent_is_rejected_not_silently_ignored(
        self, goldm_spec: InstrumentSpec, goldm_future: FutureId
    ) -> None:
        from algo.risk.engine import Rejected

        decision = self._evaluate(
            self._open_signal(goldm_future, limit=Decimal("156000")), goldm_spec
        )
        assert isinstance(decision, Rejected)
        assert decision.reason is RejectReason.UNSUPPORTED_LIMIT_INTENT
