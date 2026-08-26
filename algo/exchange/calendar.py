"""MCX trading calendar and session clock.

The hard part here is not the holidays, it is the close time. MCX runs its
non-agri evening session to **23:30 IST while US daylight saving is in force** and
to **23:55 IST otherwise**. India has no DST of its own, so the switch is driven
entirely by New York — a calendar written purely in `Asia/Kolkata` is silently
wrong for roughly eight months a year, and wrong in a way that changes the number
of 30-minute bars in a day (C-002, D-017).

Two safety properties, both deliberate:

*   A date outside the verified holiday range raises rather than defaulting to
    "probably a trading day". Silently trading a holiday is exactly the class of
    error brief §1 exists to prevent.
*   Bar boundaries are generated from the session, never assumed. 870 minutes
    divides by 30; 895 does not. The stub is produced and flagged, not rounded away.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from algo.core.bar import Timeframe
from algo.core.errors import CalendarError
from algo.core.timeutil import is_us_dst, ist_to_utc

_SATURDAY = 5


class SessionTimes(BaseModel):
    """Session boundaries in exchange-local (IST) wall clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_ist: time
    close_ist_us_dst: time
    close_ist_standard: time

    def close_for(self, on: date) -> time:
        return self.close_ist_us_dst if is_us_dst(on) else self.close_ist_standard


#: MCX non-agri commodities, including gold.
#: PROVISIONAL — to be confirmed against the MCX circular before any live use.
MCX_NON_AGRI = SessionTimes(
    open_ist=time(9, 0),
    close_ist_us_dst=time(23, 30),
    close_ist_standard=time(23, 55),
)


