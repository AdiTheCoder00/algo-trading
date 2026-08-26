"""Orders — broker instructions with a lot size attached.

Produced by the risk layer from a signal, never by a strategy. The distinction is
enforced by the type system: a strategy returns `Signal`, and nothing in the
strategy layer can construct an `Order` because nothing there knows `lots`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from algo.core.enums import OrderType, ProductType, Side, TimeInForce
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Order(BaseModel):
    """A single broker instruction.

    `qty` is carried alongside `lots` rather than derived on the fly, because the
    lot size in force can change between the moment an order is written to the
    journal and the moment a crash-recovery replays it. Freezing both means a
    recovered order is the order that was intended, not a recomputation.
    """

    model_config = _FROZEN

    client_order_id: str
    signal_id: str
    instrument: InstrumentId
    side: Side
    lots: int
    qty: Decimal
    order_type: OrderType
    product: ProductType = ProductType.NRML
    tif: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    created_at: datetime
    slice_of: str | None = None

    @field_validator("created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @field_validator("lots")
    @classmethod
    def _positive_lots(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"order lots must be >= 1, got {v}")
        return v

    @model_validator(mode="after")
    def _price_matches_type(self) -> Order:
        needs_limit = self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
        if needs_limit and self.limit_price is None:
            raise ValueError(f"{self.order_type} requires a limit price")
        if not needs_limit and self.limit_price is not None:
            raise ValueError(f"{self.order_type} must not carry a limit price")
        needs_trigger = self.order_type in (OrderType.STOP_LIMIT, OrderType.STOP_MARKET)
        if needs_trigger and self.trigger_price is None:
            raise ValueError(f"{self.order_type} requires a trigger price")
        if not needs_trigger and self.trigger_price is not None:
            raise ValueError(f"{self.order_type} must not carry a trigger price")
        return self


class BrokerOrderRef(BaseModel):
    """The broker's own handle for an order we sent.

    Kept separate from `client_order_id` so reconciliation can always answer both
    "what did we intend" and "what does the broker think", and notice when they
    disagree.
    """

    model_config = _FROZEN

    client_order_id: str
    broker_order_id: str
    accepted_at: datetime

    @field_validator("accepted_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)


