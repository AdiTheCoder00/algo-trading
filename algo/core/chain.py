"""Option chain snapshot — every strike for one expiry, at one instant.

Two invariants the strategy depends on, enforced here rather than trusted:

1.  The underlying futures quote is captured in the *same* snapshot as the option
    quotes. MCX options are options on futures (D-018), so a stale futures price
    silently corrupts every delta in the chain — and a corrupted delta picks the
    wrong strike, which is a wrong trade rather than a wrong number.
2.  Rows are held in a deterministic order (ascending strike, then right), so a
    run is byte-reproducible. Iterating a dict in insertion order would make the
    trade log depend on file layout.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from algo.core.enums import Right
from algo.core.errors import DomainError
from algo.core.instrument import OptionId
from algo.core.quote import Quote
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ChainRow(BaseModel):
    """One option contract's quote, plus whatever we have derived from it.

    `iv` and `delta` are floats and may be None. They are model outputs, not
    observations: a non-converging solve leaves them None and marks the row
    untradeable rather than substituting a plausible number (D-005).
    """

    model_config = _FROZEN

    option: OptionId
    quote: Quote
    iv: float | None = None
    delta: float | None = None
    priced_from: str = ""
    """Which price was inverted: "MID" where the book was two-sided, "LTP"
    otherwise, empty when the row could not be priced at all. Recorded per row
    because those are different qualities of evidence (assumption 5.2), and a
    report that blends a solid mid with a stale last trade overstates what it
    knows."""

    @property
    def strike(self) -> Decimal:
        return self.option.strike

    @property
    def right(self) -> Right:
        return self.option.right

    @property
    def is_tradeable(self) -> bool:
        """Quotable *and* priced. Both are required before this row can be sold."""
        return self.quote.is_tradeable and self.iv is not None and self.delta is not None


class OptionChainSnapshot(BaseModel):
    """All rows for one option expiry as of `ts`."""

    model_config = _FROZEN

    ts: datetime
    underlying: str
    option_expiry: date
    futures_price: Decimal
    futures_quote: Quote | None = None
    rows: tuple[ChainRow, ...] = ()

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @model_validator(mode="after")
    def _sorted_and_consistent(self) -> OptionChainSnapshot:
        if self.futures_price <= 0:
            raise ValueError(f"futures price must be positive, got {self.futures_price}")
        keys = [(r.strike, r.right.value) for r in self.rows]
        if keys != sorted(keys):
            raise ValueError("chain rows must be sorted by (strike, right) for determinism")
        for row in self.rows:
            if row.option.option_expiry != self.option_expiry:
                raise ValueError(
                    f"row {row.option.key} does not belong to expiry {self.option_expiry}"
                )
        return self

    def __iter__(self) -> Iterator[ChainRow]:  # type: ignore[override]
        return iter(self.rows)

    def strikes(self) -> tuple[Decimal, ...]:
        return tuple(sorted({r.strike for r in self.rows}))

    def by_strike(self, strike: Decimal, right: Right) -> ChainRow | None:
        for row in self.rows:
            if row.strike == strike and row.right is right:
                return row
        return None

    def atm_strike(self) -> Decimal:
        """Strike closest to the futures price. Ties resolve to the lower strike."""
        available = self.strikes()
        if not available:
            raise DomainError("empty chain has no ATM strike")
        return min(available, key=lambda k: (abs(k - self.futures_price), k))

    def nearest_delta(
        self, target: float, right: Right, *, tolerance: float
    ) -> ChainRow | None:
        """The tradeable row whose |delta| is closest to `target`, within `tolerance`.

        Only tradeable rows are considered — selecting a strike nobody is quoting
        produces a backtest fill that live trading could never have achieved.
        Ties break on the lower strike so selection is reproducible.
        """
        best: ChainRow | None = None
        best_gap = float("inf")
        for row in self.rows:
            if row.right is not right or not row.is_tradeable or row.delta is None:
                continue
            gap = abs(abs(row.delta) - target)
            if gap > tolerance:
                continue
            ties = gap == best_gap and best is not None and row.strike < best.strike
            if gap < best_gap or ties:
                best, best_gap = row, gap
        return best
