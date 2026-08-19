"""Reconciliation. Brief §2.3:

    "On reconnect, reconcile broker state against local state before sending
     anything."

The rule that shapes everything here: **when we cannot confirm what happened to an
order, we stop rather than guess.**

The tempting recovery for an order we sent and cannot find at the broker is "it
never arrived, send it again". That is wrong, and wrong in the expensive
direction. An order can be missing from the broker's open-order list because it
never arrived *or* because it already filled and was cleared. Resending on that
ambiguity doubles the position, and for a short strangle a doubled position is a
doubled unbounded risk.

So an unconfirmable order produces an `UNCONFIRMED_ORDER` drift, the report is not
clean, and the router refuses to trade until a human resolves it. That is a
deliberately inconvenient default. The convenient one loses money.

The reconciler adopts what the broker says wherever the broker can speak: its
order states, its executions, its positions. Our journal records intent; the
broker records reality; where they differ, reality wins and the difference is
reported rather than quietly overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from algo.core.enums import OrderState
from algo.core.fill import Fill
from algo.core.timeutil import iso
from algo.execution.broker import Broker, BrokerFillSnapshot
from algo.persistence.journal import JournalEntry, OrderJournal


class DriftKind(StrEnum):
    UNCONFIRMED_ORDER = "UNCONFIRMED_ORDER"
    """Sent, but the broker has no record of it. Might have filled and cleared."""

    ORDER_UNKNOWN_TO_US = "ORDER_UNKNOWN_TO_US"
    """The broker has an order we never journalled. Someone or something else."""

    PHANTOM_ORDER = "PHANTOM_ORDER"
    """The broker has an order we only ever journalled, never sent."""

    STATE_MISMATCH = "STATE_MISMATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    UNRECORDED_FILL = "UNRECORDED_FILL"


@dataclass(frozen=True, slots=True)
class Drift:
    """One disagreement between what we believe and what the broker says."""

    kind: DriftKind
    subject: str
    detail: str
    blocks_trading: bool = True

    def __str__(self) -> str:
        gate = "BLOCKING" if self.blocks_trading else "noted"
        return f"[{gate}] {self.kind} {self.subject}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    at: datetime
    orders_checked: int
    orders_adopted: int
    fills_recorded: int
    drifts: tuple[Drift, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple[Drift, ...]:
        return tuple(d for d in self.drifts if d.blocks_trading)

    @property
    def safe_to_trade(self) -> bool:
        return not self.blocking

    def summary(self) -> str:
        lines = [
            f"reconciliation at {iso(self.at)}",
            f"  orders checked   {self.orders_checked}",
            f"  orders adopted   {self.orders_adopted}",
            f"  fills recorded   {self.fills_recorded}",
            f"  safe to trade    {self.safe_to_trade}",
        ]
        lines.extend(f"  {d}" for d in self.drifts)
        return "\n".join(lines)


class Reconciler:
    """Brings the journal into agreement with the broker, or refuses to trade."""

    __slots__ = ("_broker", "_journal", "_position_tolerance")

    def __init__(
        self,
        broker: Broker,
        journal: OrderJournal,
        *,
        position_tolerance: Decimal = Decimal("0"),
    ) -> None:
        self._broker = broker
        self._journal = journal
        self._position_tolerance = position_tolerance

    def reconcile(self, *, now: datetime, since: datetime) -> ReconciliationReport:
        drifts: list[Drift] = []
        adopted = 0
        recorded = 0

        executions = self._broker.executions(since)
        by_order: dict[str, list[BrokerFillSnapshot]] = {}
        for execution in executions:
            by_order.setdefault(execution.client_order_id, []).append(execution)

        open_entries = self._journal.open_entries()
        for entry in open_entries:
            outcome, entry_drifts = self._reconcile_order(entry, by_order, now=now)
            adopted += 1 if outcome else 0
            drifts.extend(entry_drifts)

        recorded += self._adopt_executions(executions, drifts)
        drifts.extend(self._compare_orders(open_entries))
        drifts.extend(self._compare_positions())

        return ReconciliationReport(
            at=now,
            orders_checked=len(open_entries),
            orders_adopted=adopted,
            fills_recorded=recorded,
            drifts=tuple(drifts),
        )

    # ---------------------------------------------------------------- orders
    def _reconcile_order(
        self,
        entry: JournalEntry,
        executions: dict[str, list[BrokerFillSnapshot]],
        *,
        now: datetime,
    ) -> tuple[bool, list[Drift]]:
        remote = self._broker.order_by_client_id(entry.client_order_id)

        if not entry.may_have_reached_the_broker:
            # JOURNALLED: the send was never attempted, so the broker cannot know
            # about it. If it does, something bypassed the router.
            if remote is not None:
                return False, [
                    Drift(
                        kind=DriftKind.PHANTOM_ORDER,
                        subject=entry.client_order_id,
                        detail=(
                            "the broker holds an order that was journalled but never "
                            "sent — something placed it outside the router, and the "
                            "write-ahead guarantee no longer holds"
                        ),
                    )
                ]
            return False, []

        if remote is not None:
            self._journal.mark_state(
                entry.client_order_id,
                remote.state,
                at=now,
                filled_qty=remote.filled_qty,
                average_price=remote.average_price,
                broker_order_id=remote.broker_order_id,
                error=remote.message,
            )
            return True, []

        if executions.get(entry.client_order_id):
            # Absent from the order book but present in the executions — it filled
            # and was cleared. Adopt the fill rather than treat it as missing.
            filled = sum(
                (e.qty for e in executions[entry.client_order_id]), Decimal("0")
            )
            self._journal.mark_state(
                entry.client_order_id,
                OrderState.FILLED,
                at=now,
                filled_qty=filled,
                broker_order_id=executions[entry.client_order_id][0].broker_order_id,
            )
            return True, []

        return False, [
            Drift(
                kind=DriftKind.UNCONFIRMED_ORDER,
                subject=entry.client_order_id,
                detail=(
                    f"marked {entry.state} locally but the broker has no order and no "
                    "execution for it. It may never have arrived, or it may have "
                    "filled and been cleared. Refusing to resend on that ambiguity — "
                    "resolve it by hand before trading resumes"
                ),
            )
        ]

    def _adopt_executions(
        self, executions: list[BrokerFillSnapshot], drifts: list[Drift]
    ) -> int:
        """Record every execution the broker reports that we have not seen.

        Idempotent on `fill_id`, so replaying the day's executions on every
        reconnect cannot double-count one — which is the mechanism behind the
        "no duplicate fills" requirement in brief §11.
        """
        recorded = 0
        for execution in executions:
            if self._journal.has_fill(execution.fill_id):
                continue
            entry = self._journal.get(execution.client_order_id)
            if entry is None:
                drifts.append(
                    Drift(
                        kind=DriftKind.ORDER_UNKNOWN_TO_US,
                        subject=execution.client_order_id,
                        detail=(
                            f"execution {execution.fill_id} belongs to an order this "
                            "system never journalled"
                        ),
                    )
                )
                continue
            self._journal.record_fill(
                Fill(
                    fill_id=execution.fill_id,
                    client_order_id=execution.client_order_id,
                    signal_id=entry.signal_id,
                    instrument=entry.order.instrument,
                    side=execution.side,
                    lots=execution.lots,
                    qty=execution.qty,
                    price=execution.price,
                    ts=execution.ts,
                )
            )
            drifts.append(
                Drift(
                    kind=DriftKind.UNRECORDED_FILL,
                    subject=execution.client_order_id,
                    detail=f"adopted execution {execution.fill_id} from the broker",
                    blocks_trading=False,
                )
            )
            recorded += 1
        return recorded

    def _compare_orders(self, open_entries: list[JournalEntry]) -> list[Drift]:
        known = {e.client_order_id for e in self._journal.all_entries()}
        return [
            Drift(
                kind=DriftKind.ORDER_UNKNOWN_TO_US,
                subject=remote.client_order_id,
                detail=(
                    f"the broker holds a live {remote.side} order for "
                    f"{remote.instrument_key} that this system has no record of"
                ),
            )
            for remote in self._broker.open_orders()
            if remote.client_order_id not in known
        ]

    # -------------------------------------------------------------- positions
    def _compare_positions(self) -> list[Drift]:
        """Compare the broker's positions against the fills we have recorded.

        Position drift is the symptom that matters most: an order can be resolved
        by hand, but a position we do not know about is risk we are not managing.
        """
        local: dict[str, Decimal] = {}
        for fill in self._journal.fills():
            local[fill.instrument.key] = local.get(
                fill.instrument.key, Decimal("0")
            ) + fill.qty * Decimal(fill.side.sign)

        remote = {p.instrument_key: p.qty for p in self._broker.positions()}
        drifts: list[Drift] = []
        for key in sorted(set(local) | set(remote)):
            ours = local.get(key, Decimal("0"))
            theirs = remote.get(key, Decimal("0"))
            if abs(ours - theirs) > self._position_tolerance:
                drifts.append(
                    Drift(
                        kind=DriftKind.POSITION_MISMATCH,
                        subject=key,
                        detail=f"our fills imply {ours}, the broker reports {theirs}",
                    )
                )
        return drifts
