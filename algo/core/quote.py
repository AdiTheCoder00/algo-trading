"""Quotes and order-book depth.

Decision D-020: the recorder captures depth, not just candles. On a thin book the
spread *is* the strategy's cost, and it cannot be reconstructed from OHLC after
the fact. These models are what gets written to parquet, so their shape decides
what will be answerable in six months.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from algo.core.enums import QuoteFlag
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class DepthLevel(BaseModel):
    model_config = _FROZEN

    price: Decimal
    quantity: int
    orders: int | None = None


class Quote(BaseModel):
    """Top-of-book plus the last trade, at one instant.

    `exchange_ts` and `received_ts` are both kept so feed latency is measurable
    rather than assumed — an assumption about latency is a silent input to every
    fill decision.
    """

    model_config = _FROZEN

    exchange_ts: datetime
    received_ts: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    ltp: Decimal | None = None
    volume: int = 0
    open_interest: int | None = None
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()

    @field_validator("exchange_ts", "received_ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @property
    def mid(self) -> Decimal | None:
        """Mid price, or None when the book is one-sided.

        Deliberately returns None rather than falling back to the LTP. A stale
        last trade dressed up as a mid is how a backtest ends up marking a
        position at a price nobody was showing.
        """
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    def status(self, *, stale_after_s: float | None = None) -> QuoteFlag:
        """Classify tradeability. Brief §6 — never fill against a phantom price."""
        if self.bid is None or self.ask is None:
            return QuoteFlag.EMPTY_BOOK
        if self.bid <= 0 or self.ask <= 0:
            return QuoteFlag.NON_POSITIVE
        if self.bid > self.ask:
            return QuoteFlag.CROSSED
        if stale_after_s is not None:
            age = (self.received_ts - self.exchange_ts).total_seconds()
            if age > stale_after_s:
                return QuoteFlag.STALE
        return QuoteFlag.OK

    @property
    def is_tradeable(self) -> bool:
        return self.status() is QuoteFlag.OK
