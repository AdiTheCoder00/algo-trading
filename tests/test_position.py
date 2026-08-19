"""Position accounting, worked through a short strangle leg.

Quantities are signed, so a short is a negative quantity and the P&L arithmetic
needs no branch on direction. `multiplier` converts one point of quoted price into
rupees per lot — 10 for GOLDM, which quotes per 10 g and trades 100 g.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from algo.core.enums import Side
from algo.core.fill import Charges, Fill
from algo.core.instrument import OptionId
from algo.core.position import Position
from algo.core.timeutil import utc

MULT = Decimal("10")


def _fill(
    option: OptionId,
    side: Side,
    lots: int,
    price: str,
    *,
    at: datetime | None = None,
    charges: Charges | None = None,
) -> Fill:
    return Fill(
        fill_id=f"f{price}{side}",
        client_order_id="coid",
        signal_id="sig",
        instrument=option,
        side=side,
        lots=lots,
        qty=Decimal(lots),  # MCX order quantity is expressed in lots
        price=Decimal(price),
        ts=at or utc(2026, 8, 19, 4, 0),
        charges=charges or Charges(),
    )


def _flat(option: OptionId) -> Position:
    return Position(instrument=option, multiplier=MULT)


class TestOpeningAShort:
    def test_selling_creates_a_negative_quantity(self, goldm_call: OptionId) -> None:
        position = _flat(goldm_call).apply(_fill(goldm_call, Side.SELL, 1, "900"))
        assert position.qty == Decimal("-1")
        assert position.lots == -1
        assert position.average_price == Decimal("900")
        assert position.is_short
        assert position.side is Side.SELL

    def test_a_short_gains_when_the_premium_falls(self, goldm_call: OptionId) -> None:
        position = _flat(goldm_call).apply(_fill(goldm_call, Side.SELL, 1, "900"))
        assert position.unrealised_pnl(Decimal("850")) == Decimal("500")

    def test_a_short_loses_when_the_premium_rises(self, goldm_call: OptionId) -> None:
        position = _flat(goldm_call).apply(_fill(goldm_call, Side.SELL, 1, "900"))
        assert position.unrealised_pnl(Decimal("1000")) == Decimal("-1000")

    def test_todays_move_against_a_short_call(self, goldm_call: OptionId) -> None:
        """The chain showed calls up ~200-310% in one session.

        A leg sold near 300 and now marked at 1239.50 is 9,395 rupees underwater
        on one lot — against a stop of roughly 1,000 at 1% of margin.
        """
        position = _flat(goldm_call).apply(_fill(goldm_call, Side.SELL, 1, "300"))
        assert position.unrealised_pnl(Decimal("1239.50")) == Decimal("-9395.00")


class TestScalingAndClosing:
    def test_adding_reweights_the_average(self, goldm_call: OptionId) -> None:
        position = (
            _flat(goldm_call)
            .apply(_fill(goldm_call, Side.SELL, 1, "900"))
            .apply(_fill(goldm_call, Side.SELL, 1, "1100"))
        )
        assert position.qty == Decimal("-2")
        assert position.average_price == Decimal("1000")

    def test_partial_close_realises_against_the_average(self, goldm_call: OptionId) -> None:
        position = (
            _flat(goldm_call)
            .apply(_fill(goldm_call, Side.SELL, 1, "900"))
            .apply(_fill(goldm_call, Side.SELL, 1, "1100"))
            .apply(_fill(goldm_call, Side.BUY, 1, "950"))
        )
        assert position.qty == Decimal("-1")
        assert position.realised_pnl == Decimal("500")
        assert position.average_price == Decimal("1000"), "the remaining lot keeps its basis"

    def test_full_close_flattens(self, goldm_call: OptionId) -> None:
        position = (
            _flat(goldm_call)
            .apply(_fill(goldm_call, Side.SELL, 1, "900"))
            .apply(_fill(goldm_call, Side.BUY, 1, "800"))
        )
        assert position.is_flat
        assert position.realised_pnl == Decimal("1000")
        assert position.unrealised_pnl(Decimal("5000")) == Decimal("0")
        assert position.opened_at is None

    def test_crossing_through_flat_realises_then_reopens(self, goldm_call: OptionId) -> None:
        position = (
            _flat(goldm_call)
            .apply(_fill(goldm_call, Side.SELL, 1, "1000"))
            .apply(_fill(goldm_call, Side.BUY, 3, "900"))
        )
        assert position.realised_pnl == Decimal("1000")
        assert position.qty == Decimal("2")
        assert position.average_price == Decimal("900")
        assert not position.is_short


class TestChargesAccumulate:
    def test_charges_are_itemised_and_summed(self, goldm_call: OptionId) -> None:
        entry = Charges(brokerage=Decimal("20"), ctt=Decimal("7.50"), gst=Decimal("3.60"))
        exit_ = Charges(brokerage=Decimal("20"), stamp_duty=Decimal("0.30"), gst=Decimal("3.60"))
        position = (
            _flat(goldm_call)
            .apply(_fill(goldm_call, Side.SELL, 1, "900", charges=entry))
            .apply(_fill(goldm_call, Side.BUY, 1, "800", charges=exit_))
        )
        assert position.charges.brokerage == Decimal("40")
        assert position.charges.ctt == Decimal("7.50")
        assert position.charges.stamp_duty == Decimal("0.30")
        assert position.charges.total == Decimal("55.00")

    def test_net_is_gross_minus_charges(self, goldm_call: OptionId) -> None:
        charges = Charges(brokerage=Decimal("40"), ctt=Decimal("7.50"))
        position = (
            _flat(goldm_call)
            .apply(_fill(goldm_call, Side.SELL, 1, "900", charges=charges))
            .apply(_fill(goldm_call, Side.BUY, 1, "800"))
        )
        assert position.realised_pnl - position.charges.total == Decimal("952.50")


class TestImmutability:
    def test_applying_a_fill_returns_a_new_position(self, goldm_call: OptionId) -> None:
        """The ledger and the position book must not be able to disagree."""
        original = _flat(goldm_call)
        updated = original.apply(_fill(goldm_call, Side.SELL, 1, "900"))
        assert original.is_flat
        assert updated is not original
        assert updated.qty == Decimal("-1")
