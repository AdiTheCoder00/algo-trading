"""The XAUUSD trading week, measured rather than assumed.

`MarketCalendar` models MCX: one session a day, opening and closing on the same
date, with a close time that moves with US daylight saving. None of that
describes a CFD on spot gold, which runs continuously from Sunday evening to
Friday evening with a short daily rollover break. Forcing one into the other
would be a lie about the structure, so this is its own class with the same method
names - anything that only asks for `session_close`, `is_trading_day` and friends
can hold either.

## What the data actually shows

Derived from 5,000 real M30 bars (2026-03-27 .. 2026-08-28) off the Vantage
terminal, converted to UTC:

    Mon-Thu   bars 00:00-20:30 and 22:00-23:30      (the 21:00 and 21:30
                                                     half-hour slots are empty)
    Fri       bars 00:00-20:30, then nothing
    Sat       no bars at all
    Sun       bars 22:00-23:30 only

    gap between bars: 90 min x86  (the daily break)
                      49.5h x19   (the weekend)
                      53.5h, 73.5h (holidays)

## The session definition that makes this simple

**The session for UTC date D runs 22:00 on D-1 until 21:00 on D.**

Choosing the *closing* date as the session's name is what makes the rest fall
out: Sunday's 22:00-23:30 bars belong to Monday's session, Friday's session ends
at 21:00 with no reopen, and the 21:00-22:00 daily break is not an intra-session
break at all - it is exactly the gap *between* consecutive sessions. Nothing has
to special-case it.

21:00 UTC is midnight on the broker's own clock (server time is UTC+3 in summer).
It is the rollover, which is also when financing is charged - so a session
boundary and a swap charge are the same instant, which is the correct
relationship rather than a coincidence.

## Holidays

Not sourced, exactly as for MCX (Q20). The two long gaps in the sample are the
Christmas and New Year closes. `allow_unverified` follows the same rule the MCX
calendar uses: refuse to answer beyond a verified range, unless told the caller
accepts a weekday-only approximation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from algo.core.errors import CalendarError

_SATURDAY = 5
_SUNDAY = 6

#: Measured, not assumed - see the module docstring.
FX_SESSION_OPEN_UTC = time(22, 0)
FX_SESSION_CLOSE_UTC = time(21, 0)


class ForexCalendar:
    """Trading sessions for a 24/5 CFD. Same surface as `MarketCalendar`."""

    __slots__ = ("_allow_unverified", "_close", "_holidays", "_name", "_open", "_verified_through")

    def __init__(
        self,
        *,
        name: str = "FX-UNVERIFIED",
        holidays: frozenset[date] = frozenset(),
        verified_through: date | None = None,
        allow_unverified: bool = True,
        session_open_utc: time = FX_SESSION_OPEN_UTC,
        session_close_utc: time = FX_SESSION_CLOSE_UTC,
    ) -> None:
        self._name = name
        self._holidays = holidays
        self._verified_through = verified_through
        self._allow_unverified = allow_unverified
        self._open = session_open_utc
        self._close = session_close_utc

    @property
    def name(self) -> str:
        return self._name

    def _check_verified(self, on: date) -> None:
        if self._allow_unverified:
            return
        if self._verified_through is None:
            raise CalendarError(
                f"{self._name} has no verified holiday data. Supply one, or "
                "construct with allow_unverified=True to accept a weekday-only "
                "approximation."
            )
        if on > self._verified_through:
            raise CalendarError(
                f"{on} is beyond the verified holiday range (through "
                f"{self._verified_through}) for {self._name}. Refusing to guess."
            )

    def is_trading_day(self, on: date) -> bool:
        """Whether a session *closes* on `on`.

        Monday through Friday. Saturday and Sunday name no session - Sunday
        evening's bars belong to Monday's, which is what the closing-date
        convention buys.
        """
        self._check_verified(on)
        if on in self._holidays:
            return False
        return on.weekday() < _SATURDAY

    def require_trading_day(self, on: date) -> date:
        if not self.is_trading_day(on):
            raise CalendarError(f"{on} names no {self._name} session")
        return on

    def session_open(self, on: date) -> datetime:
        """22:00 UTC on the *previous* calendar day."""
        self.require_trading_day(on)
        return datetime.combine(on - timedelta(days=1), self._open, tzinfo=UTC)

    def session_close(self, on: date) -> datetime:
        self.require_trading_day(on)
        return datetime.combine(on, self._close, tzinfo=UTC)

    def session_minutes(self, on: date) -> int:
        return int((self.session_close(on) - self.session_open(on)).total_seconds() // 60)

    def is_open(self, instant: datetime) -> bool:
        """Whether the market is quoting at `instant`.

        The question a 24/5 venue actually raises, and the one the live loop
        asks. A weekend, a holiday and the daily rollover break all answer False
        through the same path: the instant belongs to no session.
        """
        if instant.tzinfo is None:
            raise CalendarError("is_open needs a timezone-aware instant")
        instant = instant.astimezone(UTC)
        # A session is named by the date it closes on, so the candidate is either
        # today (before its 21:00 close) or tomorrow (after its 22:00 open).
        for candidate in (instant.date(), instant.date() + timedelta(days=1)):
            if not self._is_session(candidate):
                continue
            if self.session_open(candidate) <= instant < self.session_close(candidate):
                return True
        return False

    def _is_session(self, on: date) -> bool:
        """`is_trading_day` without the verified-range check, for internal scans."""
        return on not in self._holidays and on.weekday() < _SATURDAY

    def previous_trading_day(self, on: date, *, inclusive: bool = False) -> date:
        candidate = on if inclusive else on - timedelta(days=1)
        for _ in range(30):
            if self._is_session(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise CalendarError(f"no session within 30 days before {on}")

    def next_trading_day(self, on: date, *, inclusive: bool = False) -> date:
        candidate = on if inclusive else on + timedelta(days=1)
        for _ in range(30):
            if self._is_session(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise CalendarError(f"no session within 30 days after {on}")

    def is_us_dst_session(self, on: date) -> bool:
        """Present for interface compatibility, and meaningless here.

        MCX's close time moves with US daylight saving (D-017), which is why the
        MCX calendar exposes this. A CFD session is fixed against the broker's
        own clock; when that clock shifts, `measure_server_offset` sees it and
        the bars stay aligned. Nothing on this venue should branch on this.
        """
        del on
        return False

    def weekend_gap(self, on: date) -> timedelta:
        """How long the market was shut before `on`'s session opened.

        49 hours across a normal weekend; longer over a holiday. Worth having
        because a position held across it pays financing for every one of those
        nights while being unable to react to anything.
        """
        previous = self.previous_trading_day(on)
        return self.session_open(on) - self.session_close(previous)
