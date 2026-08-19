"""Implied volatility, solved deterministically. Decision D-005.

Two properties matter more than speed here.

**Determinism.** Brief §7.4 requires byte-identical trade logs across runs. A
solver whose iteration count depends on floating-point luck breaks that, so this
is plain bisection over a fixed bracket with a fixed iteration cap. Bisection is
also unconditionally convergent on this problem — option price is strictly
monotonic in volatility — which a Newton iteration is not, and a Newton step that
overshoots into negative vol on a deep out-of-the-money quote is exactly the kind
of once-a-month failure that is impossible to reproduce later.

**Honest failure.** A quote that cannot be inverted returns a status, not a
fallback number. Substituting "roughly the vol of the strike next door" would let
a phantom delta select a strike nobody was quoting, and the backtest would then
report a fill that live trading could never have achieved.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from algo.core.enums import Right
from algo.pricing.black76 import MIN_T, price, price_bounds

#: Bracket for the search. 500% annualised is far beyond anything a gold option
#: has printed; if the answer is outside this, the quote is the problem.
VOL_LOW: Final = 1e-6
VOL_HIGH: Final = 5.0
MAX_ITER: Final = 128
PRICE_TOL: Final = 1e-9


class IvStatus(StrEnum):
    OK = "OK"
    BELOW_INTRINSIC = "BELOW_INTRINSIC"
    ABOVE_BOUND = "ABOVE_BOUND"
    NOT_CONVERGED = "NOT_CONVERGED"
    EXPIRED = "EXPIRED"
    BAD_INPUT = "BAD_INPUT"


class IvSolution(BaseModel):
    """The outcome of one solve, including why it failed if it did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: IvStatus
    iv: float | None = None
    iterations: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is IvStatus.OK and self.iv is not None


def solve_iv(
    *,
    option_price: float,
    f: float,
    k: float,
    t: float,
    r: float,
    right: Right,
) -> IvSolution:
    """Invert Black-76 for volatility.

    The bounds check happens first and deliberately. A quote below discounted
    intrinsic is an arbitrage or a stale print, not a low volatility — and the
    bisection would happily converge on `VOL_LOW` and report 0.0001% vol, which
    reads as a real answer.
    """
    if f <= 0.0 or k <= 0.0 or option_price < 0.0:
        return IvSolution(
            status=IvStatus.BAD_INPUT,
            detail=f"f={f}, k={k}, price={option_price}",
        )
    if t < MIN_T:
        return IvSolution(status=IvStatus.EXPIRED, detail=f"t={t}")

    lower, upper = price_bounds(f, k, t, r, right)
    if option_price < lower - PRICE_TOL:
        return IvSolution(
            status=IvStatus.BELOW_INTRINSIC,
            detail=f"price {option_price:.4f} < discounted intrinsic {lower:.4f}",
        )
    if option_price > upper + PRICE_TOL:
        return IvSolution(
            status=IvStatus.ABOVE_BOUND,
            detail=f"price {option_price:.4f} > no-arbitrage bound {upper:.4f}",
        )

    low, high = VOL_LOW, VOL_HIGH
    if price(f, k, t, high, r, right) < option_price:
        return IvSolution(
            status=IvStatus.NOT_CONVERGED,
            detail=f"price implies volatility above {VOL_HIGH:.0%}",
        )

    for iteration in range(1, MAX_ITER + 1):
        mid = 0.5 * (low + high)
        modelled = price(f, k, t, mid, r, right)
        if abs(modelled - option_price) < PRICE_TOL:
            return IvSolution(status=IvStatus.OK, iv=mid, iterations=iteration)
        if modelled < option_price:
            low = mid
        else:
            high = mid
        if high - low < 1e-12:
            return IvSolution(status=IvStatus.OK, iv=0.5 * (low + high), iterations=iteration)

    return IvSolution(
        status=IvStatus.NOT_CONVERGED,
        iterations=MAX_ITER,
        detail=f"bracket still [{low:.8f}, {high:.8f}] after {MAX_ITER} iterations",
    )
