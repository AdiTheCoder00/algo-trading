"""The risk layer: turns intent into instructions, or refuses.

This is the only place that knows about lots, equity and margin. A strategy hands
over a `Signal` and gets nothing back — it never learns whether the signal was
acted on, how large, or why not. That asymmetry is what keeps sizing out of
strategy code, and it is enforced by types rather than by discipline: nothing in
the strategy layer holds a reference that could construct an `Order`.

Every refusal carries a named reason and is logged. Brief §8: a size that rounds
below the minimum lot is a skipped trade with a log line, never a rounded-up one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from algo.core.enums import OrderType, ProductType, RejectReason, TimeInForce
from algo.core.ids import client_order_id
from algo.core.instrument import InstrumentSpec
from algo.core.money import round_down_to_lot_step
from algo.core.order import Order
from algo.core.signal import Signal


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """Everything the risk layer needs to judge a signal."""

    now: datetime
    session_day: date
    equity: Decimal
    open_position_count: int
    lots_held: int = 0
    margin_used: Decimal = Decimal("0")
    """Margin already blocked, rupees, on the same combo basis as the new leg."""
    propose_margin: Callable[[int], Decimal] | None = None
    """Given the sized lot count, the margin this signal would block. None when no
    margin model is in play, which disables the MARGIN_CAP check rather than
    guessing."""


@dataclass(frozen=True, slots=True)
class SizingTrace:
    """Every input and intermediate of the sizing calculation, persisted.

    Brief §8 asks for the formula; this records the arithmetic that ran, so any
    lot count in a trade log can be reconstructed months later without rerunning
    anything.
    """

    mode: str
    equity: Decimal
    requested_lots: Decimal
    rounded_lots: int
    min_lots: int
    max_lots: int | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class Accepted:
    orders: tuple[Order, ...]
    sizing: SizingTrace


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: RejectReason
    detail: str


RiskDecision = Accepted | Rejected


class FixedLotSizer:
    """The configured sizing mode: a constant number of lots.

    No risk-based scaling, by explicit choice. Worth stating plainly: a naked
    short option has unbounded loss and fixed lots does not scale exposure down
    as equity falls. The implied risk is reported per trade so the number is at
    least visible.
    """

    __slots__ = ("_lots",)

    def __init__(self, lots: int) -> None:
        self._lots = lots

    def size(self, *, equity: Decimal, spec: InstrumentSpec) -> tuple[int, SizingTrace]:
        requested = Decimal(self._lots)
        rounded = round_down_to_lot_step(requested, 1)
        if spec.max_lots is not None:
            rounded = min(rounded, spec.max_lots)
        return rounded, SizingTrace(
            mode="fixed_lots",
            equity=equity,
            requested_lots=requested,
            rounded_lots=rounded,
            min_lots=spec.min_lots,
            max_lots=spec.max_lots,
        )


class RiskEngine:
    """Sizes signals and applies the caps."""

    __slots__ = (
        "_margin_cap_pct",
        "_max_concurrent",
        "_max_lots_per_underlying",
        "_sizer",
        "_spec_for",
    )

    def __init__(
        self,
        *,
        sizer: FixedLotSizer,
        spec_for: object,
        max_concurrent_positions: int,
        max_lots_per_underlying: int,
        margin_cap_pct: Decimal | None = None,
    ) -> None:
        self._sizer = sizer
        self._spec_for = spec_for
        self._max_concurrent = max_concurrent_positions
        self._max_lots_per_underlying = max_lots_per_underlying
        self._margin_cap_pct = margin_cap_pct

    @property
    def margin_cap_pct(self) -> Decimal | None:
        """`None` when no cap is configured. Read by the engine to report
        margin utilisation to the dashboard - a second accessor rather than a
        second place this percentage is threaded through by hand."""
        return self._margin_cap_pct

    def evaluate(
        self, signal: Signal, snapshot: RiskSnapshot, *, spec: InstrumentSpec
    ) -> RiskDecision:
        for leg in signal.legs:
            if leg.entry.kind != "MARKET":
                # A limit intent is a request the engine cannot honour — the fill
                # simulator only crosses the book. Ignoring it would execute a
                # different order than the strategy asked for (brief §12).
                return Rejected(
                    reason=RejectReason.UNSUPPORTED_LIMIT_INTENT,
                    detail=(
                        f"leg {leg.instrument.key} asked for a {leg.entry.kind} entry "
                        f"at {leg.entry.limit_price}; only market intents are supported"
                    ),
                )

        lots, trace = self._sizer.size(equity=snapshot.equity, spec=spec)

        if lots < spec.min_lots:
            # Brief §8: skip and log. Never round up to reach the minimum.
            return Rejected(
                reason=RejectReason.BELOW_MIN_LOTS,
                detail=(
                    f"sized {trace.requested_lots} lots, which rounds to {lots}, "
                    f"below the minimum of {spec.min_lots}"
                ),
            )

        is_opening = signal.action.value == "OPEN"
        if is_opening and snapshot.open_position_count >= self._max_concurrent:
            return Rejected(
                reason=RejectReason.MAX_CONCURRENT,
                detail=(
                    f"{snapshot.open_position_count} positions open, "
                    f"cap is {self._max_concurrent}"
                ),
            )
        if is_opening and snapshot.lots_held + lots > self._max_lots_per_underlying:
            return Rejected(
                reason=RejectReason.ABOVE_MAX_LOTS,
                detail=(
                    f"{snapshot.lots_held} lots held plus {lots} exceeds "
                    f"the cap of {self._max_lots_per_underlying}"
                ),
            )
        if (
            is_opening
            and self._margin_cap_pct is not None
            and snapshot.propose_margin is not None
        ):
            cap = snapshot.equity * self._margin_cap_pct / Decimal("100")
            proposed = snapshot.propose_margin(lots)
            if snapshot.margin_used + proposed > cap:
                return Rejected(
                    reason=RejectReason.MARGIN_CAP,
                    detail=(
                        f"{snapshot.margin_used} margin already blocked plus "
                        f"{proposed} for this position exceeds the cap of {cap} "
                        f"({self._margin_cap_pct}% of equity)"
                    ),
                )

        orders = tuple(
            Order(
                client_order_id=client_order_id(
                    strategy_id=signal.strategy_id, sig_id=signal.signal_id, leg_ix=index
                ),
                signal_id=signal.signal_id,
                instrument=leg.instrument,
                side=leg.direction,
                lots=lots * leg.ratio,
                qty=Decimal(lots * leg.ratio),
                order_type=OrderType.MARKET,
                product=ProductType.NRML,
                tif=TimeInForce.DAY,
                created_at=signal.ts,
            )
            for index, leg in enumerate(signal.legs)
        )
        return Accepted(orders=orders, sizing=trace)
