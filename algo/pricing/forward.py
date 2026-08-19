"""The forward the options are actually priced off, and whether it agrees with
the futures price we were handed.

Decision D-018 said this module would collapse to nothing, because MCX options are
options on futures and `F` is therefore observed rather than reconstructed. That
was right in principle and wrong in practice, and the live chain showed why.

Inverting the observed 28 Aug 2026 chain against the futures price displayed on
the terminal produced put volatilities roughly 0.3 points above call volatilities
at **every** strike — a one-sided error, not noise. Solving put-call parity for
the forward instead gives a tight cluster around 1,56,610 against a displayed
1,56,640. Thirty points.

A one-sided skew like that is the signature of a wrong `F`, and a wrong `F` biases
every delta in the chain in the same direction — which biases strike selection in
the same direction, on every trade. That is precisely the silent wrong number the
brief exists to prevent, so the parity forward is computed as a cross-check and
disagreement is reported rather than absorbed.
"""

from __future__ import annotations

import math
from decimal import Decimal
from statistics import median

from pydantic import BaseModel, ConfigDict

from algo.core.chain import OptionChainSnapshot
from algo.core.enums import Right


def forward_from_parity(
    *, call_price: float, put_price: float, strike: float, t: float, r: float
) -> float:
    """Solve put-call parity for the forward.

    ``C - P = (F - K) e^{-rT}``  =>  ``F = K + (C - P) e^{rT}``

    This is model-free. It assumes no volatility, no smile and nothing about the
    distribution — only that the two options and the forward are quoted
    consistently with each other. That is what makes it a usable check on `F`.
    """
    return strike + (call_price - put_price) * math.exp(r * t)


class ForwardCheck(BaseModel):
    """Comparison of the stated futures price against what the chain implies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stated: Decimal
    implied: Decimal | None
    pairs_used: int
    spread_of_estimates: Decimal | None = None

    @property
    def gap(self) -> Decimal | None:
        if self.implied is None:
            return None
        return self.stated - self.implied

    @property
    def gap_pct(self) -> Decimal | None:
        gap = self.gap
        if gap is None or self.stated == 0:
            return None
        return gap / self.stated * Decimal("100")

    def is_consistent(self, *, tolerance_pct: Decimal) -> bool:
        """Whether the two agree closely enough to trust the deltas.

        Returns True when there is nothing to compare against — a chain with no
        two-sided strike pair cannot contradict the futures price, and treating
        "no evidence" as "failure" would halt the engine on thin days for no
        reason. The `pairs_used` count is what tells you which case you are in.
        """
        gap = self.gap_pct
        if gap is None:
            return True
        return abs(gap) <= tolerance_pct


def implied_forward(
    snapshot: OptionChainSnapshot, *, t: float, r: float, max_pairs: int = 5
) -> ForwardCheck:
    """Estimate the forward from the strikes nearest the money that quote both sides.

    The **median** of the per-strike estimates, not the mean: one stale print on
    one leg of one strike would drag a mean, and a stale print is exactly the
    condition this check exists to survive. Only strikes near the money are used,
    because parity on a far wing divides a small difference by nothing much and
    amplifies the bid-ask spread into a large forward error.
    """
    stated = snapshot.futures_price
    if not snapshot.rows:
        return ForwardCheck(stated=stated, implied=None, pairs_used=0)

    by_strike: dict[Decimal, dict[Right, float]] = {}
    for row in snapshot.rows:
        reference = row.quote.mid if row.quote.mid is not None else row.quote.ltp
        if reference is None or reference <= 0:
            continue
        by_strike.setdefault(row.strike, {})[row.right] = float(reference)

    complete = [
        (strike, sides[Right.CE], sides[Right.PE])
        for strike, sides in by_strike.items()
        if Right.CE in sides and Right.PE in sides
    ]
    if not complete:
        return ForwardCheck(stated=stated, implied=None, pairs_used=0)

    complete.sort(key=lambda item: abs(item[0] - stated))
    chosen = complete[:max_pairs]

    estimates = [
        forward_from_parity(call_price=call, put_price=put, strike=float(strike), t=t, r=r)
        for strike, call, put in chosen
    ]
    spread = Decimal(str(max(estimates) - min(estimates))) if len(estimates) > 1 else None

    return ForwardCheck(
        stated=stated,
        implied=Decimal(str(median(estimates))),
        pairs_used=len(estimates),
        spread_of_estimates=spread,
    )
