"""Devolvement and tender guards. Decision D-016 — hard rules, not configuration.

An in-the-money short leg left at MCX option expiry **devolves into a GOLDM
futures position at the strike**, and GOLDM futures go to compulsory physical
delivery of 100 g of gold. That is not a P&L event to be sized against; it is a
delivery obligation.

So there is deliberately no `enabled: false` here. The configuration tunes *when*
the guards fire, never *whether*. Two rules:

1. No short option may be carried into its expiry session.
2. No futures position — however acquired, including by devolvement — may be
   carried into the tender period.

Both are enforced identically in backtest, paper and live, and a test proves that
**disabling the rule is what produces the obligation** — so the rule is
demonstrably what prevents it, rather than luck.

The dates come from `ExpirySet`, which carries option expiry, futures expiry and
tender-period start as three separate fields. They are not derived from one
another, because deriving one from another is exactly the error that produced
C-004.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from algo.core.enums import RejectReason
from algo.core.errors import DomainError
from algo.exchange.calendar import MarketCalendar
from algo.exchange.expiries import ExpirySet


@dataclass(frozen=True, slots=True)
class DevolvementVerdict:
    """Why the guard acted, in words fit for a log line and a trade record."""

    reason: RejectReason
    detail: str


class DevolvementGuard:
    """Decides when a position must be closed, and when none may be opened."""

    __slots__ = ("_block_entry_dte", "_calendar", "_force_exit_sessions")

    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        force_exit_sessions_before_expiry: int = 1,
        block_new_entries_within_dte: int = 2,
    ) -> None:
        if force_exit_sessions_before_expiry < 1:
            raise DomainError(
                "force_exit_sessions_before_expiry must be at least 1 — carrying a short "
                "option into its own expiry session is the scenario this guard exists for"
            )
        if block_new_entries_within_dte < 0:
            raise DomainError("block_new_entries_within_dte cannot be negative")
        self._calendar = calendar
        self._force_exit_sessions = force_exit_sessions_before_expiry
        self._block_entry_dte = block_new_entries_within_dte

    # ------------------------------------------------------------------ dates
    def expiry_session(self, expiry: ExpirySet) -> date:
        """The session on which the option actually expires.

        If the listed expiry date is a holiday the contract expires on the
        preceding session, so the guard must work from the session rather than
        from the calendar date.
        """
        return self._calendar.previous_trading_day(expiry.option_expiry, inclusive=True)

    def exit_deadline(self, expiry: ExpirySet) -> date:
        """The session on which the position must be **closed**.

        Not the last session it may be held — the session the exit is placed
        on. With `force_exit_sessions_before_expiry = 1` and a Friday expiry,
        that is the Thursday: the order goes in on Thursday and fills on
        Thursday, leaving the account flat before the expiry session opens.

        The off-by-one here is not cosmetic. Reading it as "the last day you
        may hold" leaves a short option open through its own expiry session,
        which is precisely the state that devolves.
        """
        session = self.expiry_session(expiry)
        for _ in range(self._force_exit_sessions):
            session = self._calendar.previous_trading_day(session)
        return session

    def tender_deadline(self, expiry: ExpirySet) -> date | None:
        """The session on which a futures position must be closed."""
        if expiry.tender_period_start is None:
            return None
        return self._calendar.previous_trading_day(expiry.tender_period_start)

    # ----------------------------------------------------------------- checks
    def blocks_entry(self, expiry: ExpirySet, on: date) -> DevolvementVerdict | None:
        """Whether a new position may be opened on `on`."""
        dte = expiry.days_to_option_expiry(on)
        if dte <= self._block_entry_dte:
            return DevolvementVerdict(
                reason=RejectReason.DEVOLVEMENT_WINDOW,
                detail=(
                    f"{dte} days to option expiry ({expiry.option_expiry}); "
                    f"new entries are blocked within {self._block_entry_dte}"
                ),
            )
        return None

    def requires_option_exit(self, expiry: ExpirySet, on: date) -> DevolvementVerdict | None:
        """Whether an open short option must be closed on `on`, no matter what.

        This overrides strategy intent, take-profit levels and stop levels alike.
        Nothing outranks it.
        """
        if on >= self.exit_deadline(expiry):
            return DevolvementVerdict(
                reason=RejectReason.DEVOLVEMENT_WINDOW,
                detail=(
                    f"{on} is past the exit deadline of {self.exit_deadline(expiry)} "
                    f"for the {expiry.option_expiry} expiry - an in-the-money short leg "
                    "would devolve into a GOLDM futures position bound for physical delivery"
                ),
            )
        return None

    def requires_futures_exit(self, expiry: ExpirySet, on: date) -> DevolvementVerdict | None:
        """Whether an open futures position must be closed before the tender period."""
        deadline = self.tender_deadline(expiry)
        if deadline is None:
            return None
        if on >= deadline:
            return DevolvementVerdict(
                reason=RejectReason.TENDER_WINDOW,
                detail=(
                    f"{on} is past the tender deadline of {deadline}; the tender period "
                    f"for the underlying opens {expiry.tender_period_start} and GOLDM "
                    "futures settle by compulsory physical delivery"
                ),
            )
        return None

    def days_until_forced_exit(self, expiry: ExpirySet, on: date) -> int:
        """Countdown surfaced to monitoring, so the deadline is never a surprise."""
        return (self.exit_deadline(expiry) - on).days
