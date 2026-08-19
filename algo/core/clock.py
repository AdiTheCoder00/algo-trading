"""The only place in this codebase permitted to read the wall clock. Decision D-015.

Everything else takes a `Clock`. A single stray `datetime.now()` elsewhere makes
backtests non-reproducible and makes a crash-replay diverge from the original run,
so CI greps for it and fails the build.

`BacktestClock` is driven by the event loop: it advances to each bar's close and
never moves on its own. That is what makes "the engine never sees the future"
enforceable — there is no source of a later timestamp for it to read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from algo.core.errors import DomainError
from algo.core.timeutil import ensure_utc


@runtime_checkable
class Clock(Protocol):
    """Source of the current instant, always tz-aware UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time. Used in paper and live only."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class BacktestClock:
    """Deterministic clock advanced explicitly by the engine.

    Time only ever moves forward. An attempt to rewind is a bug in the event loop
    and is raised rather than tolerated, because a silently rewound clock produces
    a trade log that looks plausible and is wrong.
    """

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, moment: datetime) -> None:
        moment = ensure_utc(moment)
        if moment < self._now:
            raise DomainError(f"clock cannot go backwards: {self._now} -> {moment}")
        self._now = moment
