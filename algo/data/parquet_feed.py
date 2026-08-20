"""Parquet bar feed.

Price columns are stored as **strings**, not floats. Parquet's float64 cannot
represent every tick-grid price exactly, and a backtest that loses a paisa per bar
to binary rounding is a backtest that lies quietly. Strings compress well enough
that the cost is not worth arguing about.

A float price column is rejected loudly with an explanation, rather than silently
converted — a silent conversion here would undo brief §2.5 at the point of entry.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pandas as pd

from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.core.instrument import InstrumentId
from algo.core.timeutil import ensure_utc

PRICE_COLUMNS = ("open", "high", "low", "close")


def read_parquet_bars(path: Path, timeframe: Timeframe) -> list[Bar]:
    if not path.exists():
        raise DataError(f"bar file not found: {path}")
    frame = pd.read_parquet(path)

    missing = [c for c in ("ts", *PRICE_COLUMNS) if c not in frame.columns]
    if missing:
        raise DataError(f"{path} is missing required columns: {', '.join(missing)}")

    for column in PRICE_COLUMNS:
        if pd.api.types.is_float_dtype(frame[column]):
            raise DataError(
                f"{path} stores '{column}' as float. Prices must be stored as strings so "
                "they convert to Decimal exactly — re-export with write_parquet_bars()."
            )

    bars: list[Bar] = []
    for record in frame.to_dict(orient="records"):
        bars.append(
            Bar(
                ts=ensure_utc(pd.Timestamp(record["ts"]).to_pydatetime()),
                timeframe=timeframe,
                open=Decimal(str(record["open"])),
                high=Decimal(str(record["high"])),
                low=Decimal(str(record["low"])),
                close=Decimal(str(record["close"])),
                volume=int(record.get("volume") or 0),
                open_interest=(
                    None if pd.isna(record.get("open_interest")) else int(record["open_interest"])
                ),
                is_partial=bool(record.get("is_partial", False)),
            )
        )
    return bars


def write_parquet_bars(bars: list[Bar], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [str(b.open) for b in bars],
            "high": [str(b.high) for b in bars],
            "low": [str(b.low) for b in bars],
            "close": [str(b.close) for b in bars],
            "volume": [b.volume for b in bars],
            "open_interest": [b.open_interest for b in bars],
            "is_partial": [b.is_partial for b in bars],
        }
    )
    frame.to_parquet(path, index=False)


class ParquetBarFeed:
    """A `BarFeed` backed by a parquet file."""

    __slots__ = ("_bars", "_instrument", "_timeframe")

    def __init__(self, instrument: InstrumentId, timeframe: Timeframe, path: Path) -> None:
        self._instrument = instrument
        self._timeframe = timeframe
        self._bars = tuple(read_parquet_bars(path, timeframe))

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
