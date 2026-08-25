"""Expiry resolution.

Decision D-023, and the reason for it: a derived expiry rule was wrong once
already in this project. The system therefore treats expiry dates as **data to be
read**, not a formula to be evaluated. The instrument master is the source of
truth; the rule is a cross-check that raises an alarm when the two disagree.

The confirmed rule for GOLDM options is the **last Friday of the contract month**
(verified against the live chain: 28 Aug 2026 is a Friday and the last Friday of
August). It is implemented here as `LastFridayRule` — and used only to check the
authoritative dates, never to replace them.

Getting this wrong is not a rounding error. An in-the-money short leg left past
option expiry devolves into a GOLDM futures position, and GOLDM futures go to
compulsory physical delivery.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from algo.core.errors import CalendarError
from algo.exchange.calendar import MarketCalendar

_FRIDAY = 4


class ExpirySet(BaseModel):
    """The three dates the devolvement guard needs, for one contract cycle.

    They are separate fields rather than one date plus offsets because the offsets
    are exactly what was wrong before. Reading all three means the risk layer never
    has to infer one from another.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    option_expiry: date
    futures_expiry: date | None = None
    tender_period_start: date | None = None

    def days_to_option_expiry(self, on: date) -> int:
        return (self.option_expiry - on).days


def last_friday(year: int, month: int) -> date:
    """The last Friday of the given month, before any holiday adjustment."""
    last_day = monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    while candidate.weekday() != _FRIDAY:
        candidate -= timedelta(days=1)
    return candidate


class ExpiryProvider(Protocol):
    """Anything that can answer 'when does this contract month expire?'."""

    def option_expiry(self, underlying: str, year: int, month: int) -> date: ...


class LastFridayRule:
    """Cross-check heuristic. Last Friday of the month, rolled back over holidays.

    Confirmed against one live contract. One data point is one month, so the
    recorder validates it across many before anything relies on it alone.
    """

    __slots__ = ("_calendar",)

    def __init__(self, calendar: MarketCalendar) -> None:
        self._calendar = calendar

    def option_expiry(self, underlying: str, year: int, month: int) -> date:
        del underlying  # the rule is the same for every MCX contract we trade
        return self._calendar.previous_trading_day(last_friday(year, month), inclusive=True)


class InstrumentMasterExpiries:
    """Authoritative expiry dates, as listed by the broker's instrument master.

    Populated at Milestone 1.5 from the live instrument master. Until then it is
    filled explicitly in tests and fixtures — which is honest: the engine cannot
    invent an expiry it has not been told.
    """

    __slots__ = ("_table",)

    def __init__(self, table: dict[tuple[str, int, int], ExpirySet]) -> None:
        self._table = dict(table)

    def option_expiry(self, underlying: str, year: int, month: int) -> date:
        return self.expiry_set(underlying, year, month).option_expiry

    def expiry_set(self, underlying: str, year: int, month: int) -> ExpirySet:
        try:
            return self._table[(underlying, year, month)]
        except KeyError as exc:
            raise CalendarError(
                f"no expiry recorded for {underlying} {year}-{month:02d}. "
                "The instrument master is the source of truth and it does not "
                "contain this contract — refusing to derive one."
            ) from exc

    def known_months(self) -> tuple[tuple[str, int, int], ...]:
        return tuple(sorted(self._table))


class ExpiryCalendar:
    """Authoritative expiries, continuously cross-checked against the rule.

    A mismatch is not resolved by preferring one side — it raises. If the two
    sources disagree, the safe action is to stop trading and have a human look,
    not to pick the more convenient answer.
    """

    __slots__ = ("_authority", "_rule", "_strict")

    def __init__(
        self,
        *,
        authority: InstrumentMasterExpiries,
        rule: LastFridayRule | None = None,
        strict: bool = True,
    ) -> None:
        self._authority = authority
        self._rule = rule
        self._strict = strict

    def expiry_set(self, underlying: str, year: int, month: int) -> ExpirySet:
        resolved = self._authority.expiry_set(underlying, year, month)
        if self._rule is not None:
            expected = self._rule.option_expiry(underlying, year, month)
            if expected != resolved.option_expiry and self._strict:
                raise CalendarError(
                    f"expiry mismatch for {underlying} {year}-{month:02d}: "
                    f"instrument master says {resolved.option_expiry}, "
                    f"last-Friday rule says {expected}. Halting rather than choosing."
                )
        return resolved

    def option_expiry(self, underlying: str, year: int, month: int) -> date:
        return self.expiry_set(underlying, year, month).option_expiry

    def nearest_expiry_on_or_after(
        self, underlying: str, on: date, *, horizon: int = 3
    ) -> ExpirySet:
        """The first cycle whose option expiry is on or after `on`.

        Looks forward `horizon` contract months, no further. Silently scanning
        further would let the engine pick a contract that is not yet listed.

        A contract month with nothing recorded is skipped rather than treated as
        the end of the search — `option_expiries` (BarContext) already tolerates
        this same gap for the identical reason: the *calendar* month `on` falls in
        need not itself have a listed contract (its own contract may already have
        expired and rolled off, or — for a table built from a bhavcopy archive
        that only ever saw one expiry — never have existed at all). What matters
        is finding the nearest listed one within the horizon, not the first one.
        """
        year, month = on.year, on.month
        for _ in range(horizon):
            try:
                candidate = self.expiry_set(underlying, year, month)
            except CalendarError:
                candidate = None
            if candidate is not None and candidate.option_expiry >= on:
                return candidate
            month += 1
            if month > 12:
                year, month = year + 1, 1
        raise CalendarError(
            f"no {underlying} option expiry on or after {on} within {horizon} contract months"
        )
