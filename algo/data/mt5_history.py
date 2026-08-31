"""Historical MT5 bars for research, including while the market is shut.

`Mt5BarFeed` serves the live loop and rightly refuses to guess: it takes a
measured `server_offset` and `measure_server_offset` raises rather than assume
one. That is correct for trading and wrong for research, because the moment you
most want to run a study is the weekend - when the newest tick is hours old and
`measure_server_offset` refuses it as stale (which is exactly what it is).

The offset is a property of the broker's clock, not of the current tick. So it
is measured whenever a fresh tick allows, cached to disk, and reused when it
does not. A cached offset is reported as cached; nothing here pretends a
weekend measurement is a live one.

## Why a cache is safe here and would not be in the live loop

An offset that is stale by a DST transition would misalign every bar by an
hour. The live loop must never risk that, and does not - it measures every
start. A research run over 50,000 historical bars carries a different risk
profile: the error is visible (bars land on the wrong side of a session
boundary), it is reported alongside the result, and no order is placed on it.
`CACHE_MAX_AGE` still bounds how stale a cached offset may be before this
refuses, so a cache left over from the previous DST regime cannot be used
silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.data.mt5_feed import Mt5Terminal, measure_server_offset

#: How stale a cached offset may be before it is refused. Comfortably shorter
#: than the gap between DST transitions, and longer than any weekend or holiday
#: close, so the weekend case this exists for always works and the transition
#: case this must not get wrong never does.
CACHE_MAX_AGE = timedelta(days=21)

DEFAULT_CACHE = Path("state/mt5_server_offset.json")

#: MT5's own timeframe constants, by minutes. Only the ones this project has
#: measured against are listed - an unlisted interval is refused rather than
#: guessed at.
TIMEFRAME_CONSTANTS: dict[int, str] = {
    5: "TIMEFRAME_M5",
    15: "TIMEFRAME_M15",
    30: "TIMEFRAME_M30",
    60: "TIMEFRAME_H1",
    240: "TIMEFRAME_H4",
    1440: "TIMEFRAME_D1",
}


@dataclass(frozen=True, slots=True)
class ResolvedOffset:
    """A server-clock offset, and whether it was measured now or reused."""

    offset: timedelta
    measured_now: bool
    measured_at: datetime

    def describe(self) -> str:
        if self.measured_now:
            return f"{self.offset} (measured just now)"
        age = datetime.now(UTC) - self.measured_at
        return f"{self.offset} (cached {int(age.total_seconds() // 3600)}h ago)"


def resolve_server_offset(
    terminal: Mt5Terminal,
    symbol: str,
    *,
    cache_path: Path = DEFAULT_CACHE,
    now: datetime | None = None,
) -> ResolvedOffset:
    """Measure the broker clock, or reuse the last good measurement.

    Raises only when there is neither a usable tick nor a fresh enough cache -
    a state where guessing would misalign every bar, and saying so is the only
    honest answer.
    """
    reference = now or datetime.now(UTC)
    try:
        offset = measure_server_offset(terminal, symbol, now=reference)
    except DataError as exc:
        cached = _read_cache(cache_path, symbol)
        if cached is None:
            raise DataError(
                f"cannot establish the MT5 server clock for {symbol}: {exc}. No "
                f"cached offset in {cache_path} either - run this once while the "
                "market is open so the offset can be measured and stored."
            ) from exc
        measured_at, seconds = cached
        if reference - measured_at > CACHE_MAX_AGE:
            raise DataError(
                f"the cached server offset for {symbol} was measured "
                f"{(reference - measured_at).days} days ago, past the "
                f"{CACHE_MAX_AGE.days}-day limit. A daylight-saving change since "
                "then would shift every bar by an hour, so this refuses rather "
                "than reuse it - run once while the market is open."
            ) from exc
        return ResolvedOffset(
            offset=timedelta(seconds=seconds), measured_now=False, measured_at=measured_at
        )

    _write_cache(cache_path, symbol, reference, offset)
    return ResolvedOffset(offset=offset, measured_now=True, measured_at=reference)


def _read_cache(path: Path, symbol: str) -> tuple[datetime, float] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entry = raw[symbol]
        return datetime.fromisoformat(entry["measured_at"]), float(entry["offset_seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        # A corrupt cache is a missing cache; it must never be a wrong offset.
        return None


def _write_cache(path: Path, symbol: str, at: datetime, offset: timedelta) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                raw = {}
        raw[symbol] = {
            "measured_at": at.isoformat(),
            "offset_seconds": offset.total_seconds(),
        }
        path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        # Failing to cache is not a reason to fail the caller - the offset it
        # asked for was measured successfully and is being returned.
        return


def fetch_history(
    terminal: Mt5Terminal,
    *,
    symbol: str,
    timeframe: Timeframe,
    count: int,
    offset: timedelta,
) -> list[Bar]:
    """Closed bars, oldest first, stamped in UTC.

    Reads from position 1, not 0 - the same "exclude the forming bar" rule
    `Mt5BarFeed.closed_bars` applies, for the same reason: position 0's high,
    low and close can all still change, and handing that to a strategy is
    look-ahead by another name.
    """
    constant = TIMEFRAME_CONSTANTS.get(timeframe.minutes)
    if constant is None:
        raise DataError(
            f"MT5 has no {timeframe.minutes}-minute timeframe; available: "
            f"{sorted(TIMEFRAME_CONSTANTS)}"
        )
    resolved = getattr(terminal, constant, None)
    if resolved is None:
        raise DataError(f"the MT5 module exposes no {constant}")
    if count < 1:
        raise DataError(f"count must be at least 1, got {count}")

    raw = terminal.copy_rates_from_pos(symbol, resolved, 1, count)
    if raw is None or len(raw) == 0:
        raise DataError(
            f"MT5 returned no {timeframe.label} bars for {symbol}: "
            f"{terminal.last_error()}. The terminal may still be downloading "
            "history for this symbol."
        )

    bars = [
        Bar(
            ts=datetime.fromtimestamp(int(row["time"]), UTC) - offset,
            timeframe=timeframe,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["tick_volume"]),
        )
        for row in raw
    ]
    bars.sort(key=lambda b: b.ts)
    return bars
