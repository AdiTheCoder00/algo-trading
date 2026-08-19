"""Bars, and the read-only window that is handed to a strategy.

`BarWindow` is the look-ahead firewall (brief §7.1, decision D-006). It is
constructed from a *copy* of history up to and including bar `i`. There is no
accessor for `i+1` because there is no bar `i+1` in the object — the future is not
merely hidden, it is absent. That distinction is the whole point: an accessor
guard can be argued around by a determined strategy author six months from now;
data that was never copied in cannot.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Final

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from algo.core.errors import DomainError, LookAheadError
from algo.core.timeutil import ensure_utc


class Timeframe(BaseModel):
    """A bar duration, in whole minutes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minutes: int

    @field_validator("minutes")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"timeframe must be >= 1 minute, got {v}")
        return v

    @property
    def label(self) -> str:
        return f"{self.minutes}m"

    def __str__(self) -> str:
        return self.label


M1: Final = Timeframe(minutes=1)
M15: Final = Timeframe(minutes=15)
M30: Final = Timeframe(minutes=30)


class Bar(BaseModel):
    """One completed bar, labelled by its **close** timestamp.

    The close-label convention is stated once here and relied on everywhere: a bar
    with `ts = 09:30` covers the half-open interval (09:00, 09:30]. Labelling by
    open instead would make "is this bar finished?" ambiguous, and that ambiguity
    is exactly where look-ahead creeps in.

    `is_partial` marks a bar that closed early because the session ended mid-
    interval. On MCX that is the 23:30–23:55 IST stub outside US daylight saving
    (D-014). It is kept rather than dropped, and flagged rather than merged, so
    the data stays honest and the risk layer can still act in the last 25 minutes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    open_interest: int | None = None
    is_partial: bool = False

    @field_validator("ts")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Bar:
        if self.low > self.high:
            raise ValueError(f"low {self.low} > high {self.high} at {self.ts}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open {self.open} outside [{self.low}, {self.high}] at {self.ts}")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close {self.close} outside [{self.low}, {self.high}] at {self.ts}")
        if self.volume < 0:
            raise ValueError(f"negative volume {self.volume} at {self.ts}")
        return self

    @property
    def range(self) -> Decimal:
        return self.high - self.low


class BarWindow:
    """Immutable, index-bounded view of bars `[0 .. i]`.

    Deliberately *not* a pandas object. A DataFrame handed to a strategy carries
    `.iloc`, `.shift(-1)`, `.tail()` on the parent frame and a dozen other ways to
    reach past the current bar. This class has none of them.
    """

    __slots__ = ("_bars",)

    def __init__(self, bars: tuple[Bar, ...]) -> None:
        # Stored as a tuple so neither the caller nor the strategy can mutate or
        # extend it after construction.
        self._bars = bars

    @classmethod
    def of(cls, bars: list[Bar] | tuple[Bar, ...]) -> BarWindow:
        """Copy `bars` into a window, checking the timestamps are strictly increasing.

        Out-of-order bars are a data bug that would otherwise present as a subtle
        look-ahead: a strategy reading `window[-1]` would get whichever bar
        happened to be last in the file, not the latest in time.
        """
        materialised = tuple(bars)
        for earlier, later in pairwise(materialised):
            if later.ts <= earlier.ts:
                raise DomainError(
                    f"bars must be strictly increasing in time: {earlier.ts} then {later.ts}"
                )
        return cls(materialised)

    def __len__(self) -> int:
        return len(self._bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)

    def __getitem__(self, index: int) -> Bar:
        """Bar at `index`. Negative indices count back from the current bar.

        Any index at or past the end raises `LookAheadError` rather than a bare
        `IndexError`, so the failure names what actually went wrong.
        """
        n = len(self._bars)
        if index >= n or index < -n:
            raise LookAheadError(
                f"bar index {index} is outside the visible window of {n} closed bars — "
                "there is no data beyond the current bar"
            )
        return self._bars[index]

    @property
    def current(self) -> Bar:
        """The most recently closed bar — bar `i`. This is 'now' for a strategy."""
        if not self._bars:
            raise DomainError("window is empty; no current bar")
        return self._bars[-1]

    def tail(self, n: int) -> BarWindow:
        """The last `n` bars, as a narrower window. Never widens."""
        if n < 0:
            raise DomainError(f"tail length must be >= 0, got {n}")
        return BarWindow(self._bars[-n:] if n else ())

    # ---------------------------------------------------------------- arrays
    # Brief §3 allows vectorised indicator math. This is the single, explicit
    # boundary where Decimal becomes float: indicator inputs only. Nothing that
    # comes back out of an indicator may be used as a price or a P&L figure
    # (decision D-004) — it is used to make decisions, not to move money.

    def closes(self) -> NDArray[np.float64]:
        return np.array([float(b.close) for b in self._bars], dtype=np.float64)

    def highs(self) -> NDArray[np.float64]:
        return np.array([float(b.high) for b in self._bars], dtype=np.float64)

    def lows(self) -> NDArray[np.float64]:
        return np.array([float(b.low) for b in self._bars], dtype=np.float64)

    def opens(self) -> NDArray[np.float64]:
        return np.array([float(b.open) for b in self._bars], dtype=np.float64)

    def volumes(self) -> NDArray[np.float64]:
        return np.array([float(b.volume) for b in self._bars], dtype=np.float64)

    def timestamps(self) -> tuple[datetime, ...]:
        return tuple(b.ts for b in self._bars)

    def __repr__(self) -> str:
        if not self._bars:
            return "BarWindow(empty)"
        return f"BarWindow({len(self._bars)} bars, ending {self._bars[-1].ts:%Y-%m-%d %H:%M}Z)"
