"""The kill switch. Brief §2.2.

    "A single command halts new orders and optionally flattens all positions. A
     daily loss limit and a max-consecutive-losses limit trip it automatically."

Three automatic trips: daily loss against **start-of-day** equity, consecutive
losing trades, and drawdown from the equity peak. Any one of them halts new
orders.

Decision D-012: a trip **halts by default, it does not flatten**. Market-closing a
short strangle during the fast move that tripped the limit can cost more than the
breach did — the position that just gapped against you is the one whose exit
liquidity is worst. Flattening is available and explicit, never the default.

State is designed to survive a crash: `KillSwitchState` is a plain frozen model
that serialises to JSON, so the persisted value is inspectable by a human at three
in the morning. A kill switch whose state is lost on restart is not a kill switch.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from algo.core.timeutil import ensure_utc


class TripReason(StrEnum):
    DAILY_LOSS = "DAILY_LOSS"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    MANUAL = "MANUAL"


class KillSwitchState(BaseModel):
    """Everything needed to resume after a restart, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tripped: bool = False
    reason: TripReason | None = None
    detail: str = ""
    tripped_at: datetime | None = None

    session_date: date | None = None
    start_of_day_equity: Decimal | None = None
    peak_equity: Decimal | None = None
    consecutive_losses: int = 0


class KillSwitch:
    """Watches equity and trade outcomes; halts when a limit is breached."""

    __slots__ = ("_daily_loss_pct", "_max_consecutive", "_max_drawdown_pct", "_state")

    def __init__(
        self,
        *,
        daily_loss_limit_pct: Decimal,
        max_consecutive_losses: int,
        max_drawdown_pct: Decimal,
        state: KillSwitchState | None = None,
    ) -> None:
        self._daily_loss_pct = daily_loss_limit_pct
        self._max_consecutive = max_consecutive_losses
        self._max_drawdown_pct = max_drawdown_pct
        self._state = state or KillSwitchState()

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def is_tripped(self) -> bool:
        return self._state.tripped

    def allows_new_orders(self) -> bool:
        return not self._state.tripped

    # ---------------------------------------------------------------- updates
    def start_session(self, session_date: date, equity: Decimal) -> None:
        """Reset the daily baseline.

        Measured against start-of-day equity rather than current or peak, so an
        account that recovers intraday does not silently raise its own loss limit.
        """
        self._state = self._state.model_copy(
            update={
                "session_date": session_date,
                "start_of_day_equity": equity,
                "peak_equity": max(self._state.peak_equity or equity, equity),
            }
        )

    def observe_equity(self, equity: Decimal, at: datetime) -> TripReason | None:
        """Check the equity-based limits. Returns the reason if this call tripped it."""
        if self._state.tripped:
            return None

        peak = max(self._state.peak_equity or equity, equity)
        self._state = self._state.model_copy(update={"peak_equity": peak})

        start = self._state.start_of_day_equity
        if start is not None and start > 0:
            loss_pct = (start - equity) / start * Decimal("100")
            if loss_pct >= self._daily_loss_pct:
                return self._trip(
                    TripReason.DAILY_LOSS,
                    f"down {loss_pct:.4f}% on the session against a {self._daily_loss_pct}% "
                    f"limit (start {start}, now {equity})",
                    at,
                )

        if peak > 0:
            drawdown_pct = (peak - equity) / peak * Decimal("100")
            if drawdown_pct >= self._max_drawdown_pct:
                return self._trip(
                    TripReason.MAX_DRAWDOWN,
                    f"drawdown {drawdown_pct:.4f}% from a peak of {peak} against a "
                    f"{self._max_drawdown_pct}% limit",
                    at,
                )
        return None

    def observe_trade(self, net_pnl: Decimal, at: datetime) -> TripReason | None:
        """Record a completed round trip and check the losing-streak limit."""
        if self._state.tripped:
            return None
        streak = self._state.consecutive_losses + 1 if net_pnl < 0 else 0
        self._state = self._state.model_copy(update={"consecutive_losses": streak})
        if streak >= self._max_consecutive:
            return self._trip(
                TripReason.CONSECUTIVE_LOSSES,
                f"{streak} losing trades in a row against a limit of {self._max_consecutive}",
                at,
            )
        return None

    def trip_manually(self, detail: str, at: datetime) -> TripReason:
        """The single command from §2.2."""
        return self._trip(TripReason.MANUAL, detail, at)

    def reset(self) -> None:
        """Clear a trip. Deliberately explicit — nothing resets this automatically,
        because a kill switch that un-trips itself overnight has not stopped
        anything."""
        self._state = KillSwitchState(
            session_date=self._state.session_date,
            start_of_day_equity=self._state.start_of_day_equity,
            peak_equity=self._state.peak_equity,
        )

    def _trip(self, reason: TripReason, detail: str, at: datetime) -> TripReason:
        self._state = self._state.model_copy(
            update={
                "tripped": True,
                "reason": reason,
                "detail": detail,
                "tripped_at": ensure_utc(at),
            }
        )
        return reason
