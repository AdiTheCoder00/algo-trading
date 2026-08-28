"""CFD costs: commission per fill, financing every night (D-121).

The sign convention is the thing most worth pinning. `carry_for` returns a value
that is **added to P&L**, so a long paying financing must come back negative. Get
that backwards and a 6.6%-a-year cost silently becomes 6.6%-a-year of income,
which would make every long-biased backtest look wonderful.

Rates below are the real ones, measured from the Vantage demo account on
2026-08-28: long -80.54 points, short +32.67, triple on Wednesday.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from algo.core.enums import Side
from algo.core.errors import ConfigError
from algo.costs.cfd import CfdChargeModel, SwapModel

#: One point is $1 per 100-ounce broker lot, so $0.01 on a one-ounce engine lot.
POINT_VALUE = Decimal("0.01")
LONG_POINTS = Decimal("-80.54")
SHORT_POINTS = Decimal("32.67")

MONDAY = date(2026, 8, 24)
WEDNESDAY = date(2026, 8, 26)
FRIDAY = date(2026, 8, 28)


def _swap(**kwargs: object) -> SwapModel:
    return SwapModel(  # type: ignore[arg-type]
        long_points=kwargs.get("long_points", LONG_POINTS),
        short_points=kwargs.get("short_points", SHORT_POINTS),
        point_value=kwargs.get("point_value", POINT_VALUE),
        **{k: v for k, v in kwargs.items() if k == "triple_weekday"},
    )


class TestTheSignConvention:
    """Backwards here turns the largest cost in the model into income."""

    def test_a_long_pays(self) -> None:
        assert _swap().carry_for(side=Side.BUY, lots=1, on=MONDAY) < 0

    def test_a_short_receives(self) -> None:
        assert _swap().carry_for(side=Side.SELL, lots=1, on=MONDAY) > 0

    def test_the_long_debit_matches_the_broker_figure(self) -> None:
        """-80.54 points on a 100-ounce lot is -$80.54; per ounce, -$0.8054."""
        assert _swap().carry_for(side=Side.BUY, lots=1, on=MONDAY) == Decimal("-0.8054")

    def test_the_short_credit_matches_the_broker_figure(self) -> None:
        assert _swap().carry_for(side=Side.SELL, lots=1, on=MONDAY) == Decimal("0.3267")


class TestTripleSwapWednesday:
    """The model `docs/milestone-0-plan.md` struck out for MCX, back because this
    venue really has it."""

    def test_wednesday_charges_three_nights(self) -> None:
        swap = _swap()

        assert swap.nights_charged(WEDNESDAY) == Decimal("3")
        assert swap.carry_for(side=Side.BUY, lots=1, on=WEDNESDAY) == Decimal("-2.4162")

    @pytest.mark.parametrize("day", [MONDAY, FRIDAY, date(2026, 8, 25)])
    def test_every_other_day_charges_one(self, day: date) -> None:
        assert _swap().nights_charged(day) == Decimal("1")

    def test_the_triple_day_is_configurable_and_can_be_switched_off(self) -> None:
        """Not every broker rolls on Wednesday, and some do not triple at all."""
        assert _swap(triple_weekday=None).nights_charged(WEDNESDAY) == Decimal("1")
        assert _swap(triple_weekday=4).nights_charged(FRIDAY) == Decimal("3")

    def test_a_nonsense_weekday_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="0-6"):
            _swap(triple_weekday=9)


class TestItScalesWithSize:
    def test_carry_is_linear_in_lots(self) -> None:
        swap = _swap()
        one = swap.carry_for(side=Side.BUY, lots=1, on=MONDAY)

        assert swap.carry_for(side=Side.BUY, lots=100, on=MONDAY) == one * 100

    def test_a_flat_position_costs_nothing(self) -> None:
        assert _swap().carry_for(side=Side.BUY, lots=0, on=MONDAY) == 0

    def test_negative_lots_are_refused(self) -> None:
        """Direction is `side`; a negative size would double-encode it."""
        with pytest.raises(ConfigError, match="must not be negative"):
            _swap().carry_for(side=Side.BUY, lots=-1, on=MONDAY)


class TestTheAnnualisedFigure:
    """The number worth showing a person: '-6.59%/yr' says what '-80.54 points'
    does not."""

    def test_the_long_rate_matches_what_was_measured(self) -> None:
        rate = _swap().annualised_pct(side=Side.BUY, price=Decimal("4463.08"))

        assert round(rate, 2) == Decimal("-6.59")

    def test_the_short_rate_matches(self) -> None:
        rate = _swap().annualised_pct(side=Side.SELL, price=Decimal("4463.08"))

        assert round(rate, 2) == Decimal("2.67")

    def test_a_zero_price_is_refused_rather_than_dividing(self) -> None:
        with pytest.raises(ConfigError, match="must be positive"):
            _swap().annualised_pct(side=Side.BUY, price=Decimal("0"))


class TestSwapIsNeverCalledVerified:
    def test_it_reports_itself_unverified(self) -> None:
        """MT5 publishes no historical swap series, so a backtest applies today's
        rate to four years of bars. Nothing could ever verify that."""
        assert _swap().is_verified is False


class TestCommission:
    def test_it_defaults_to_zero_but_unverified(self) -> None:
        """Zero-because-checked and zero-because-nobody-looked produce identical
        numbers, which is exactly why the flag exists (D-011)."""
        model = CfdChargeModel()

        charges = model.charges_for(
            side=Side.BUY,
            lots=10,
            price=Decimal("4459"),
            multiplier=Decimal("1"),
            is_option=False,
            on=MONDAY,
        )

        assert charges.total == 0
        assert model.is_verified is False

    def test_a_commission_is_per_lot(self) -> None:
        model = CfdChargeModel(commission_per_lot=Decimal("0.07"), verified=True)

        charges = model.charges_for(
            side=Side.SELL,
            lots=100,
            price=Decimal("4459"),
            multiplier=Decimal("1"),
            is_option=False,
            on=MONDAY,
        )

        assert charges.brokerage == Decimal("7.00")
        assert model.is_verified is True

    def test_the_mcx_taxes_are_zero_because_this_venue_has_none(self) -> None:
        """Not unmodelled - absent. An OTC contract with an offshore broker
        attracts no CTT, SEBI fee, stamp duty or GST."""
        charges = CfdChargeModel(commission_per_lot=Decimal("1")).charges_for(
            side=Side.BUY,
            lots=1,
            price=Decimal("4459"),
            multiplier=Decimal("1"),
            is_option=False,
            on=MONDAY,
        )

        assert charges.ctt == 0
        assert charges.sebi_fee == 0
        assert charges.stamp_duty == 0
        assert charges.gst == 0
        assert charges.exchange_txn == 0

    def test_a_negative_commission_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="cannot be negative"):
            CfdChargeModel(commission_per_lot=Decimal("-1"))


class TestTheVantageStandardModelIsEvidenced:
    """Unlike the MCX rates, this one has its contract note: 54 real XAU deals
    on the account, every one with commission 0.0 and fee 0.0."""

    def test_it_is_verified(self) -> None:
        model = CfdChargeModel.vantage_standard()

        assert model.is_verified is True

    def test_it_charges_nothing(self) -> None:
        charges = CfdChargeModel.vantage_standard().charges_for(
            side=Side.BUY,
            lots=1000,
            price=Decimal("4459"),
            multiplier=Decimal("1"),
            is_option=False,
            on=MONDAY,
        )

        assert charges.total == 0

    def test_the_bare_constructor_stays_unverified(self) -> None:
        """Same numbers, different epistemic status - and the flag is the only
        thing that can tell them apart."""
        assert CfdChargeModel().is_verified is False
        assert CfdChargeModel().charges_for(
            side=Side.BUY,
            lots=1000,
            price=Decimal("4459"),
            multiplier=Decimal("1"),
            is_option=False,
            on=MONDAY,
        ).total == CfdChargeModel.vantage_standard().charges_for(
            side=Side.BUY,
            lots=1000,
            price=Decimal("4459"),
            multiplier=Decimal("1"),
            is_option=False,
            on=MONDAY,
        ).total
