"""Margin models.

Margin matters here for one specific reason: the configured exits are **2% and 1%
of margin blocked** (Q4a). So the margin number does not merely constrain the
position, it *sets the stop*. A margin model that is 30% wrong moves the stop by
30%, which changes the strategy rather than merely mis-reporting it.

Nothing here is SPAN. Real SPAN margin comes from a scenario grid the exchange
publishes and revises, and approximating it from a percentage of notional is a
placeholder — an honest one, but a placeholder. Every model reports
`is_calibrated`, and an uncalibrated margin propagates a warning onto the run,
because a stop derived from a guess should not look like a stop derived from a
broker quote.

Replaced at Milestone 7 by margin the broker actually reports (open question Q18).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from algo.core.errors import DomainError
from algo.core.money import quantize_paisa


class MarginModel(Protocol):
    """Margin blocked for a position, in rupees."""

    def margin_for(self, *, notional: Decimal, lots: int, is_short_option: bool) -> Decimal: ...

    @property
    def is_calibrated(self) -> bool:
        """False while the model is an approximation rather than a broker figure."""
        ...


class SpanApproxMargin:
    """Margin as a percentage of notional. A placeholder, and labelled as one.

    Short options attract a higher rate than futures, reflecting that a naked
    short carries open-ended risk while a future does not. Both percentages are
    guesses until checked against real broker margin quotes.
    """

    __slots__ = ("_futures_pct", "_short_option_pct")

    def __init__(
        self,
        *,
        futures_pct: Decimal = Decimal("6"),
        short_option_pct: Decimal = Decimal("8"),
    ) -> None:
        if futures_pct <= 0 or short_option_pct <= 0:
            raise DomainError("margin percentages must be positive")
        self._futures_pct = futures_pct
        self._short_option_pct = short_option_pct

    def margin_for(self, *, notional: Decimal, lots: int, is_short_option: bool) -> Decimal:
        del lots  # notional already scales with lots
        rate = self._short_option_pct if is_short_option else self._futures_pct
        return quantize_paisa(abs(notional) * rate / Decimal("100"))

    @property
    def is_calibrated(self) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            f"SpanApproxMargin(futures={self._futures_pct}%, "
            f"short_option={self._short_option_pct}%)"
        )


class FixedMarginPerLot:
    """A flat rupee figure per lot.

    Use this when a real broker margin quote is available for the position size
    being traded — it is cruder than a percentage model but, given an actual
    quote, it is *right* rather than merely plausible. Pass `calibrated=True` only
    when the number came from the broker.
    """

    __slots__ = ("_calibrated", "_per_lot")

    def __init__(self, per_lot: Decimal, *, calibrated: bool = False) -> None:
        if per_lot <= 0:
            raise DomainError(f"margin per lot must be positive, got {per_lot}")
        self._per_lot = per_lot
        self._calibrated = calibrated

    def margin_for(self, *, notional: Decimal, lots: int, is_short_option: bool) -> Decimal:
        del notional, is_short_option
        return self._per_lot * Decimal(lots)

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated

    def __repr__(self) -> str:
        return f"FixedMarginPerLot({self._per_lot}, calibrated={self._calibrated})"
