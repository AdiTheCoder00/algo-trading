"""Idempotency, reconciliation and crash recovery. Brief §2.3, §9 M6, §11.

    §11: "Reconciliation test: simulate a disconnect mid-order and assert no
          duplicate fills"

The disconnect is injected at each of the three points where it can do damage —
before the send, during it, and after it but before the acknowledgement — and each
one is asserted separately. A single "it recovers" test would pass while two of
the three windows were broken.

The scenario that matters most is `TestCrashRecovery`: the broker keeps its books,
the engine loses everything, and reconciliation has to work out what actually
happened from two sources that disagree.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.clock import BacktestClock
from algo.core.enums import Exchange, OrderState, OrderType, Right, Side
from algo.core.errors import DomainError, FatalBrokerError, RetryableBrokerError
from algo.core.instrument import FutureId, InstrumentSpec, OptionId
from algo.core.order import Order
from algo.core.timeutil import utc
from algo.costs.charges import FlatChargeModel
from algo.costs.slippage import NoSlippage
from algo.costs.spread import FixedTickSpread
from algo.exchange.specs import ContractSpecStore
from algo.execution.broker import Broker
from algo.execution.fills import FillSimulator
from algo.execution.paper import PaperBroker
from algo.execution.reconcile import DriftKind, Reconciler
from algo.execution.router import OrderRouter, Outcome
from algo.persistence.journal import OrderJournal

NOW = utc(2026, 8, 19, 4, 0)
GOLDM_FUT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))
CALL = OptionId(
    underlying_future=GOLDM_FUT,
    option_expiry=date(2026, 8, 28),
    strike=Decimal("160500"),
    right=Right.CE,
)
PRICE = Decimal("756.00")

SPEC = InstrumentSpec(
    underlying="GOLDM",
    exchange=Exchange.MCX,
    lot_size=Decimal("100"),
    multiplier=Decimal("10"),
    tick_size=Decimal("0.50"),
    min_lots=1,
    effective_from=date(2026, 1, 1),
    source="execution fixture",
)


def _order(suffix: str = "0", side: Side = Side.SELL) -> Order:
    return Order(
        client_order_id=f"strat.sig123.{suffix}.0",
        signal_id="sig123",
        instrument=CALL,
        side=side,
        lots=1,
        qty=Decimal("1"),
        order_type=OrderType.MARKET,
        created_at=NOW,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[OrderJournal]:
    with OrderJournal(tmp_path / "journal.db") as open_journal:
        yield open_journal


@pytest.fixture
def clock() -> BacktestClock:
    return BacktestClock(NOW)


def _broker(clock: BacktestClock, *, fault: object = None) -> PaperBroker:
    broker = PaperBroker(
        simulator=FillSimulator(
            spread=FixedTickSpread(2),
            slippage=NoSlippage(),
            charges=FlatChargeModel(Decimal("20")),
        ),
        specs=ContractSpecStore([SPEC]),
        quote=lambda key: PRICE if key == CALL.key else None,
        clock=clock,
        fault=fault,  # type: ignore[arg-type]
    )
    broker.connect()
    return broker


def _router(broker: Broker, journal: OrderJournal, clock: BacktestClock) -> OrderRouter:
    return OrderRouter(broker=broker, journal=journal, clock=clock)


class TestJournalIsIdempotent:
    def test_recording_the_same_intent_twice_writes_one_row(
        self, journal: OrderJournal
    ) -> None:
        """The guarantee that makes a crash replay safe rather than merely likely
        to work."""
        order = _order()
        first = journal.record_intent(order, at=NOW)
        second = journal.record_intent(order, at=NOW + timedelta(minutes=5))
        assert first.client_order_id == second.client_order_id
        assert len(journal.all_entries()) == 1
        assert second.created_at == first.created_at

    def test_a_terminal_order_cannot_be_moved_backwards(
        self, journal: OrderJournal
    ) -> None:
        order = _order()
        journal.record_intent(order, at=NOW)
        journal.mark_state(order.client_order_id, OrderState.FILLED, at=NOW)
        with pytest.raises(DomainError, match="is how a filled order gets sent twice"):
            journal.mark_sent(order.client_order_id, at=NOW)

    def test_a_state_change_on_an_unjournalled_order_is_refused(
        self, journal: OrderJournal
    ) -> None:
        """Every order is written before it is sent; anything else bypassed the router."""
        with pytest.raises(DomainError, match="bypassed the router"):
            journal.mark_sent("never.seen.0.0", at=NOW)

    def test_prices_survive_the_round_trip_exactly(self, journal: OrderJournal) -> None:
        """Stored as TEXT, not REAL. This file is what a crash recovers from."""
        order = _order()
        journal.record_intent(order, at=NOW)
        journal.mark_state(
            order.client_order_id,
            OrderState.FILLED,
            at=NOW,
            filled_qty=Decimal("1"),
            average_price=Decimal("156640.05"),
        )
        entry = journal.get(order.client_order_id)
        assert entry is not None
        assert entry.average_price == Decimal("156640.05")
        assert str(entry.average_price) == "156640.05"

    def test_the_same_fill_is_never_stored_twice(self, journal: OrderJournal) -> None:
        """A reconnect replays the day's executions; that must not double-count."""
        from algo.core.fill import Fill

        fill = Fill(
            fill_id="F1",
            client_order_id="strat.sig123.0.0",
            signal_id="sig123",
            instrument=CALL,
            side=Side.SELL,
            lots=1,
            qty=Decimal("1"),
            price=PRICE,
            ts=NOW,
        )
        assert journal.record_fill(fill) is True
        assert journal.record_fill(fill) is False
        assert len(journal.fills()) == 1


