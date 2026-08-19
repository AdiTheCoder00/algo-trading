"""Turning a chain of quotes into a chain of deltas.

This is the step the strategy depends on and the step most able to lie. Three
rules keep it honest:

1.  **The futures price comes from the same snapshot as the option quotes.** A
    stale `F` corrupts every delta in the chain at once, and a corrupted delta
    picks the wrong strike — a wrong trade, not a wrong number.
2.  **A row that will not price stays unpriced.** No borrowing the neighbouring
    strike's volatility, no interpolating a smile through a gap. An unpriced row
    is untradeable, which is the truthful outcome for a strike nobody is quoting.
3.  **Which price was inverted is recorded per row.** Mid where both sides exist,
    last trade otherwise. Those are different qualities of evidence and a report
    that blends them silently is overstating what it knows.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Right
from algo.core.errors import DomainError
from algo.pricing.black76 import greeks, strike_for_delta
from algo.pricing.iv import IvSolution, solve_iv

#: Actual/365 fixed. The Indian convention, and the one a broker's own option
#: calculator uses — matching it matters more than theoretical elegance, because
#: a delta that disagrees with the terminal is a delta nobody will trust.
DAYS_PER_YEAR = 365.0


def time_to_expiry(now: datetime, expires_at: datetime) -> float:
    """Year fraction from `now` to `expires_at`, ACT/365, never negative.

    Both are instants, not dates. An option expiring at the close on 28 August is
    worth something at 09:30 that morning, and a date-only calculation would
    price it at zero for the whole final session.
    """
    seconds = (expires_at - now).total_seconds()
    return max(seconds / (86400.0 * DAYS_PER_YEAR), 0.0)


def _reference_price(row: ChainRow) -> tuple[float | None, str]:
    """Mid where the book is two-sided, last trade otherwise."""
    mid = row.quote.mid
    if mid is not None and mid > 0:
        return float(mid), "MID"
    if row.quote.ltp is not None and row.quote.ltp > 0:
        return float(row.quote.ltp), "LTP"
    return None, ""


def price_row(
    row: ChainRow, *, futures_price: Decimal, t: float, r: float
) -> tuple[ChainRow, IvSolution | None]:
    """Solve one row's volatility and delta, or leave it unpriced."""
    reference, source = _reference_price(row)
    if reference is None:
        return row.model_copy(update={"iv": None, "delta": None, "priced_from": ""}), None

    solution = solve_iv(
        option_price=reference,
        f=float(futures_price),
        k=float(row.strike),
        t=t,
        r=r,
        right=row.right,
    )
    if not solution.ok or solution.iv is None:
        return (
            row.model_copy(update={"iv": None, "delta": None, "priced_from": ""}),
            solution,
        )

    row_greeks = greeks(float(futures_price), float(row.strike), t, solution.iv, r, row.right)
    return (
        row.model_copy(
            update={"iv": solution.iv, "delta": row_greeks.delta, "priced_from": source}
        ),
        solution,
    )


def enrich(snapshot: OptionChainSnapshot, *, expires_at: datetime, r: float) -> OptionChainSnapshot:
    """Return the snapshot with `iv`, `delta` and `priced_from` filled in."""
    t = time_to_expiry(snapshot.ts, expires_at)
    priced = tuple(
        price_row(row, futures_price=snapshot.futures_price, t=t, r=r)[0] for row in snapshot.rows
    )
    return snapshot.model_copy(update={"rows": priced})


def atm_iv(snapshot: OptionChainSnapshot) -> float | None:
    """Volatility at the strike nearest the futures price.

    Averaged across the call and the put where both priced, because the two
    almost never agree exactly and picking one arbitrarily would make the
    reference vol depend on which right happened to be quoted.
    """
    if not snapshot.rows:
        return None
    strike = snapshot.atm_strike()
    values = [
        row.iv
        for row in snapshot.rows
        if row.strike == strike and row.iv is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def theoretical_delta_strike(
    snapshot: OptionChainSnapshot,
    *,
    expires_at: datetime,
    r: float,
    target_delta: float,
    right: Right,
    strike_interval: Decimal,
) -> Decimal | None:
    """Where `target_delta` sits, rounded to the listed strike ladder.

    Answers "which strike *should* we want" independently of whether it is
    quoted, so the gap between the two is measurable. On a thin book that gap is
    the whole question: the strategy asks for 0.25 delta, and the honest answer
    may be that no such strike has a two-sided quote.
    """
    reference_vol = atm_iv(snapshot)
    if reference_vol is None:
        return None
    t = time_to_expiry(snapshot.ts, expires_at)
    if t <= 0:
        return None
    raw = strike_for_delta(
        float(snapshot.futures_price), t, reference_vol, r, target_delta, right
    )
    if strike_interval <= 0:
        raise DomainError(f"strike interval must be positive, got {strike_interval}")
    steps = (Decimal(str(raw)) / strike_interval).quantize(Decimal("1"))
    return steps * strike_interval
