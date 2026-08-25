"""The order router. Brief §2.3 — every order is idempotent, never fire-and-forget.

One rule governs this file: **an order is written before it is sent, and never sent
twice.**

The router is the only path from a risk decision to a broker. Everything it does is
arranged so that a process dying at any point leaves a state a restart can recover
from safely:

* Before the network call, the journal says SENT. A crash there is ambiguous, and
  reconciliation asks the broker.
* An entry that has reached SENT or beyond is **never re-sent**, whatever happens.
  Retrying an unconfirmed order is how a position doubles.
* Trading is refused entirely until a reconciliation has run and come back clean.

A retryable error does not trigger an automatic retry here. It leaves the order in
SENT and reports it. Retrying a request that may already have been accepted is the
same mistake as resending, dressed up as resilience.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from algo.core.clock import Clock
from algo.core.enums import OrderState
from algo.core.errors import FatalBrokerError, RetryableBrokerError
from algo.core.order import BrokerOrderRef, Order
from algo.execution.broker import Broker
from algo.execution.reconcile import Reconciler, ReconciliationReport
from algo.persistence.journal import OrderJournal


class Outcome(StrEnum):
    PLACED = "PLACED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    """The order already finished. A replay found it and did nothing."""

    ALREADY_IN_FLIGHT = "ALREADY_IN_FLIGHT"
    """Sent but not yet resolved. Deliberately not re-sent."""

    UNCONFIRMED = "UNCONFIRMED"
    """The send failed in a way that leaves it unknown whether it arrived."""

    REJECTED = "REJECTED"
    BLOCKED_UNRECONCILED = "BLOCKED_UNRECONCILED"


@dataclass(frozen=True, slots=True)
class RoutingResult:
    outcome: Outcome
    client_order_id: str
    detail: str = ""
    ref: BrokerOrderRef | None = None

    @property
    def reached_the_broker(self) -> bool:
        return self.outcome is Outcome.PLACED


class OrderRouter:
    """Journal, send, confirm — in that order, exactly once."""

    __slots__ = ("_broker", "_clock", "_journal", "_last_report", "_lookback", "_reconciler")

    def __init__(
        self,
        *,
        broker: Broker,
        journal: OrderJournal,
        clock: Clock,
        reconciler: Reconciler | None = None,
        execution_lookback: timedelta = timedelta(days=1),
    ) -> None:
        self._broker = broker
        self._journal = journal
        self._clock = clock
        self._reconciler = reconciler or Reconciler(broker, journal)
        self._last_report: ReconciliationReport | None = None
        self._lookback = execution_lookback

    # ---------------------------------------------------------- reconciliation
    @property
    def last_reconciliation(self) -> ReconciliationReport | None:
        return self._last_report

    @property
    def is_safe_to_trade(self) -> bool:
        """False until a clean reconciliation has run.

        Deliberately false on a fresh router too. "Reconcile before sending
        anything" (§2.3) means anything, including the first order after a
        restart — which is precisely when the local state is least trustworthy.
        """
        return self._last_report is not None and self._last_report.safe_to_trade

    def reconcile(self, *, since: datetime | None = None) -> ReconciliationReport:
        now = self._clock.now()
        report = self._reconciler.reconcile(
            now=now, since=since if since is not None else now - self._lookback
        )
        self._last_report = report
        return report

    # ----------------------------------------------------------------- sending
    def place(self, order: Order) -> RoutingResult:
        """Place `order` at most once, ever."""
        coid = order.client_order_id

        if not self.is_safe_to_trade:
            detail = (
                "no reconciliation has run yet"
                if self._last_report is None
                else "; ".join(str(d) for d in self._last_report.blocking)
            )
            return RoutingResult(
                outcome=Outcome.BLOCKED_UNRECONCILED,
                client_order_id=coid,
                detail=f"refusing to send before the books agree: {detail}",
            )

        existing = self._journal.get(coid)
        if existing is not None and existing.is_terminal:
            return RoutingResult(
                outcome=Outcome.ALREADY_TERMINAL,
                client_order_id=coid,
                detail=f"already {existing.state}; a replay of the same signal changes nothing",
            )
        if existing is not None and existing.may_have_reached_the_broker:
            return RoutingResult(
                outcome=Outcome.ALREADY_IN_FLIGHT,
                client_order_id=coid,
                detail=(
                    f"already {existing.state}. Not re-sending - retrying an "
                    "unconfirmed order is how a position doubles"
                ),
            )

        now = self._clock.now()
        self._journal.record_intent(order, at=now)
        # Written BEFORE the call. A crash between here and the broker leaves an
        # ambiguous SENT, which reconciliation can resolve; the reverse ordering
        # would leave a JOURNALLED order the broker already holds, and recovery
        # would send it again.
        self._journal.mark_sent(coid, at=now)

        try:
            ref = self._broker.place(order)
        except FatalBrokerError as exc:
            self._journal.mark_state(
                coid, OrderState.REJECTED, at=self._clock.now(), error=str(exc)
            )
            return RoutingResult(
                outcome=Outcome.REJECTED, client_order_id=coid, detail=str(exc)
            )
        except RetryableBrokerError as exc:
            # Left in SENT on purpose. It may or may not have arrived, and only the
            # broker can say which — so reconciliation decides, not a retry loop.
            return RoutingResult(
                outcome=Outcome.UNCONFIRMED,
                client_order_id=coid,
                detail=(
                    f"{exc} - the order stays SENT and unresolved. Reconcile before "
                    "doing anything else with it"
                ),
            )

        self._journal.mark_acknowledged(coid, ref.broker_order_id, at=self._clock.now())
        return RoutingResult(outcome=Outcome.PLACED, client_order_id=coid, ref=ref)

    def place_all(self, orders: list[Order]) -> list[RoutingResult]:
        """Place several orders, stopping at the first that does not reach the broker.

        For an all-or-none combo, continuing after a failed leg would leave a naked
        short option — a different instrument of risk entirely (D-008). The caller
        unwinds whatever did fill.
        """
        results: list[RoutingResult] = []
        for order in orders:
            result = self.place(order)
            results.append(result)
            if not result.reached_the_broker:
                break
        return results
