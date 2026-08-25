"""Combo-level exits, and the check that the stop is bigger than the cost of trading.

Brief §8 puts stop and target logic here rather than in the strategy, and for a
strangle they have to be *combo* level: the position is two legs whose P&L only
means anything together. A per-leg stop on a strangle closes the winning side and
leaves the losing one open, which is the opposite of what a stop is for.

Two decisions carry through this module.

**D-025 — levels are resolved once, at entry, and frozen.** Take profit is 2% of
margin blocked; stop loss is 1%. Both become absolute rupee figures the moment the
position opens. A level that floated with live equity would make the same trade
exit at a different price because of unrelated P&L elsewhere in the account.

**D-024 — the stop is compared against the round-trip cost.** On the margin basis
the stop lands near ₹1,000 a lot while round-trip friction on a thin option book
plausibly runs ₹500–1,500. A position that opens at its own stop is not a
strategy. The check defaults to `warn` rather than `refuse`, because the basis was
chosen with that arithmetic in view — the engine's job is to report, not to veto.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from algo.core.errors import DomainError
from algo.core.signal import ComboExit


class ExitReason(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"
    FORCED_PRE_EXPIRY = "FORCED_PRE_EXPIRY"
    KILL_SWITCH = "KILL_SWITCH"
    END_OF_RUN = "END_OF_RUN"


@dataclass(frozen=True, slots=True)
class ExitLevels:
    """Absolute rupee levels for one open position, fixed at entry."""

    take_profit: Decimal
    stop_loss: Decimal
    margin_at_entry: Decimal
    equity_at_entry: Decimal
    credit_at_entry: Decimal
    basis: str

    def check(self, combo_pnl: Decimal) -> ExitReason | None:
        """Evaluate open P&L against the frozen levels.

        The stop is tested first. If a bar moved far enough to touch both — which
        on a gap it can — the pessimistic reading is that the stop went first, and
        that is the same assumption the fill simulator makes for intrabar order
        (brief §6). Being consistent about it matters more than which one is
        chosen.
        """
        if combo_pnl <= -self.stop_loss:
            return ExitReason.STOP_LOSS
        if combo_pnl >= self.take_profit:
            return ExitReason.TAKE_PROFIT
        return None


@dataclass(frozen=True, slots=True)
class ViabilityCheck:
    """Whether the stop is large enough to survive the cost of entering and exiting."""

    stop: Decimal
    round_trip_cost: Decimal
    ratio: Decimal | None
    threshold: Decimal
    passes: bool

    def message(self) -> str:
        if self.ratio is None:
            return (
                f"stop {self.stop} vs round-trip cost {self.round_trip_cost}: "
                "cost is zero, nothing to compare"
            )
        verdict = "clears" if self.passes else "BELOW"
        return (
            f"stop {self.stop} is {self.ratio:.2f}x the round-trip cost of "
            f"{self.round_trip_cost} - {verdict} the {self.threshold}x threshold"
        )


def resolve_levels(
    *,
    take_profit: ComboExit,
    stop_loss: ComboExit,
    margin: Decimal,
    equity: Decimal,
    credit: Decimal,
) -> ExitLevels:
    """Turn percentage exits into absolute rupee levels, once."""
    return ExitLevels(
        take_profit=_absolute(take_profit, margin=margin, equity=equity, credit=credit),
        stop_loss=_absolute(stop_loss, margin=margin, equity=equity, credit=credit),
        margin_at_entry=margin,
        equity_at_entry=equity,
        credit_at_entry=credit,
        basis=take_profit.kind,
    )


def _absolute(
    exit_spec: ComboExit, *, margin: Decimal, equity: Decimal, credit: Decimal
) -> Decimal:
    kind = exit_spec.kind
    value = exit_spec.value
    if kind == "PCT_OF_MARGIN_AT_ENTRY":
        base = margin
    elif kind == "PCT_OF_EQUITY_AT_ENTRY":
        base = equity
    elif kind == "PCT_OF_CREDIT":
        base = credit
    elif kind == "MULTIPLE_OF_CREDIT":
        return credit * value
    elif kind == "ABS_INR":
        return value
    else:
        raise DomainError(
            f"exit kind {kind} cannot be resolved to a rupee level here; "
            "DELTA_BREACH and UNDERLYING_MOVE_PCT are evaluated separately"
        )
    if base <= 0:
        raise DomainError(f"cannot resolve {kind}: base is {base}")
    return base * value / Decimal("100")


def check_viability(
    *, stop: Decimal, round_trip_cost: Decimal, threshold: Decimal
) -> ViabilityCheck:
    """Decision D-024. Compare the stop against what it costs to trade at all."""
    if round_trip_cost <= 0:
        return ViabilityCheck(
            stop=stop,
            round_trip_cost=round_trip_cost,
            ratio=None,
            threshold=threshold,
            passes=True,
        )
    ratio = stop / round_trip_cost
    return ViabilityCheck(
        stop=stop,
        round_trip_cost=round_trip_cost,
        ratio=ratio,
        threshold=threshold,
        passes=ratio >= threshold,
    )