class TestRouterSendsAtMostOnce:
    def test_it_refuses_to_trade_before_reconciling(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """§2.3 — reconcile before sending anything, including the first order."""
        router = _router(_broker(clock), journal, clock)
        result = router.place(_order())
        assert result.outcome is Outcome.BLOCKED_UNRECONCILED
        assert "no reconciliation has run yet" in result.detail
        assert not journal.all_entries()

    def test_a_clean_reconciliation_unblocks_it(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        router = _router(_broker(clock), journal, clock)
        assert router.reconcile().safe_to_trade
        assert router.place(_order()).outcome is Outcome.PLACED

    def test_replaying_the_same_order_does_not_send_it_again(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """The order is ACKNOWLEDGED, not yet known to have filled — a real broker
        reports fills asynchronously, so the router has only the acknowledgement.
        The replay must still not resend."""
        broker = _broker(clock)
        router = _router(broker, journal, clock)
        router.reconcile()

        first = router.place(_order())
        second = router.place(_order())

        assert first.outcome is Outcome.PLACED
        assert second.outcome is Outcome.ALREADY_IN_FLIGHT
        assert len(broker.executions(NOW - timedelta(days=1))) == 1

    def test_after_reconciling_the_replay_reports_it_as_finished(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        broker = _broker(clock)
        router = _router(broker, journal, clock)
        router.reconcile()
        router.place(_order())

        router.reconcile(since=NOW - timedelta(days=1))
        assert router.place(_order()).outcome is Outcome.ALREADY_TERMINAL
        assert len(broker.executions(NOW - timedelta(days=1))) == 1

    def test_an_in_flight_order_is_never_re_sent(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """Retrying an unconfirmed order is how a position doubles."""
        order = _order()
        journal.record_intent(order, at=NOW)
        journal.mark_sent(order.client_order_id, at=NOW)

        router = _router(_broker(clock), journal, clock)
        router.reconcile()
        # Reconciliation will flag it; force the check we care about directly.
        result = router.place(order)
        assert result.outcome in (Outcome.ALREADY_IN_FLIGHT, Outcome.BLOCKED_UNRECONCILED)

    def test_the_journal_records_sent_before_the_broker_is_called(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """The ordering the whole design rests on.

        A crash between the write and the call must leave an ambiguous SENT, never
        a JOURNALLED order the broker already holds.
        """
        seen: list[str] = []

        def fault(action: str, order: Order | None) -> None:
            del order
            if action == "place":
                entry = journal.get("strat.sig123.0.0")
                seen.append(entry.state.value if entry else "MISSING")

        router = _router(_broker(clock, fault=fault), journal, clock)
        router.reconcile()
        router.place(_order())
        assert seen == ["SENT"]

    def test_a_fatal_error_marks_the_order_rejected(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        def fault(action: str, order: Order | None) -> None:
            del order
            if action == "place":
                raise FatalBrokerError("margin shortfall")

        router = _router(_broker(clock, fault=fault), journal, clock)
        router.reconcile()
        result = router.place(_order())
        assert result.outcome is Outcome.REJECTED
        entry = journal.get("strat.sig123.0.0")
        assert entry is not None and entry.state is OrderState.REJECTED

    def test_a_retryable_error_leaves_it_unconfirmed_and_does_not_retry(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """Retrying a request that may already have been accepted is the same
        mistake as resending, dressed up as resilience."""

        def fault(action: str, order: Order | None) -> None:
            del order
            if action == "place":
                raise RetryableBrokerError("gateway timeout")

        broker = _broker(clock, fault=fault)
        router = _router(broker, journal, clock)
        router.reconcile()
        result = router.place(_order())

        assert result.outcome is Outcome.UNCONFIRMED
        entry = journal.get("strat.sig123.0.0")
        assert entry is not None and entry.state is OrderState.SENT

    def test_a_combo_stops_at_the_first_leg_that_does_not_reach_the_broker(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """Continuing after a failed leg leaves a naked short option (D-008)."""
        attempts: list[str] = []

        def fault(action: str, order: Order | None) -> None:
            if action == "place" and order is not None:
                attempts.append(order.client_order_id)
                if order.client_order_id.endswith("1.0"):
                    raise FatalBrokerError("second leg rejected")

        router = _router(_broker(clock, fault=fault), journal, clock)
        router.reconcile()
        results = router.place_all([_order("0"), _order("1"), _order("2")])

        assert [r.outcome for r in results] == [Outcome.PLACED, Outcome.REJECTED]
        assert len(attempts) == 2, "the third leg was never attempted"


class TestDisconnectMidOrder:
    """Brief §11, at each of the three windows where a disconnect can do damage."""

    def test_disconnect_before_the_send_leaves_nothing_behind(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        broker = _broker(clock)
        router = _router(broker, journal, clock)
        router.reconcile()
        broker.disconnect()

        result = router.place(_order())
        assert result.outcome is Outcome.UNCONFIRMED
        assert not broker._fills

    def test_disconnect_during_the_send_produces_no_duplicate_fill(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """The order did arrive; the acknowledgement did not. Recovery must adopt
        the fill, not place a second order."""
        broker = _broker(clock)
        router = _router(broker, journal, clock)
        router.reconcile()

        order = _order()
        journal.record_intent(order, at=NOW)
        journal.mark_sent(order.client_order_id, at=NOW)
        broker.place(order)  # reached the venue; we never learned it did

        report = Reconciler(broker, journal).reconcile(
            now=NOW + timedelta(minutes=1), since=NOW - timedelta(days=1)
        )

        entry = journal.get(order.client_order_id)
        assert entry is not None and entry.state is OrderState.FILLED
        assert len(journal.fills()) == 1
        assert len(broker.executions(NOW - timedelta(days=1))) == 1
        assert report.orders_adopted == 1

    def test_a_second_reconciliation_changes_nothing(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """Reconnecting repeatedly must not accumulate fills."""
        broker = _broker(clock)
        order = _order()
        journal.record_intent(order, at=NOW)
        journal.mark_sent(order.client_order_id, at=NOW)
        broker.place(order)

        reconciler = Reconciler(broker, journal)
        for _ in range(3):
            reconciler.reconcile(now=NOW + timedelta(minutes=1), since=NOW - timedelta(days=1))
        assert len(journal.fills()) == 1

    def test_an_order_the_broker_never_saw_is_not_silently_resent(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """The ambiguity that must halt trading rather than resolve itself."""
        broker = _broker(clock)
        order = _order()
        journal.record_intent(order, at=NOW)
        journal.mark_sent(order.client_order_id, at=NOW)
        # Never actually placed at the broker.

        report = Reconciler(broker, journal).reconcile(
            now=NOW + timedelta(minutes=1), since=NOW - timedelta(days=1)
        )
        assert not report.safe_to_trade
        assert any(d.kind is DriftKind.UNCONFIRMED_ORDER for d in report.drifts)
        assert "Refusing to resend on that ambiguity" in report.summary()

    def test_the_router_will_not_trade_while_that_drift_stands(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        broker = _broker(clock)
        stranded = _order("9")
        journal.record_intent(stranded, at=NOW)
        journal.mark_sent(stranded.client_order_id, at=NOW)

        router = _router(broker, journal, clock)
        router.reconcile()
        assert not router.is_safe_to_trade

        result = router.place(_order("0"))
        assert result.outcome is Outcome.BLOCKED_UNRECONCILED
        assert "UNCONFIRMED_ORDER" in result.detail


class TestCrashRecovery:
    """The broker remembers; the engine does not. That asymmetry is the whole test."""

    def test_state_is_rebuilt_from_the_journal_and_the_broker(
        self, tmp_path: Path, clock: BacktestClock
    ) -> None:
        journal_path = tmp_path / "journal.db"
        broker_path = tmp_path / "broker.json"

        # --- session one: place an order, then die before recording the outcome
        broker = _broker(clock)
        with OrderJournal(journal_path) as first_journal:
            router = _router(broker, first_journal, clock)
            router.reconcile()
            order = _order()
            first_journal.record_intent(order, at=NOW)
            first_journal.mark_sent(order.client_order_id, at=NOW)
            broker.place(order)
        broker.save(broker_path)

        # --- session two: fresh process, fresh broker connection, same books
        clock.advance_to(NOW + timedelta(hours=1))
        recovered_broker = _broker(clock)
        recovered_broker.restore(broker_path)

        with OrderJournal(journal_path) as second_journal:
            assert second_journal.get(order.client_order_id) is not None
            recovered_router = _router(recovered_broker, second_journal, clock)
            report = recovered_router.reconcile(since=NOW - timedelta(days=1))

            assert report.safe_to_trade, report.summary()
            entry = second_journal.get(order.client_order_id)
            assert entry is not None and entry.state is OrderState.FILLED
            assert len(second_journal.fills()) == 1

            # And the replayed signal does not produce a second order.
            assert recovered_router.place(order).outcome is Outcome.ALREADY_TERMINAL
            assert len(recovered_broker.executions(NOW - timedelta(days=1))) == 1

    def test_a_position_the_journal_does_not_know_about_blocks_trading(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """The drift that matters most: risk we are not managing."""
        broker = _broker(clock)
        broker.place(_order("7"))  # placed outside the router entirely

        report = Reconciler(broker, journal).reconcile(
            now=NOW, since=NOW - timedelta(days=1)
        )
        assert not report.safe_to_trade
        kinds = {d.kind for d in report.drifts}
        assert DriftKind.ORDER_UNKNOWN_TO_US in kinds or DriftKind.POSITION_MISMATCH in kinds

    def test_positions_agree_after_a_clean_run(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        broker = _broker(clock)
        router = _router(broker, journal, clock)
        router.reconcile()
        router.place(_order())

        report = Reconciler(broker, journal).reconcile(
            now=NOW + timedelta(minutes=1), since=NOW - timedelta(days=1)
        )
        assert not [d for d in report.drifts if d.kind is DriftKind.POSITION_MISMATCH]


class TestPaperBrokerBehaviour:
    def test_it_uses_the_same_fill_simulator_as_the_backtest(
        self, journal: OrderJournal, clock: BacktestClock
    ) -> None:
        """A sell crosses the spread downward, exactly as in a backtest fill."""
        broker = _broker(clock)
        broker.place(_order())
        fill = broker.executions(NOW - timedelta(days=1))[0]
        assert fill.price == PRICE - Decimal("0.50")  # one tick of a two-tick spread

    def test_it_refuses_to_invent_a_price_for_an_unquoted_instrument(
        self, clock: BacktestClock
    ) -> None:
        broker = _broker(clock)
        unquoted = _order().model_copy(update={"instrument": GOLDM_FUT})
        with pytest.raises(FatalBrokerError, match="refusing to invent a fill price"):
            broker.place(unquoted)

    def test_a_duplicate_client_order_id_is_rejected(self, clock: BacktestClock) -> None:
        """Mirroring a real broker is what makes the router's idempotency testable."""
        broker = _broker(clock)
        broker.place(_order())
        with pytest.raises(FatalBrokerError, match="duplicate client order id"):
            broker.place(_order())

    def test_a_disconnected_broker_fails_retryably(self, clock: BacktestClock) -> None:
        broker = _broker(clock)
        broker.disconnect()
        with pytest.raises(RetryableBrokerError, match="disconnected"):
            broker.positions()
        assert not broker.health().connected

    def test_positions_net_out(self, clock: BacktestClock) -> None:
        broker = _broker(clock)
        broker.place(_order("0", side=Side.SELL))
        broker.place(_order("1", side=Side.BUY))
        assert broker.positions() == []