class BarBoundary(BaseModel):
    """One bar slot within a session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_ts: datetime
    close_ts: datetime
    is_partial: bool


class MarketCalendar:
    """Trading days and session boundaries for one venue."""

    __slots__ = (
        "_allow_unverified",
        "_holidays",
        "_name",
        "_session",
        "_special_sessions",
        "_verified_through",
    )

    def __init__(
        self,
        *,
        name: str,
        session: SessionTimes,
        holidays: frozenset[date],
        verified_through: date | None,
        allow_unverified: bool = False,
        special_sessions: frozenset[date] = frozenset(),
    ) -> None:
        self._name = name
        self._session = session
        self._holidays = holidays
        self._verified_through = verified_through
        self._allow_unverified = allow_unverified
        self._special_sessions = special_sessions

    @property
    def name(self) -> str:
        return self._name

    def _check_verified(self, on: date) -> None:
        if self._allow_unverified:
            return
        if self._verified_through is None:
            raise CalendarError(
                f"{self._name} calendar has no verified holiday data. Load a sourced "
                "holiday file, or construct the calendar with allow_unverified=True "
                "for synthetic tests only."
            )
        if on > self._verified_through:
            raise CalendarError(
                f"{on} is beyond the verified holiday range "
                f"(through {self._verified_through}) for {self._name}. Refusing to guess."
            )

    def is_trading_day(self, on: date) -> bool:
        """Whether the venue was open.

        A weekend is not conclusive. MCX runs **special weekend sessions** - the
        Union Budget falls on 1 February and the exchanges open for it even when
        that is a Saturday or Sunday, as it was in 2020, 2025 and 2026. Real
        bhavcopy for Sunday 2026-02-01 carries 285,223 lots of GOLDM option
        volume across 149 traded strikes (D-107), so treating the weekend rule as
        absolute would discard a genuine session's worth of real trades.

        A holiday still wins over a special session: a date listed in both is a
        session that was scheduled and then cancelled.
        """
        self._check_verified(on)
        if on in self._holidays:
            return False
        if on in self._special_sessions:
            return True
        return on.weekday() < _SATURDAY

    def require_trading_day(self, on: date) -> date:
        if not self.is_trading_day(on):
            raise CalendarError(f"{on} is not an MCX trading day")
        return on

    def previous_trading_day(self, on: date, *, inclusive: bool = False) -> date:
        candidate = on if inclusive else on - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise CalendarError(f"no trading day within 30 days before {on}")

    def next_trading_day(self, on: date, *, inclusive: bool = False) -> date:
        candidate = on if inclusive else on + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise CalendarError(f"no trading day within 30 days after {on}")

    def trading_days(self, start: date, end: date) -> Iterator[date]:
        current = start
        while current <= end:
            if self.is_trading_day(current):
                yield current
            current += timedelta(days=1)

    def session_open(self, on: date) -> datetime:
        self.require_trading_day(on)
        return ist_to_utc(on, self._session.open_ist)

    def session_close(self, on: date) -> datetime:
        self.require_trading_day(on)
        return ist_to_utc(on, self._session.close_for(on))

    def session_minutes(self, on: date) -> int:
        delta = self.session_close(on) - self.session_open(on)
        return int(delta.total_seconds() // 60)

    def bar_boundaries(self, on: date, timeframe: Timeframe) -> tuple[BarBoundary, ...]:
        """Every bar slot for `on`, anchored at the session open.

        The final slot is truncated to the session close and flagged `is_partial`
        when the session length is not a whole number of bars — 09:00–23:55 is
        895 minutes, which is 29 thirty-minute bars plus a 25-minute stub.
        """
        opened = self.session_open(on)
        closes = self.session_close(on)
        step = timedelta(minutes=timeframe.minutes)

        boundaries: list[BarBoundary] = []
        cursor = opened
        while cursor < closes:
            nxt = cursor + step
            if nxt >= closes:
                boundaries.append(
                    BarBoundary(open_ts=cursor, close_ts=closes, is_partial=nxt > closes)
                )
                break
            boundaries.append(BarBoundary(open_ts=cursor, close_ts=nxt, is_partial=False))
            cursor = nxt
        return tuple(boundaries)

    def is_us_dst_session(self, on: date) -> bool:
        """Exposed so a strategy can see which session-length regime it is in."""
        return is_us_dst(on)


#: Weekend dates MCX was demonstrably open on, evidenced by traded volume in the
#: bhavcopy itself (D-107). India's Union Budget is presented on 1 February and
#: the exchanges hold a live session for it regardless of the weekday.
#: Not a rule - a list of observed facts. Extend it only from real data.
MCX_SPECIAL_SESSIONS = frozenset({date(2026, 2, 1)})


def synthetic_calendar(
    *,
    holidays: frozenset[date] = frozenset(),
    session: SessionTimes = MCX_NON_AGRI,
    special_sessions: frozenset[date] = MCX_SPECIAL_SESSIONS,
) -> MarketCalendar:
    """A calendar for tests and synthetic fixtures. Never for a real run.

    Named explicitly rather than offered as a default so that an unverified
    calendar can never reach production by accident.
    """
    return MarketCalendar(
        name="SYNTHETIC",
        session=session,
        holidays=holidays,
        verified_through=None,
        allow_unverified=True,
        special_sessions=special_sessions,
    )


def load_holidays(path: Path) -> tuple[frozenset[date], date]:
    """Read a sourced holiday list: one ISO date per line, `#` starts a comment.

    Returns the dates **and** the last one, which becomes `verified_through` -
    the calendar refuses to answer past that date rather than guessing, so the
    file's own horizon is what bounds it. A file is a list of facts someone
    checked; it must not be read as a rule that extends forever.
    """
    if not path.exists():
        raise CalendarError(f"holiday file not found: {path}")
    dates: set[date] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        try:
            dates.add(date.fromisoformat(text))
        except ValueError as exc:
            raise CalendarError(f"{path}:{line_no}: {text!r} is not an ISO date") from exc
    if not dates:
        raise CalendarError(f"{path} lists no holidays; an empty file is not a source")
    return frozenset(dates), max(dates)


def mcx_calendar(
    *,
    holidays_file: Path | None,
    allow_unverified: bool,
    session: SessionTimes = MCX_NON_AGRI,
    special_sessions: frozenset[date] = MCX_SPECIAL_SESSIONS,
) -> MarketCalendar:
    """The calendar a real run uses. `synthetic_calendar` is for fixtures only.

    Until D-115 every command built `synthetic_calendar()` - including the live
    loop - which the function itself says is "never for a real run" and which
    silently passes `allow_unverified=True`. The configured
    `allow_unverified_calendar` decided nothing at all, so MCX holidays were not
    modelled anywhere. That matters most for `DevolvementGuard`: its exit
    deadline is computed by walking back trading days, and with no holidays a
    deadline can land on a day the market is shut - which, running without a
    stop loss, is the only hard backstop there is.

    With `allow_unverified=False` and no file this refuses rather than degrading,
    because degrading is what it used to do.
    """
    if holidays_file is not None:
        holidays, verified_through = load_holidays(holidays_file)
        return MarketCalendar(
            name="MCX",
            session=session,
            holidays=holidays,
            verified_through=verified_through,
            allow_unverified=allow_unverified,
            special_sessions=special_sessions,
        )
    if not allow_unverified:
        raise CalendarError(
            "market.allow_unverified_calendar is false but no market.holidays_file "
            "is configured, so there are no sourced MCX holidays to honour. Supply "
            "a holiday file, or set allow_unverified_calendar: true to accept a "
            "weekends-only calendar and everything that follows from it."
        )
    return MarketCalendar(
        name="MCX-UNVERIFIED",
        session=session,
        holidays=frozenset(),
        verified_through=None,
        allow_unverified=True,
        special_sessions=special_sessions,
    )
