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

#: A two-sided book wider than this percentage of its own mid is treated as no
#: book at all (`QuoteFlag.TOO_WIDE`).
#:
#: Answers Q17. Every other check in `status` asks whether a quote *exists*;
#: none asked how wide it was, so a row at bid 76.5 / ask 884.5 passed as
#: tradeable, and the IV solver inverted its 480.5 mid into a delta of 0.150 —
#: landing it squarely on the strategy's own selling target, ahead of both real
#: neighbours. Selling that strike live would have collected 76.5 while the
#: backtest recorded ~480.
#:
#: 10 is chosen from the data rather than by feel: on a real GOLDM scrape the
#: genuine near-the-money book runs 0.3-1.5% of mid and the fabricated rows run
#: 50%+, so anything in the 5-15% band separates them cleanly. Signed off by the
#: operator. It is deliberately a *rejection*, never a widening — an untradeable
#: row is the truthful outcome, and D-005 forbids substituting a plausible one.
DEFAULT_MAX_SPREAD_PCT = Decimal("10")


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

    @property
    def spread_pct(self) -> Decimal | None:
        """Spread as a percentage of mid, or None when the book is one-sided.

        Relative rather than absolute on purpose: 5 points is a tight book on a
        2000-rupee option and a nonsense one on a 20-rupee option, so an absolute
        threshold would have to be re-tuned for every strike in the ladder.
        """
        mid = self.mid
        if mid is None or mid <= 0 or self.spread is None:
            return None
        return self.spread / mid * Decimal("100")

    def status(
        self,
        *,
        stale_after_s: float | None = None,
        max_spread_pct: Decimal | None = DEFAULT_MAX_SPREAD_PCT,
    ) -> QuoteFlag:
        """Classify tradeability. Brief §6 — never fill against a phantom price.

        `max_spread_pct` defaults to a real bound rather than to None (as
        `stale_after_s` does) because staleness needs a policy number this model
        cannot know, whereas a book wider than its own mid is not a book on any
        policy. Pass None to disable the check for a caller that genuinely wants
        the raw classification.
        """
        if self.bid is None or self.ask is None:
            return QuoteFlag.EMPTY_BOOK
        if self.bid <= 0 or self.ask <= 0:
            return QuoteFlag.NON_POSITIVE
        if self.bid > self.ask:
            return QuoteFlag.CROSSED
        # Exactly zero, not falsy: `None` means the feed never reported open
        # interest, which is not evidence that nobody holds the contract.
        if self.open_interest == 0:
            return QuoteFlag.NO_OPEN_INTEREST
        if max_spread_pct is not None:
            spread_pct = self.spread_pct
            if spread_pct is not None and spread_pct > max_spread_pct:
                return QuoteFlag.TOO_WIDE
        if stale_after_s is not None:
            age = (self.received_ts - self.exchange_ts).total_seconds()
            if age > stale_after_s:
                return QuoteFlag.STALE
        return QuoteFlag.OK

    @property
    def is_tradeable(self) -> bool:
        return self.status() is QuoteFlag.OK
