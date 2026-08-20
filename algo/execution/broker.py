"""The broker boundary — the only place the engine faces outward.

Everything above this line is deterministic and testable offline. Everything below
it is somebody else's system, reachable only over a network that will fail at the
worst possible moment.

Two design rules follow from brief §2.3 ("never fire-and-forget"):

**Every snapshot carries our own `client_order_id`.** Reconciliation has to be able
to ask "is *this* order — the one I intended — live at the broker?", and matching on
symbol, side and quantity cannot answer that when two identical orders exist. For
the paper adapter the id round-trips exactly. For the Kotak Neo adapter (Milestone
7) the broker's order book echoes the id back: the ledger records it at placement
(`GuiOrdId` on SDK main builds; the `order_report` book is authoritative in the
published SDK), and every read path matches the book on it before falling back to
a timestamped symbol/side/quantity window match, which is weaker and only used for
orders that never learned a broker id.

**Snapshots are what the broker says, never what we believe.** They are kept apart
from our journal precisely so the two can be compared and found to disagree.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from algo.core.enums import OrderState, Side
from algo.core.order import BrokerOrderRef, Order
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class BrokerOrderSnapshot(BaseModel):
    """One order as the **broker** currently sees it."""

    model_config = _FROZEN

    client_order_id: str
    broker_order_id: str
    instrument_key: str
    side: Side
    lots: int
    state: OrderState
    filled_qty: Decimal = Decimal("0")
    average_price: Decimal | None = None
    message: str = ""
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class BrokerPositionSnapshot(BaseModel):
    """One position as the broker currently sees it. Signed: negative is short."""

    model_config = _FROZEN

    instrument_key: str
    qty: Decimal
    lots: int
    average_price: Decimal


class BrokerFillSnapshot(BaseModel):
    """One execution as the broker reports it."""

    model_config = _FROZEN

    fill_id: str
    client_order_id: str
    broker_order_id: str
    instrument_key: str
    side: Side
    lots: int
    qty: Decimal
    price: Decimal
    ts: datetime

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class Funds(BaseModel):
    model_config = _FROZEN

    cash: Decimal
    margin_used: Decimal = Decimal("0")
    margin_available: Decimal = Decimal("0")


class BrokerHealth(BaseModel):
    model_config = _FROZEN

    connected: bool
    last_heartbeat: datetime | None = None
    detail: str = ""


@runtime_checkable
class Broker(Protocol):
    """What the router needs from any venue, real or simulated."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def place(self, order: Order) -> BrokerOrderRef: ...

    def cancel(self, client_order_id: str) -> None: ...

    def open_orders(self) -> list[BrokerOrderSnapshot]: ...

    def order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        """Look up one order by **our** id.

        The method reconciliation actually depends on. Without it, answering
        "did my order arrive?" means guessing from symbol and quantity, which
        cannot distinguish two identical orders — exactly the case where getting
        it wrong duplicates a position.
        """
        ...

    def positions(self) -> list[BrokerPositionSnapshot]: ...

    def executions(self, since: datetime) -> list[BrokerFillSnapshot]: ...

    def funds(self) -> Funds: ...

    def health(self) -> BrokerHealth: ...
