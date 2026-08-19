"""Decimal money math. Brief §2.5: prices, lot sizes, P&L and balances are never float.

This module has exactly one subject — money and the quantisation of money onto the
exchange's tick and lot grids. It is not a `utils.py`: anything that is not about
money does not belong here.

Two rules that keep the arithmetic honest:

1.  Decimals are constructed from `str`, never from `float`. `Decimal(0.1)` is
    0.1000000000000000055511151231257827, which will eventually produce a price
    that is off the tick grid by a fraction of a paisa and be rejected by the
    exchange with an unhelpful error.
2.  Rounding direction is always explicit and always chosen to be conservative
    for the caller's side of the trade. There is no "round to nearest" here for
    order prices, because nearest is sometimes through the market.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

from algo.core.errors import DomainError

ZERO: Final = Decimal("0")

#: Money is stored to the paisa. Intermediate arithmetic keeps full precision;
#: only presentation and settled cash amounts are quantised to this.
PAISA: Final = Decimal("0.01")


def dec(value: str | int | Decimal) -> Decimal:
    """Construct a Decimal safely.

    `float` is deliberately not accepted — if you have a float and you need money,
    the bug is upstream, and silently accepting it here would hide it.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Decimal(0.1) is legal Python and silently produces
        # 0.1000000000000000055511151231257827. Refusing here is the whole point
        # of the function — `bool` is caught too, since it is an int subclass
        # that has no business being a rupee amount.
        raise DomainError(
            f"refusing to build a Decimal from the float {value!r} — the precision is "
            "already lost. Pass a string, and fix whatever produced a float."
        )
    if isinstance(value, bool):
        raise DomainError(f"refusing to build a Decimal from the bool {value!r}")
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(f"cannot build Decimal from {value!r}") from exc


def quantize_paisa(amount: Decimal) -> Decimal:
    """Round a rupee amount to the paisa, banker's rounding.

    Used for settled cash figures. Banker's rounding because repeated
    round-half-up on a long trade log accumulates a measurable upward bias.
    """
    return amount.quantize(PAISA, rounding=ROUND_HALF_EVEN)


def is_on_tick(price: Decimal, tick: Decimal) -> bool:
    """True when `price` sits exactly on the instrument's tick grid."""
    _require_positive(tick, "tick")
    return (price % tick) == ZERO


def quantize_to_tick(price: Decimal, tick: Decimal, *, side: str) -> Decimal:
    """Snap a price onto the tick grid, conservatively for `side`.

    A BUY limit is rounded *down* and a SELL limit is rounded *up*, so
    quantisation can never make an order more aggressive than intended. Rounding
    to nearest would occasionally move a limit through the market, which is the
    kind of one-tick error that is invisible in a backtest and expensive live.

    `side` is "BUY" or "SELL" (accepting the raw string keeps this module free of
    an import from `enums`, so `money` sits at the very bottom of the graph).
    """
    _require_positive(tick, "tick")
    if price < ZERO:
        raise DomainError(f"negative price {price}")
    units = price / tick
    if side == "BUY":
        stepped = units.to_integral_value(rounding=ROUND_FLOOR)
    elif side == "SELL":
        # Ceiling, expressed via floor so we never touch a rounding mode that
        # behaves differently for negatives.
        floored = units.to_integral_value(rounding=ROUND_FLOOR)
        stepped = floored if floored == units else floored + 1
    else:
        raise DomainError(f"side must be BUY or SELL, got {side!r}")
    return (stepped * tick).normalize() + ZERO


def round_down_to_lot_step(lots: Decimal, lot_step: int) -> int:
    """Round a fractional lot count *down* to the tradeable step.

    Brief §8: "If the rounded size is below min lot, skip the trade and log it —
    do not round up." Rounding up would silently exceed the risk budget, so this
    function only ever rounds down. The caller checks the result against min_lots.
    """
    if lot_step < 1:
        raise DomainError(f"lot_step must be >= 1, got {lot_step}")
    if lots < ZERO:
        raise DomainError(f"negative lots {lots}")
    step = Decimal(lot_step)
    return int((lots / step).to_integral_value(rounding=ROUND_DOWN) * step)


def contract_value(price: Decimal, multiplier: Decimal, lots: int, lot_size: Decimal) -> Decimal:
    """Rupee value of `lots` contracts quoted at `price`.

    GOLDM is quoted in rupees per 10 g and trades in 100 g lots, so one point of
    quoted price is worth `multiplier` = 10 rupees per lot. Keeping `lot_size`
    separate from `multiplier` means the two never get conflated — they are
    numerically different things that happen to both be 10-ish for this contract.
    """
    if lots < 0:
        raise DomainError(f"negative lots {lots}")
    _require_positive(multiplier, "multiplier")
    _require_positive(lot_size, "lot_size")
    return price * multiplier * Decimal(lots)


def pct(amount: Decimal, percent: Decimal) -> Decimal:
    """`percent` percent of `amount`, e.g. pct(margin, Decimal("1")) -> 1% of margin."""
    return amount * percent / Decimal("100")


def _require_positive(value: Decimal, name: str) -> None:
    if value <= ZERO:
        raise DomainError(f"{name} must be positive, got {value}")
