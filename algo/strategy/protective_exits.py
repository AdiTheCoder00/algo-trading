"""The flat stop and the trailing profit stop, orchestrated as one unit.

`price_stop.py` and `trailing_profit_stop.py` are the primitives - each answers
a single question about a single bar. This module is the *sequencing* of those
two, which is the part both CFD strategies have to agree on exactly: advance
the trail, test the flat stop, test the trail, and describe whichever fired in
a form the caller can act on.

`price_stop.py`'s own docstring already states why that sequencing belongs
here rather than in each strategy - it is "the shared, tested piece that adds
it identically to both rather than two copies that could quietly drift apart."
The primitives were shared from the start; the 42 lines of orchestration over
them were copy-pasted into `MacdCrossover` and `TrendlineBreakout`
byte-for-byte, which is precisely the drift that sentence warns about. This
module deletes the second copy.

## The order is fixed here, not per-strategy

The flat stop is tested before the trail, so a bar that crosses both is
reported as the stop - the same pessimistic reading `algo/risk/exits.py`
already states for the MCX path ("if a bar moved far enough to touch both ...
the stop went first"). The trail's peak advances *before* either test, because
a real trailing-stop order moves the instant a new best price prints; see
`trailing_profit_stop.py`'s docstring on that convention.

## What the caller still owns

This module builds no `Signal` and knows nothing about instruments. It reports
*what* should happen and *why*; the strategy turns that into a signal with its
own ids, legs and context. So the two strategies stay free to describe
themselves differently while agreeing exactly on when a protective exit fires.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from algo.core.bar import Bar
from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.core.position import Position
from algo.strategy.price_stop import stop_touched
from algo.strategy.trailing_profit_stop import (
    TrailState,
    advance_trail,
    start_trail,
    trail_touched,
)

ExitKind = Literal["stop", "trail"]
"""Which protective exit fired.

Travels into `Signal.context["exit"]`, so a consumer can branch on it without
parsing the human-readable `reason`. `scripts/measure_macd_xauusd.py` needs
exactly this: a stop-triggered close fills at the stop level, not at
`bar.close +/- spread`, and deciding that from a prose prefix made every
reported P&L figure depend on the wording of a log message.
"""


@dataclass(frozen=True)
class ExitDecision:
    """One protective exit that fired on one bar."""

    kind: ExitKind
    side: Side
    """The side that *closes* the held position, not the side it was opened on."""
    reason: str


class ProtectiveExits:
    """A flat percentage stop and a trailing profit stop, checked in that order.

    Owns the trail's running state, so it is stateful and must not be shared
    between two strategy instances - each strategy constructs its own, exactly
    as it would have owned its own `TrailState` before.
    """

    def __init__(
        self,
        *,
        stop_loss_pct: Decimal,
        trail_activation_pct: Decimal,
        trail_pct: Decimal,
    ) -> None:
        if stop_loss_pct < 0:
            raise DomainError(f"stop_loss_pct cannot be negative, got {stop_loss_pct}")
        if trail_activation_pct < 0:
            raise DomainError(
                f"trail_activation_pct cannot be negative, got {trail_activation_pct}"
            )
        if trail_pct < 0:
            raise DomainError(f"trail_pct cannot be negative, got {trail_pct}")
        self._stop_loss_pct = stop_loss_pct
        self._trail_activation_pct = trail_activation_pct
        self._trail_pct = trail_pct
        self._trail: TrailState | None = None

    def params(self) -> dict[str, str]:
        """The three tunables, in the exact keys both strategies already publish.

        These feed `Strategy.params_hash()` and from there every `signal_id`, so
        renaming a key here silently renumbers history. Merged into each
        strategy's own `params()` rather than replacing it.
        """
        return {
            "stop_loss_pct": str(self._stop_loss_pct),
            "trail_activation_pct": str(self._trail_activation_pct),
            "trail_pct": str(self._trail_pct),
        }

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, str]:
        """The armed trail's running peak, or nothing when no trail is live.

        The flat stop is path-independent - it is recomputed from the held
        position's own entry every bar - so it has nothing to persist.
        """
        if self._trail is None:
            return {}
        return {
            "trail_side": self._trail.side.value,
            "trail_entry": str(self._trail.entry_price),
            "trail_peak": str(self._trail.peak),
        }

    def restore(self, state: Mapping[str, str]) -> None:
        """Rebuild the trail from `state`. Absent trail keys are a cold start."""
        raw_trail_side = state.get("trail_side", "").strip()
        if not raw_trail_side:
            self._trail = None
            return
        try:
            self._trail = TrailState(
                side=Side(raw_trail_side),
                entry_price=Decimal(state["trail_entry"]),
                peak=Decimal(state["trail_peak"]),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            # A half-restored trail would arm against a peak the position never
            # actually reached. `InvalidOperation` is in the tuple because that
            # is what `Decimal("garbage")` raises - it is an `ArithmeticError`,
            # not a `ValueError`, so the two copies this replaces would both
            # have crashed with a raw decimal error instead of this message.
            raise DomainError(
                f"cannot restore trailing-stop state from {dict(state)!r}: {exc}. "
                "Refusing to run with a partially-restored trail."
            ) from exc

    # ------------------------------------------------------------------ logic
    def check(self, bar: Bar, held: Position | None) -> ExitDecision | None:
        """Advance the trail for `bar`, then report the first exit that fired.

        Call this every bar, before the strategy's own entry logic *and* before
        its warmup gate - a held position must never go unprotected because the
        indicator that would eventually close it has not converged yet.

        Returns `None` when nothing fired, including whenever flat, which also
        clears any trail so the next position starts one of its own.
        """
        if held is None or held.is_flat:
            self._trail = None
            return None

        side = Side.BUY if held.qty > 0 else Side.SELL
        # Re-seed on a change of side *or* of entry price. Neither strategy can
        # currently reopen without a flat bar in between - each emits at most
        # one signal per bar - so in practice only the side check ever fires;
        # the entry-price check states that assumption instead of depending on
        # it silently, since a stale peak would arm a trail against a price the
        # current position never traded at.
        if (
            self._trail is None
            or self._trail.side is not side
            or self._trail.entry_price != held.average_price
        ):
            self._trail = start_trail(held.average_price, side)
        self._trail = advance_trail(self._trail, bar)

        closing_side = Side.SELL if held.qty > 0 else Side.BUY

        if stop_touched(bar, held, self._stop_loss_pct):
            return ExitDecision(
                kind="stop",
                side=closing_side,
                reason=(
                    f"stop loss: {self._stop_loss_pct}% move against a "
                    f"{held.side} position of {held.lots} lot(s), entry "
                    f"{held.average_price:.2f}"
                ),
            )

        if trail_touched(self._trail, bar, self._trail_activation_pct, self._trail_pct):
            return ExitDecision(
                kind="trail",
                side=closing_side,
                reason=(
                    f"trailing stop: {self._trail_pct}% pullback from a peak of "
                    f"{self._trail.peak:.2f} (armed at {self._trail_activation_pct}% "
                    f"profit) on a {held.side} position of {held.lots} lot(s), "
                    f"entry {held.average_price:.2f}"
                ),
            )

        return None
