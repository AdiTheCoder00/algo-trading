"""Timezone-aware time handling. Brief §2.6.

Internally every timestamp is tz-aware UTC. Conversion to an exchange-local wall
clock happens only at the edges, and only through a *named* zone — never a fixed
offset.

The named-zone rule is not academic here. MCX shortens its evening session from
23:55 to 23:30 IST while **US** daylight saving is in force. India has no DST, so
a session model written purely in `Asia/Kolkata` is silently wrong for roughly
eight months of the year. The DST question has to be asked of New York.

This module has one subject — time — and holds nothing else.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from algo.core.errors import DomainError

IST: Final = ZoneInfo("Asia/Kolkata")
NY: Final = ZoneInfo("America/New_York")
LONDON: Final = ZoneInfo("Europe/London")


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    """Build a tz-aware UTC datetime. The only constructor used inside the engine."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise anything aware to UTC.

    Called at every adapter boundary. A naive datetime crossing into the engine is
    a bug, not something to guess a zone for.
    """
    if value.tzinfo is None:
        raise DomainError(f"naive datetime {value!r} — every timestamp must be tz-aware")
    return value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    """UTC -> IST wall clock, via the named zone."""
    return ensure_utc(value).astimezone(IST)


def ist_to_utc(session_day: date, wall: time) -> datetime:
    """Exchange-local wall clock on a given date -> UTC.

    IST has no DST so this is unambiguous today; going through `ZoneInfo` anyway
    means the function stays correct if a venue in a DST zone is ever added.
    """
    return datetime.combine(session_day, wall, tzinfo=IST).astimezone(UTC)


def ist_date(value: datetime) -> date:
    """The IST calendar date a UTC instant falls on.

    Needed because MCX sessions run to 23:30/23:55 IST, which is 18:00/18:25 UTC —
    still the same IST day, but a naive UTC-date grouping would be fine here and
    then break the moment a session crossed UTC midnight. Grouping by IST date is
    correct by construction.
    """
    return to_ist(value).date()


def is_us_dst(on: date) -> bool:
    """Is US daylight saving in force on `on`?

    Evaluated at noon New York time so the answer never lands inside a transition
    hour. US DST runs from the second Sunday in March to the first Sunday in
    November; both are non-trading days for MCX, so a trading session never
    straddles the switch.
    """
    noon_ny = datetime.combine(on, time(12, 0), tzinfo=NY)
    offset = noon_ny.dst()
    return offset is not None and offset != timedelta(0)


def minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes from `start` to `end`. Raises if the interval is negative."""
    delta = ensure_utc(end) - ensure_utc(start)
    seconds = delta.total_seconds()
    if seconds < 0:
        raise DomainError(f"negative interval: {start} -> {end}")
    return int(seconds // 60)


def iso(value: datetime) -> str:
    """Canonical timestamp rendering for logs and golden files.

    Fixed format, always UTC, always to the second — so a trade log diff shows
    real changes rather than formatting drift.
    """
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
