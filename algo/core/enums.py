"""Closed vocabularies shared across every layer.

`StrEnum` so that serialised values are readable in the trade log and stable
across runs — an integer enum would make golden files unreadable and would
silently reorder if a member were inserted.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Brief §2.1. `LIVE` is never reachable by default; see config.modes."""

    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Exchange(StrEnum):
    MCX = "MCX"
    NSE = "NSE"
    NFO = "NFO"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Used for signed position quantities."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Right(StrEnum):
    """Option right. MCX uses CE/PE, matching the Indian convention."""

    CE = "CE"
    PE = "PE"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class ProductType(StrEnum):
    """Broker product code. NRML carries overnight; INTRADAY is squared off."""

    NRML = "NRML"
    INTRADAY = "INTRADAY"


class OrderState(StrEnum):
    """Lifecycle. `JOURNALLED` exists before the broker has ever seen the order —
    it is what makes crash recovery possible (write-ahead, then send)."""

    JOURNALLED = "JOURNALLED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


class SignalAction(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    MODIFY = "MODIFY"


class Atomicity(StrEnum):
    """How a multi-leg signal must be executed.

    ALL_OR_NONE: if any leg fails, already-filled legs are closed immediately.
    A half-filled strangle is a naked short option, which is a different
    instrument of risk entirely.
    """

    ALL_OR_NONE = "ALL_OR_NONE"
    BEST_EFFORT = "BEST_EFFORT"


class RejectReason(StrEnum):
    """Why the risk engine declined. Every rejection is logged with one of these."""

    BELOW_MIN_LOTS = "BELOW_MIN_LOTS"
    ABOVE_MAX_LOTS = "ABOVE_MAX_LOTS"
    MAX_CONCURRENT = "MAX_CONCURRENT"
    MARGIN_CAP = "MARGIN_CAP"
    KILL_SWITCH_TRIPPED = "KILL_SWITCH_TRIPPED"
    DEVOLVEMENT_WINDOW = "DEVOLVEMENT_WINDOW"
    TENDER_WINDOW = "TENDER_WINDOW"
    UNTRADEABLE_QUOTE = "UNTRADEABLE_QUOTE"
    SPEC_VIOLATION = "SPEC_VIOLATION"
    STOP_BELOW_COST = "STOP_BELOW_COST"


class QuoteFlag(StrEnum):
    """Why a chain row is not tradeable. Brief §6 — the engine must never fill
    against a phantom price."""

    OK = "OK"
    EMPTY_BOOK = "EMPTY_BOOK"
    CROSSED = "CROSSED"
    STALE = "STALE"
    ZERO_VOLUME = "ZERO_VOLUME"
    NON_POSITIVE = "NON_POSITIVE"
    CIRCUIT_LOCKED = "CIRCUIT_LOCKED"
