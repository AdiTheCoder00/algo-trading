"""The paper broker: live data, simulated fills, its own books.

Brief §9 Milestone 6 — "live data, simulated fills, full reconciliation logic,
reconnect handling, crash recovery from persisted state".

Two properties make this worth having rather than a toy.

**It uses the same fill simulator as the backtest.** Not a similar one — the same
`FillSimulator` object, applying the same spread, slippage and charge models
(brief §4). If paper and backtest disagree about a fill, the disagreement is in the
data or the timing, never in two implementations that drifted apart.

**It keeps its own books, separately persisted.** A real broker remembers your
orders when your process dies. A paper broker that lived only in our memory would
make crash recovery untestable, because the thing recovery has to reconcile
*against* would vanish at the same moment. So its state is saved to its own file,
and a "restart" reloads it while the engine starts from nothing — which is exactly
the asymmetry a real crash produces.

The fault hook exists for reconnection drills. A paper adapter whose failure
behaviour is never exercised is an untested assumption about the most dangerous
part of the system.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from algo.core.clock import Clock
from algo.core.enums import Exchange, OrderState, Side
from algo.core.errors import FatalBrokerError, RetryableBrokerError
from algo.core.instrument import InstrumentId, OptionId
from algo.core.order import BrokerOrderRef, Order
from algo.core.timeutil import ensure_utc, iso, ist_date
from algo.exchange.specs import ContractSpecStore
from algo.execution.broker import (
    BrokerFillSnapshot,
    BrokerHealth,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    Funds,
)
from algo.execution.fills import FillSimulator

#: Returns the current price for an instrument, or None when it is not quoted.
QuoteFn = Callable[[str], Decimal | None]
#: Optional fault hook for reconnection drills. Raise to simulate a failure.
FaultFn = Callable[[str, Order | None], None]


class PaperBroker:
    """A broker that behaves like one, without sending anything anywhere."""

    __slots__ = (
        "_clock",
        "_connected",
        "_exchange",
        "_fault",
        "_fills",
        "_instruments",
        "_orders",
        "_quote",
        "_sequence",
        "_simulator",
        "_specs",
        "_starting_cash",
    )

    def __init__(
        self,
        *,
        simulator: FillSimulator,
        specs: ContractSpecStore,
        quote: QuoteFn,
        clock: Clock,
        starting_cash: Decimal = Decimal("1000000"),
        exchange: Exchange = Exchange.MCX,
        fault: FaultFn | None = None,
    ) -> None:
        self._simulator = simulator
        self._specs = specs
        self._quote = quote
        self._clock = clock
        self._exchange = exchange
        self._starting_cash = starting_cash
        self._fault = fault
        self._connected = False
        self._orders: dict[str, BrokerOrderSnapshot] = {}
        self._fills: list[BrokerFillSnapshot] = []
        self._instruments: dict[str, InstrumentId] = {}
        self._sequence = 0

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        self._trip("connect", None)
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self._connected,
            last_heartbeat=self._clock.now() if self._connected else None,
            detail="paper adapter" if self._connected else "disconnected",
        )

    def _require_connection(self, action: str) -> None:
        if not self._connected:
            raise RetryableBrokerError(
                f"cannot {action}: the paper adapter is disconnected. This is "
                "retryable, but only after reconciling — the connection dropping "
                "is exactly when local state stops being trustworthy"
            )

    def _trip(self, action: str, order: Order | None) -> None:
        if self._fault is not None:
            self._fault(action, order)

    # ----------------------------------------------------------------- orders
    def place(self, order: Order) -> BrokerOrderRef:
        self._require_connection("place an order")
        self._trip("place", order)

        if order.client_order_id in self._orders:
            # A real broker rejects a duplicate client id rather than silently
            # accepting a second order. Mirroring that is what makes the router's
            # idempotency testable rather than assumed.
            raise FatalBrokerError(
                f"duplicate client order id {order.client_order_id}; the broker "
                "already holds an order with that id"
            )

        self._sequence += 1
        broker_order_id = f"PAPER-{self._sequence:06d}"
        now = self._clock.now()
        self._instruments[order.instrument.key] = order.instrument

        price = self._quote(order.instrument.key)
        if price is None:
            self._orders[order.client_order_id] = BrokerOrderSnapshot(
                client_order_id=order.client_order_id,
                broker_order_id=broker_order_id,
                instrument_key=order.instrument.key,
                side=order.side,
                lots=order.lots,
                state=OrderState.REJECTED,
                message="no quote available for this instrument",
                updated_at=now,
            )
            raise FatalBrokerError(
                f"{order.instrument.key} is not quoted; refusing to invent a fill price"
            )

        fill = self._simulate(order, price, now)
        self._fills.append(fill)
        self._orders[order.client_order_id] = BrokerOrderSnapshot(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            instrument_key=order.instrument.key,
            side=order.side,
            lots=order.lots,
            state=OrderState.FILLED,
            filled_qty=fill.qty,
            average_price=fill.price,
            updated_at=now,
        )
        return BrokerOrderRef(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            accepted_at=now,
        )

    def _simulate(self, order: Order, price: Decimal, now: datetime) -> BrokerFillSnapshot:
        session_day: date = ist_date(now)
        spec = self._specs.spec_for(order.instrument.underlying, self._exchange, session_day)
        simulated = self._simulator.fill(
            fill_id=f"PF-{self._sequence:06d}",
            client_order_id=order.client_order_id,
            signal_id=order.signal_id,
            instrument_key=order.instrument.key,
            instrument=order.instrument,
            side=order.side,
            lots=order.lots,
            reference_price=price,
            spec=spec,
            ts_utc=now,
            session_day=session_day,
            is_option=isinstance(order.instrument, OptionId),
        )
        return BrokerFillSnapshot(
            fill_id=simulated.fill_id,
            client_order_id=order.client_order_id,
            broker_order_id=f"PAPER-{self._sequence:06d}",
            instrument_key=order.instrument.key,
            side=order.side,
            lots=order.lots,
            qty=simulated.qty,
            price=simulated.price,
            ts=simulated.ts,
        )

    def cancel(self, client_order_id: str) -> None:
        self._require_connection("cancel an order")
        snapshot = self._orders.get(client_order_id)
        if snapshot is None:
            raise FatalBrokerError(f"unknown order {client_order_id}")
        if snapshot.state is OrderState.FILLED:
            raise FatalBrokerError(f"{client_order_id} has already filled and cannot be cancelled")
        self._orders[client_order_id] = snapshot.model_copy(
            update={"state": OrderState.CANCELLED, "updated_at": self._clock.now()}
        )

    # ------------------------------------------------------------------ reads
    def open_orders(self) -> list[BrokerOrderSnapshot]:
        self._require_connection("read open orders")
        return [
            snapshot
            for _, snapshot in sorted(self._orders.items())
            if snapshot.state
            not in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)
        ]

    def order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        self._require_connection("look up an order")
        return self._orders.get(client_order_id)

    def executions(self, since: datetime) -> list[BrokerFillSnapshot]:
        self._require_connection("read executions")
        cutoff = ensure_utc(since)
        return [fill for fill in self._fills if fill.ts >= cutoff]

    def positions(self) -> list[BrokerPositionSnapshot]:
        self._require_connection("read positions")
        net: dict[str, tuple[Decimal, int, Decimal]] = {}
        for fill in self._fills:
            qty, lots, cost = net.get(fill.instrument_key, (Decimal("0"), 0, Decimal("0")))
            signed = fill.qty * Decimal(fill.side.sign)
            net[fill.instrument_key] = (
                qty + signed,
                lots + fill.lots * fill.side.sign,
                cost + fill.price * fill.qty,
            )
        return [
            BrokerPositionSnapshot(
                instrument_key=key,
                qty=qty,
                lots=lots,
                average_price=(cost / abs(qty)) if qty else Decimal("0"),
            )
            for key, (qty, lots, cost) in sorted(net.items())
            if qty != 0
        ]

    def funds(self) -> Funds:
        self._require_connection("read funds")
        cash = self._starting_cash
        for fill in self._fills:
            spec = self._specs.spec_for(
                self._instruments[fill.instrument_key].underlying,
                self._exchange,
                ist_date(fill.ts),
            )
            notional = fill.price * fill.qty * spec.multiplier
            cash += notional if fill.side is Side.SELL else -notional
        return Funds(cash=cash)

    # ------------------------------------------------- persistence for restarts
    def save(self, path: Path | str) -> None:
        """Write the broker's own books to disk.

        A real broker survives our crash. Persisting separately is what lets a
        test restart the *engine* while the *broker* remembers — the asymmetry a
        real crash actually produces, and the one reconciliation exists for.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "sequence": self._sequence,
                    "orders": [s.model_dump(mode="json") for s in self._orders.values()],
                    "fills": [f.model_dump(mode="json") for f in self._fills],
                    "instruments": {
                        key: value.model_dump(mode="json")
                        for key, value in self._instruments.items()
                    },
                    "starting_cash": str(self._starting_cash),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def restore(self, path: Path | str) -> None:
        source = Path(path)
        if not source.exists():
            return
        raw = json.loads(source.read_text(encoding="utf-8"))
        self._sequence = int(raw["sequence"])
        self._starting_cash = Decimal(raw["starting_cash"])
        self._orders = {
            entry["client_order_id"]: BrokerOrderSnapshot.model_validate(entry)
            for entry in raw["orders"]
        }
        self._fills = [BrokerFillSnapshot.model_validate(entry) for entry in raw["fills"]]
        self._instruments = {
            key: _instrument_from(value) for key, value in raw["instruments"].items()
        }

    def __repr__(self) -> str:
        return (
            f"PaperBroker(connected={self._connected}, orders={len(self._orders)}, "
            f"fills={len(self._fills)}, at={iso(self._clock.now())})"
        )


def _instrument_from(payload: dict[str, object]) -> InstrumentId:
    from algo.core.instrument import CfdId, FutureId

    if payload.get("kind") == "option":
        return OptionId.model_validate(payload)
    if payload.get("kind") == "cfd":
        return CfdId.model_validate(payload)
    return FutureId.model_validate(payload)
