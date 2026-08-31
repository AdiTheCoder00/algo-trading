"""Real bid/ask spread, read from MT5 tick history rather than assumed.

`CfdCosts.half_spread` has been a constant since D-121: 29 ticks, $0.29 the
round trip, taken from a single live quote. The dashboard has been saying so on
every page - "spread is modelled, not measured" - because that is exactly what
it was, and because on the M5 study (D-123) spread was $56,608 against $12,271
of gross profit, which makes the assumption the whole result rather than a
detail.

MT5 does carry the real thing: `copy_ticks_range` returns bid **and** ask, so
the spread at any historical instant is recoverable. This module turns that into
something a backtest can charge.

## Sampled by hour of week, not tick by tick

Two hours of XAUUSD is ~76,000 ticks; ten months is on the order of a hundred
million. Fetching that to price 144 fills would be absurd. Instead this samples
a short window at points spread across the history and builds a **profile by
hour of the trading week**, which is the shape the spread actually has: tight
through the London/New York overlap, wide at the 21:00 rollover, wide at the
Sunday reopen.

That is a real measurement of a real pattern, and it is honest about being a
profile rather than a per-fill reading - `SpreadProfile.describe()` says how
many samples stand behind it and the panel repeats that. A fill at 03:00 on a
Tuesday is charged what 03:00 Tuesdays actually cost, not what one quote on one
afternoon in August happened to be.

## The median, not the mean

One 40-tick spread spike during a news print would drag a mean far above what a
typical fill pays. The median is what the strategy actually meets most of the
time, and the 90th percentile is carried alongside it so the tail is visible
rather than averaged away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from algo.core.errors import DataError

#: Hours in a trading week, indexed Monday 00:00 UTC = 0.
HOURS_IN_WEEK = 24 * 7

#: How long a window to pull at each sample point. Long enough to hold hundreds
#: of ticks in a quiet hour, short enough that a sample is cheap.
SAMPLE_MINUTES = 3

DEFAULT_CACHE = Path("state/mt5_spread_profile.json")


@dataclass(frozen=True, slots=True)
class SpreadProfile:
    """Measured spread by hour of the trading week, in price units."""

    symbol: str
    #: hour-of-week -> median full spread. Missing hours are market-closed.
    median_by_hour: dict[int, Decimal]
    #: hour-of-week -> 90th-percentile full spread, so the tail is visible.
    p90_by_hour: dict[int, Decimal]
    samples: int
    ticks: int
    measured_at: datetime
    #: Charged when an hour has no sample of its own - the median across every
    #: hour that does. Never a guess pulled from outside the measurement.
    fallback: Decimal

    def full_spread_at(self, ts: datetime) -> Decimal:
        """The measured spread for `ts`'s hour of the week."""
        return self.median_by_hour.get(_hour_of_week(ts), self.fallback)

    def half_spread_at(self, ts: datetime) -> Decimal:
        """Half the spread - what one side of a round trip crosses."""
        return self.full_spread_at(ts) / Decimal("2")

    @property
    def median(self) -> Decimal:
        return self.fallback

    @property
    def widest_hour(self) -> tuple[int, Decimal] | None:
        if not self.median_by_hour:
            return None
        hour = max(self.median_by_hour, key=lambda h: self.median_by_hour[h])
        return hour, self.median_by_hour[hour]

    @property
    def tightest_hour(self) -> tuple[int, Decimal] | None:
        if not self.median_by_hour:
            return None
        hour = min(self.median_by_hour, key=lambda h: self.median_by_hour[h])
        return hour, self.median_by_hour[hour]

    def describe(self) -> str:
        return (
            f"measured from {self.ticks:,} ticks across {self.samples} samples; "
            f"median {self.median} over {len(self.median_by_hour)} covered hours"
        )


def _hour_of_week(ts: datetime) -> int:
    ts = ts.astimezone(UTC)
    return ts.weekday() * 24 + ts.hour


def label_hour(hour: int) -> str:
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return f"{days[hour // 24]} {hour % 24:02d}:00 UTC"


def _quantile(values: list[Decimal], q: float) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise DataError("no values to take a quantile of")
    index = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[index]


def measure_spread_profile(
    terminal: Any,
    *,
    symbol: str,
    offset: timedelta,
    start: datetime,
    end: datetime,
    samples: int = 240,
    now: datetime | None = None,
) -> SpreadProfile:
    """Sample tick history across `[start, end]` and build the profile.

    Sample points are spread evenly, so every hour of the week is hit many
    times across a long history rather than the profile being dominated by one
    stretch of days.
    """
    if end <= start:
        raise DataError(f"end {end} is not after start {start}")
    if samples < 1:
        raise DataError(f"samples must be at least 1, got {samples}")

    step = (end - start) / samples
    by_hour: dict[int, list[Decimal]] = {}
    total_ticks = 0
    taken = 0

    for index in range(samples):
        at = start + step * index
        # Ticks are stamped in server time, exactly as bars are.
        raw = terminal.copy_ticks_range(
            symbol,
            at + offset,
            at + offset + timedelta(minutes=SAMPLE_MINUTES),
            terminal.COPY_TICKS_INFO,
        )
        if raw is None or len(raw) == 0:
            continue
        taken += 1
        hour = _hour_of_week(at)
        bucket = by_hour.setdefault(hour, [])
        for row in raw:
            bid = Decimal(str(row["bid"]))
            ask = Decimal(str(row["ask"]))
            # A zero or inverted quote is a bad tick, not a free trade.
            if ask > bid:
                bucket.append(ask - bid)
                total_ticks += 1

    populated = {hour: values for hour, values in by_hour.items() if values}
    if not populated:
        raise DataError(
            f"no usable ticks for {symbol} between {start} and {end}. The "
            "terminal may not have tick history that far back - scroll the "
            "symbol's chart to force a download, or shorten the window."
        )

    median_by_hour = {hour: _quantile(values, 0.5) for hour, values in populated.items()}
    p90_by_hour = {hour: _quantile(values, 0.9) for hour, values in populated.items()}
    every = [value for values in populated.values() for value in values]

    return SpreadProfile(
        symbol=symbol,
        median_by_hour=median_by_hour,
        p90_by_hour=p90_by_hour,
        samples=taken,
        ticks=total_ticks,
        measured_at=now or datetime.now(UTC),
        fallback=_quantile(every, 0.5),
    )


def save_profile(profile: SpreadProfile, path: Path = DEFAULT_CACHE) -> None:
    """Persist so a backtest need not re-sample tick history every run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": profile.symbol,
        "measured_at": profile.measured_at.isoformat(),
        "samples": profile.samples,
        "ticks": profile.ticks,
        "fallback": str(profile.fallback),
        "median_by_hour": {str(h): str(v) for h, v in profile.median_by_hour.items()},
        "p90_by_hour": {str(h): str(v) for h, v in profile.p90_by_hour.items()},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def load_profile(symbol: str, path: Path = DEFAULT_CACHE) -> SpreadProfile | None:
    """The stored profile, or None. A corrupt file is a missing file - never a
    wrong spread, which would silently mis-price every fill."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("symbol") != symbol:
            return None
        return SpreadProfile(
            symbol=raw["symbol"],
            median_by_hour={int(h): Decimal(v) for h, v in raw["median_by_hour"].items()},
            p90_by_hour={int(h): Decimal(v) for h, v in raw["p90_by_hour"].items()},
            samples=int(raw["samples"]),
            ticks=int(raw["ticks"]),
            measured_at=datetime.fromisoformat(raw["measured_at"]),
            fallback=Decimal(raw["fallback"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
