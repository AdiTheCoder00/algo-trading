"""CSV bar feed.

Prices are read as **strings** and converted to `Decimal` directly. Going via
`float` would defeat brief §2.5 before the data even entered the engine: a price
of 156640.05 becomes 156640.04999999999... and then fails a tick-grid check
several layers away, where the cause is no longer visible.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc

REQUIRED_COLUMNS = ("ts", "open", "high", "low", "close")


def read_csv_bars(path: Path, timeframe: Timeframe) -> list[Bar]:
    """Load bars from `path`.

    Expected columns: ts (ISO 8601 with offset), open, high, low, close,
    and optionally volume, open_interest, is_partial.
    """
    if not path.exists():
        raise DataError(f"bar file not found: {path}")

    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataError(f"{path} has no header row")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise DataError(f"{path} is missing required columns: {', '.join(missing)}")

        for line_no, row in enumerate(reader, start=2):
            bars.append(_row_to_bar(row, timeframe, path, line_no))
    return bars


def _row_to_bar(row: dict[str, str], timeframe: Timeframe, path: Path, line_no: int) -> Bar:
    try:
        ts = ensure_utc(datetime.fromisoformat(row["ts"]))
        return Bar(
            ts=ts,
            timeframe=timeframe,
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=int(row.get("volume") or 0),
            open_interest=int(row["open_interest"]) if row.get("open_interest") else None,
            is_partial=(row.get("is_partial", "").strip().lower() in {"1", "true", "yes"}),
        )
    except (InvalidOperation, ValueError, KeyError) as exc:
        # Brief §12: never swallow. Re-raise with the location, which is the one
        # piece of information the original exception does not carry.
        raise DataError(f"{path}:{line_no} could not be parsed as a bar: {exc}") from exc


def write_csv_bars(bars: list[Bar], path: Path) -> None:
    """Write bars back out losslessly — Decimals as their exact string form."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume", "open_interest",
                         "is_partial"])
        for bar in bars:
            writer.writerow(
                [
                    bar.ts.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    bar.volume,
                    "" if bar.open_interest is None else bar.open_interest,
                    "true" if bar.is_partial else "false",
                ]
            )


class CsvBarFeed:
    """A `BarFeed` backed by a CSV file."""

    __slots__ = ("_bars", "_instrument", "_timeframe")

    def __init__(self, instrument: InstrumentId, timeframe: Timeframe, path: Path) -> None:
        self._instrument = instrument
        self._timeframe = timeframe
        self._bars = tuple(read_csv_bars(path, timeframe))

    @property
    def instrument(self) -> InstrumentId:
        return self._instrument

    @property
    def timeframe(self) -> Timeframe:
        return self._timeframe

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)

    def __len__(self) -> int:
        return len(self._bars)
