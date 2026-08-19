"""Black-76: European options on a forward.

This is the right model here rather than a convenience. MCX options are options
**on futures** (D-018), so `F` is directly observed as the underlying futures
price — there is no synthetic forward to reconstruct, no cost-of-carry term and no
dividend term. The one place a rate still appears is discounting the payoff back
from expiry.

Everything in this module is `float`, and that is a deliberate boundary (D-004).
Implied volatility comes out of an iterative solver, so forcing `Decimal` through
it buys no accuracy and costs determinism. It is allowed only because greeks never
touch money: a delta selects a strike, and the strike itself is a `Decimal` read
from the chain. Nothing here may be used as a price or a P&L figure.

The normal CDF is `math.erf`, not a polynomial approximation. It is exact to
double precision and identical across platforms, which matters because a delta of
0.2499 versus 0.2501 selects a different strike — and a backtest that picks
different strikes on different machines is not reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from algo.core.enums import Right
from algo.core.errors import DomainError

_SQRT_2: Final = math.sqrt(2.0)
_INV_SQRT_2PI: Final = 1.0 / math.sqrt(2.0 * math.pi)

#: Below this, time value is numerically indistinguishable from zero and the
#: model degenerates. Roughly five minutes in years.
MIN_T: Final = 1e-5
MIN_VOL: Final = 1e-6


def norm_cdf(x: float) -> float:
    """Standard normal CDF, exact to double precision."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def norm_pdf(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF, by bisection.

    Bisection rather than a rational approximation because it is trivially
    verifiable and runs a fixed number of iterations, so the answer cannot depend
    on how a compiler ordered a polynomial. 200 iterations over [-40, 40] is far
    beyond double precision.
    """
    if not 0.0 < p < 1.0:
        raise DomainError(f"norm_ppf needs 0 < p < 1, got {p}")
    low, high = -40.0, 40.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if norm_cdf(mid) < p:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


@dataclass(frozen=True, slots=True)
class Greeks:
    """Sensitivities for one option.

    Units are stated because getting them wrong is a silent error:

    * ``delta``  — change in option price per 1.0 change in the futures price
    * ``gamma``  — change in delta per 1.0 change in the futures price
    * ``vega``   — change in option price per **1 percentage point** of vol
    * ``theta``  — change in option price per **calendar day**
    """

    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    d1: float
    d2: float


def _validate(f: float, k: float, t: float, vol: float) -> None:
    if f <= 0.0:
        raise DomainError(f"futures price must be positive, got {f}")
    if k <= 0.0:
        raise DomainError(f"strike must be positive, got {k}")
    if t < 0.0:
        raise DomainError(f"time to expiry cannot be negative, got {t}")
    if vol < 0.0:
        raise DomainError(f"volatility cannot be negative, got {vol}")


def _d1_d2(f: float, k: float, t: float, vol: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * vol * vol * t) / (vol * sqrt_t)
    return d1, d1 - vol * sqrt_t


def price(f: float, k: float, t: float, vol: float, r: float, right: Right) -> float:
    """Black-76 option price.

    At expiry, or at zero volatility, this returns discounted intrinsic value
    rather than raising. Both are genuine limits of the model, and an engine
    stepping onto expiry day should get the right number rather than an exception.
    """
    _validate(f, k, t, vol)
    discount = math.exp(-r * t)

    if t < MIN_T or vol < MIN_VOL:
        intrinsic = max(f - k, 0.0) if right is Right.CE else max(k - f, 0.0)
        return discount * intrinsic

    d1, d2 = _d1_d2(f, k, t, vol)
    if right is Right.CE:
        return discount * (f * norm_cdf(d1) - k * norm_cdf(d2))
    return discount * (k * norm_cdf(-d2) - f * norm_cdf(-d1))


def greeks(f: float, k: float, t: float, vol: float, r: float, right: Right) -> Greeks:
    """Price and sensitivities together, so `d1` is computed once."""
    _validate(f, k, t, vol)
    discount = math.exp(-r * t)

    if t < MIN_T or vol < MIN_VOL:
        # Degenerate limit: the option is a discounted forward or nothing at all.
        intrinsic = max(f - k, 0.0) if right is Right.CE else max(k - f, 0.0)
        if right is Right.CE:
            delta = discount if f > k else 0.0
        else:
            delta = -discount if f < k else 0.0
        return Greeks(
            price=discount * intrinsic,
            delta=delta,
            gamma=0.0,
            vega=0.0,
            theta=0.0,
            d1=math.inf if f > k else -math.inf,
            d2=math.inf if f > k else -math.inf,
        )

    sqrt_t = math.sqrt(t)
    d1, d2 = _d1_d2(f, k, t, vol)
    pdf_d1 = norm_pdf(d1)

    if right is Right.CE:
        option_price = discount * (f * norm_cdf(d1) - k * norm_cdf(d2))
        delta = discount * norm_cdf(d1)
        carry = r * f * discount * norm_cdf(d1) - r * k * discount * norm_cdf(d2)
    else:
        option_price = discount * (k * norm_cdf(-d2) - f * norm_cdf(-d1))
        delta = -discount * norm_cdf(-d1)
        carry = -r * f * discount * norm_cdf(-d1) + r * k * discount * norm_cdf(-d2)

    gamma = discount * pdf_d1 / (f * vol * sqrt_t)
    vega_per_unit = discount * f * pdf_d1 * sqrt_t
    theta_per_year = -(f * discount * pdf_d1 * vol) / (2.0 * sqrt_t) + carry

    return Greeks(
        price=option_price,
        delta=delta,
        gamma=gamma,
        vega=vega_per_unit / 100.0,  # per 1 percentage point of vol
        theta=theta_per_year / 365.0,  # per calendar day
        d1=d1,
        d2=d2,
    )


def delta(f: float, k: float, t: float, vol: float, r: float, right: Right) -> float:
    return greeks(f, k, t, vol, r, right).delta


def strike_for_delta(
    f: float, t: float, vol: float, r: float, target_delta: float, right: Right
) -> float:
    """The strike whose |delta| equals `target_delta`, inverted analytically.

    Used to answer "where does 0.25 delta actually sit?" without walking a chain —
    which matters because the answer moves with time to expiry. A fixed delta sits
    *further* from spot as `t` grows, so entering early in a cycle pushes strike
    selection further into the illiquid tail. This function makes that visible.
    """
    if not 0.0 < target_delta < 1.0:
        raise DomainError(f"target delta must be in (0, 1), got {target_delta}")
    _validate(f, 1.0, t, vol)
    if t < MIN_T or vol < MIN_VOL:
        raise DomainError("cannot invert delta at zero time or zero volatility")

    discount = math.exp(-r * t)
    # delta_call = e^{-rT} N(d1)  =>  N(d1) = target / e^{-rT}
    scaled = target_delta / discount
    if not 0.0 < scaled < 1.0:
        raise DomainError(f"target delta {target_delta} is unreachable at r={r}, t={t}")

    z = norm_ppf(scaled) if right is Right.CE else norm_ppf(1.0 - scaled)
    sqrt_t = math.sqrt(t)
    # ln(F/K) = z*vol*sqrt(t) - 0.5*vol^2*t  =>  K = F * exp(0.5*vol^2*t - z*vol*sqrt(t))
    return f * math.exp(0.5 * vol * vol * t - z * vol * sqrt_t)


def intrinsic(f: float, k: float, right: Right) -> float:
    return max(f - k, 0.0) if right is Right.CE else max(k - f, 0.0)


def price_bounds(f: float, k: float, t: float, r: float, right: Right) -> tuple[float, float]:
    """No-arbitrage bounds on the option price.

    A quote outside these cannot be inverted to a volatility, and the honest
    response is to mark the row untradeable rather than to clamp it into range.
    """
    discount = math.exp(-r * t)
    lower = discount * intrinsic(f, k, right)
    upper = discount * (f if right is Right.CE else k)
    return lower, upper
