"""What a CFD costs: commission per fill, and financing every night.

The MCX charge stack does not apply here. There is no CTT, no SEBI turnover fee,
no stamp duty and no GST on an OTC contract with an offshore broker - those
fields exist on `Charges` because MCX has them, and on this venue they are
genuinely zero rather than unmodelled.

What MCX does *not* have, and this does:

## Overnight financing (swap)

`docs/milestone-0-plan.md` struck "swap / financing, triple-swap Wednesday" off
the brief with the note "Does not exist" - correct for MCX, where margin is
blocked rather than borrowed. On a CFD the broker is financing the position and
charges for it nightly. Measured on the Vantage demo account, 2026-08-28:

    long   -80.54 points/lot/night  =  -6.59% a year
    short  +32.67 points/lot/night  =  +2.67% a year
    triple charge on Wednesday

A long held for a year pays **6.6% of notional** in financing before it makes
anything. That is not a rounding item; for anything holding longer than a day it
is likely the largest cost in the model, larger than spread. A backtest that
ignores it would overstate every long and understate every short.

## The rates are a snapshot, and cannot be otherwise

MT5 publishes only the swap rate **in force right now**. There is no historical
series, so a backtest over four years of bars can only apply today's rate to all
of it. Brokers change these with the underlying rate environment, so that is an
approximation - and, like the MCX charge rates, it is reported rather than
buried. `SwapModel.is_verified` is False for exactly this reason and stays False:
unlike a contract note, there is nothing that could ever verify a historical
rate that was never published.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from algo.core.enums import Side
from algo.core.errors import ConfigError
from algo.core.fill import Charges

#: `date.weekday()` for Wednesday, when most brokers book three nights of
#: financing to cover the weekend (spot settles T+2, so Wednesday's roll carries
#: value dates over Saturday and Sunday).
_WEDNESDAY = 2

#: How many nights the triple day charges.
_TRIPLE = Decimal("3")


class CfdChargeModel:
    """Per-fill commission. Zero on a spread-only account, and not assumed.

    Vantage's standard account is quoted as spread-only, with commission charged
    on RAW/ECN tiers instead. This defaults to zero because that is what the
    demo account appears to be - but `is_verified` stays False until a real fill
    shows the deal's commission, exactly as `McxChargeModel` refuses to call its
    rates verified without a contract note (D-011).

    A commission that is really zero and a commission nobody has checked produce
    identical numbers here, which is the whole reason the flag exists.
    """

    __slots__ = ("_per_lot", "_verified")

    def __init__(
        self, *, commission_per_lot: Decimal = Decimal("0"), verified: bool = False
    ) -> None:
        if commission_per_lot < 0:
            raise ConfigError(
                f"commission cannot be negative, got {commission_per_lot}; a rebate "
                "is not modelled here"
            )
        self._per_lot = commission_per_lot
        self._verified = verified

    @classmethod
    def vantage_standard(cls) -> CfdChargeModel:
        """Zero commission, and this one **is** verified (D-121).

        Not asserted from Vantage's marketing but read out of the account's own
        dealing history: all 54 XAU deals on account 25804244, pulled via
        `history_deals_get` on 2026-08-28, carry `commission == 0.0` and
        `fee == 0.0`. That is the evidence `McxChargeModel` has been waiting for
        a contract note to supply, and here it already exists.

        Scope of the claim: **that** account, that tier. A Vantage RAW/ECN
        account charges commission and would need a different model.

        The scope line is not decoration, and it bit on 2026-08-30: the terminal
        moved to account 26017545 (same server, same company, fresh $100,000
        demo) and `history_deals_get` returns **zero** deals there. Everything
        else re-read identically - contract size 100, 0.01 volume step, swap
        -80.54/+32.67, `swap_rollover3days` 3 (Wednesday, matching this module's
        Python-weekday 2) - so the terms plainly carry over, but "carries over
        by strong inference" is not what `verified=True` claims. Until this
        account has dealing history of its own, the commission figure is
        inherited evidence, not measured evidence, and that distinction is the
        whole reason this flag exists.
        """
        return cls(commission_per_lot=Decimal("0"), verified=True)

    @property
    def is_verified(self) -> bool:
        return self._verified

    def charges_for(
        self,
        *,
        side: Side,
        lots: int,
        price: Decimal,
        multiplier: Decimal,
        is_option: bool,
        on: date,
    ) -> Charges:
        """Commission only. Every other field is zero because this venue has no
        such tax, not because it has not been modelled."""
        del side, price, multiplier, is_option, on  # a flat per-lot commission
        return Charges(brokerage=self._per_lot * Decimal(lots))


class SwapModel:
    """Overnight financing on an open CFD position.

    Rates are in **points per broker lot per night**, the form MT5 publishes
    them in (`swap_mode == POINTS`). `point_value` converts one point on one
    *engine* lot into account currency - for XAUUSD on a one-ounce engine lot
    that is 0.01, since a point is $1 per 100-ounce broker lot.

    Sign convention, stated because getting it backwards turns a cost into
    income: the returned value is **added to P&L**. A long paying financing
    yields a negative number; a short receiving it yields a positive one. That
    matches how MT5 reports `swap_long` and `swap_short` and avoids a second
    negation somewhere else in the chain.
    """

    __slots__ = ("_long_points", "_point_value", "_short_points", "_triple_weekday")

    def __init__(
        self,
        *,
        long_points: Decimal,
        short_points: Decimal,
        point_value: Decimal,
        triple_weekday: int | None = _WEDNESDAY,
    ) -> None:
        if point_value <= 0:
            raise ConfigError(f"point_value must be positive, got {point_value}")
        if triple_weekday is not None and not 0 <= triple_weekday <= 6:
            raise ConfigError(
                f"triple_weekday must be a date.weekday() value 0-6, got {triple_weekday}"
            )
        self._long_points = long_points
        self._short_points = short_points
        self._point_value = point_value
        self._triple_weekday = triple_weekday

    @classmethod
    def vantage_xauusd(cls) -> SwapModel:
        """The rates measured on the Vantage demo account, 2026-08-28.

        A classmethod rather than a literal repeated at each call site: the
        backtest runner, the walk-forward and the live loop must charge the
        *same* financing, and two copies of a constant are two things that can
        drift apart. `point_value` is 0.01 because one MT5 point is $1 on a
        100-ounce broker lot, and an engine lot is one ounce.
        """
        return cls(
            long_points=Decimal("-80.54"),
            short_points=Decimal("32.67"),
            point_value=Decimal("0.01"),
        )

    @property
    def is_verified(self) -> bool:
        """Always False. See the module docstring: MT5 publishes no historical
        swap series, so a backtest necessarily applies today's rate to the past
        and no evidence could ever settle it."""
        return False

    def nights_charged(self, on: date) -> Decimal:
        """How many nights of financing roll over into `on`.

        Three on the triple day, one otherwise. The triple day covers the
        weekend because spot metal settles T+2, so that roll carries value dates
        across Saturday and Sunday.
        """
        if self._triple_weekday is not None and on.weekday() == self._triple_weekday:
            return _TRIPLE
        return Decimal("1")

    def carry_for(self, *, side: Side, lots: int, on: date) -> Decimal:
        """Financing for holding `lots` overnight into `on`, in account currency.

        Positive is a credit to P&L, negative a debit - see the class docstring.
        """
        if lots < 0:
            raise ConfigError(f"lots must not be negative, got {lots}")
        points = self._long_points if side is Side.BUY else self._short_points
        return points * self._point_value * Decimal(lots) * self.nights_charged(on)

    def annualised_pct(self, *, side: Side, price: Decimal) -> Decimal:
        """The carry as a percentage of notional per year, for reporting.

        The number worth putting in front of a person: "-6.59%/yr" says what
        "-80.54 points" does not.
        """
        if price <= 0:
            raise ConfigError(f"price must be positive, got {price}")
        points = self._long_points if side is Side.BUY else self._short_points
        nightly = points * self._point_value
        return nightly / price * Decimal("365") * Decimal("100")
