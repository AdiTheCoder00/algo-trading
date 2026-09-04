"""High-impact US economic events, and the window they block trading in.

## Why this module exists at all

Nothing in this project had a news filter before. `ForexCalendar` knows when the
market is *open*; it has never known when the market is *dangerous*. The MT5
Python API exposes no calendar (there is no `calendar_value_history` binding -
checked, not assumed), and the terminal's own calendar is reachable only from
MQL5 inside a running chart. So the events had to come from somewhere, and the
choice was between a scraped aggregator, a guess, and the primary sources.

**These are the primary sources.** Release dates and times come from the
publishing agency's own schedule:

- Employment Situation (non-farm payrolls), CPI and PPI, all 08:30 ET, from the
  Bureau of Labor Statistics' published release schedules for 2024, 2025 and
  2026 (`bls.gov/schedule/{year}/home.htm`).
- FOMC statement days, 14:00 ET, with the Chair's press conference 30 minutes
  later, from the Federal Reserve's own FOMC calendar
  (`federalreserve.gov/monetarypolicy/fomccalendars.htm`).

`data/us_high_impact_events.csv` is that list, with the source recorded per row.

## What is deliberately NOT in it, because it is not sourced

Retail sales, advance GDP, the PCE price index, the ISM surveys, ADP, jobless
claims, Fed speeches outside the statement window, and every non-US release.
Several of those move gold. They are absent because there was no primary
schedule to hand for them, and a filter built on remembered dates is worse than
no filter: it would block real bars on made-up timestamps and there would be no
way to tell from the output that it had. `EconomicCalendar.describe()` says what
the coverage is, and the report repeats it - the honest statement is "this
blocks NFP, CPI, PPI and FOMC", not "this blocks the news".

The 2025 rows carry the real consequences of that year's lapse in
appropriations: October's Employment Situation was never published and November's
CPI was not either, so those months simply have no row. That is the schedule
that happened, not a gap in the data.

## The window

`blocks(ts)` is true from `before` ahead of an event through `after` past it.
The default (30 minutes before, 15 after) is the strategy's own rule, passed in
rather than hard-coded so a variant run can compare filter settings without
touching the calendar.
"""

from __future__ import annotations

import bisect
import csv
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from algo.core.errors import DataError
from algo.core.timeutil import ensure_utc

#: Shipped alongside the module, so a run cannot silently find no calendar.
DEFAULT_EVENTS = Path(__file__).parent / "data" / "us_high_impact_events.csv"

REQUIRED_COLUMNS = ("ts_utc", "name", "category", "impact", "source")


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    ts: datetime
    name: str
    category: str
    impact: str
    source: str


class EconomicCalendar:
    """Scheduled events, and whether a given instant sits in a blocked window.

    Events are held sorted so `blocks()` is a binary search rather than a scan:
    it is called once per five-minute bar over a hundred thousand bars, and a
    linear scan over a few hundred events would dominate the run.
    """

    __slots__ = ("_events", "_starts")

    def __init__(self, events: Iterable[EconomicEvent]) -> None:
        self._events = sorted(events, key=lambda e: e.ts)
        self._starts = [event.ts for event in self._events]

    @classmethod
    def load(cls, path: Path = DEFAULT_EVENTS) -> EconomicCalendar:
        if not path.exists():
            raise DataError(
                f"economic calendar not found: {path}. The news filter cannot run "
                "without one, and an empty calendar would report zero blocked "
                "entries as though the filter had been applied."
            )
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(_uncommented(handle))
            if reader.fieldnames is None:
                raise DataError(f"{path} has no header row")
            missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
            if missing:
                raise DataError(f"{path} is missing columns: {', '.join(missing)}")
            events = [
                EconomicEvent(
                    ts=ensure_utc(datetime.fromisoformat(row["ts_utc"])),
                    name=row["name"],
                    category=row["category"],
                    impact=row["impact"],
                    source=row["source"],
                )
                for row in reader
            ]
        return cls(events)

    @property
    def events(self) -> Sequence[EconomicEvent]:
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def blocking_event(
        self,
        ts: datetime,
        *,
        before: timedelta = timedelta(minutes=30),
        after: timedelta = timedelta(minutes=15),
    ) -> EconomicEvent | None:
        """The event whose window contains `ts`, or None.

        Returns the event rather than a bool so the trade log can name *which*
        release blocked an entry - "blocked by news" with no name is the kind of
        line that gets read as a bug six weeks later.

        Windows overlap: the FOMC statement at 14:00 and its press conference at
        14:30 produce one continuous block, and the first event covering `ts`
        wins. Which one is reported is arbitrary and does not change the answer.
        """
        ts = ensure_utc(ts)
        # The earliest event that could still cover `ts` starts no earlier than
        # ts - after; the latest starts no later than ts + before.
        left = bisect_left(self._starts, ts - after)
        for index in range(left, len(self._events)):
            event = self._events[index]
            if event.ts - before > ts:
                break
            if event.ts - before <= ts <= event.ts + after:
                return event
        return None

    def blocks(
        self,
        ts: datetime,
        *,
        before: timedelta = timedelta(minutes=30),
        after: timedelta = timedelta(minutes=15),
    ) -> bool:
        return self.blocking_event(ts, before=before, after=after) is not None

    def within(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        """Events in `[start, end)` - what a run's window actually covers."""
        lo = bisect.bisect_left(self._starts, ensure_utc(start))
        hi = bisect.bisect_left(self._starts, ensure_utc(end))
        return self._events[lo:hi]

    def describe(self) -> str:
        if not self._events:
            return "no events loaded"
        kinds: dict[str, int] = {}
        for event in self._events:
            kinds[event.category] = kinds.get(event.category, 0) + 1
        listed = ", ".join(f"{name} x{count}" for name, count in sorted(kinds.items()))
        return (
            f"{len(self._events)} events, "
            f"{self._events[0].ts:%Y-%m-%d} .. {self._events[-1].ts:%Y-%m-%d} ({listed})"
        )


def _uncommented(lines: Iterable[str]) -> Iterable[str]:
    """Drop `#` provenance lines so the CSV can carry its own sourcing."""
    return (line for line in lines if not line.lstrip().startswith("#"))
