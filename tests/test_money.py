"""Decimal money math. Brief §2.5 and §8."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from algo.core.errors import DomainError
from algo.core.money import (
    contract_value,
    dec,
    is_on_tick,
    pct,
    quantize_paisa,
    quantize_to_tick,
    round_down_to_lot_step,
)

TICK = Decimal("0.50")  # GOLDM options, observed


class TestDecimalConstruction:
    def test_floats_are_refused(self) -> None:
        """A float here means the precision was already lost upstream.

        Note `Decimal(0.1)` is perfectly legal Python — which is exactly why this
        has to be an explicit refusal rather than something the type system
        catches on its own.
        """
        with pytest.raises(DomainError, match="refusing to build a Decimal from the float"):
            dec(0.1)  # type: ignore[arg-type]

    def test_strings_are_exact(self) -> None:
        assert dec("156640.05") * 100 == Decimal("15664005")

    def test_the_reason_floats_are_banned(self) -> None:
        assert Decimal(0.1) != Decimal("0.1")  # noqa: RUF032 - demonstrating the hazard


class TestTickQuantisation:
    @pytest.mark.parametrize(
        ("price", "side", "expected"),
        [
            ("156640.00", "BUY", "156640.0"),
            ("156640.00", "SELL", "156640.0"),
            ("156640.30", "BUY", "156640.0"),
            ("156640.30", "SELL", "156640.5"),
            ("156640.70", "BUY", "156640.5"),
            ("156640.70", "SELL", "156641.0"),
        ],
    )
    def test_rounds_conservatively_for_the_side(
        self, price: str, side: str, expected: str
    ) -> None:
        assert quantize_to_tick(Decimal(price), TICK, side=side) == Decimal(expected)

    def test_a_buy_limit_is_never_raised(self) -> None:
        """Rounding to nearest would sometimes push a limit through the market."""
        for raw in ("100.01", "100.49", "100.99"):
            assert quantize_to_tick(Decimal(raw), TICK, side="BUY") <= Decimal(raw)

    def test_a_sell_limit_is_never_lowered(self) -> None:
        for raw in ("100.01", "100.49", "100.99"):
            assert quantize_to_tick(Decimal(raw), TICK, side="SELL") >= Decimal(raw)

    def test_result_is_always_on_the_grid(self) -> None:
        for raw in ("0.01", "3.33", "156640.37", "999999.99"):
            for side in ("BUY", "SELL"):
                assert is_on_tick(quantize_to_tick(Decimal(raw), TICK, side=side), TICK)

    def test_bad_inputs_are_refused(self) -> None:
        with pytest.raises(DomainError, match="tick must be positive"):
            quantize_to_tick(Decimal("10"), Decimal("0"), side="BUY")
        with pytest.raises(DomainError, match="negative price"):
            quantize_to_tick(Decimal("-1"), TICK, side="BUY")
        with pytest.raises(DomainError, match="side must be"):
            quantize_to_tick(Decimal("10"), TICK, side="SIDEWAYS")


class TestLotRounding:
    """Brief §8: never round a position size up."""

    @pytest.mark.parametrize(
        ("lots", "step", "expected"),
        [("3.9", 1, 3), ("3.0", 1, 3), ("0.9", 1, 0), ("7.5", 5, 5), ("11.2", 5, 10)],
    )
    def test_always_rounds_down(self, lots: str, step: int, expected: int) -> None:
        assert round_down_to_lot_step(Decimal(lots), step) == expected

    def test_sub_minimum_size_becomes_zero_not_one(self) -> None:
        """The caller then skips the trade and logs it, per §8 — it does not round up."""
        assert round_down_to_lot_step(Decimal("0.99"), 1) == 0

    def test_bad_step_is_refused(self) -> None:
        with pytest.raises(DomainError, match="lot_step must be"):
            round_down_to_lot_step(Decimal("5"), 0)


class TestContractValue:
    def test_goldm_lot_value(self) -> None:
        """One GOLDM lot at the observed price: 156640 per 10 g x multiplier 10."""
        value = contract_value(
            Decimal("156640"), multiplier=Decimal("10"), lots=1, lot_size=Decimal("100")
        )
        assert value == Decimal("1566400")

    def test_scales_with_lots(self) -> None:
        one = contract_value(Decimal("100"), Decimal("10"), 1, Decimal("100"))
        three = contract_value(Decimal("100"), Decimal("10"), 3, Decimal("100"))
        assert three == one * 3


class TestPercentAndRounding:
    def test_one_percent_of_margin(self) -> None:
        """The configured stop: 1% of margin blocked."""
        assert pct(Decimal("100000"), Decimal("1")) == Decimal("1000")
        assert pct(Decimal("100000"), Decimal("2")) == Decimal("2000")

    def test_paisa_rounding_is_bankers(self) -> None:
        assert quantize_paisa(Decimal("1.005")) == Decimal("1.00")
        assert quantize_paisa(Decimal("1.015")) == Decimal("1.02")

    def test_bankers_rounding_beats_half_up_on_a_long_series(self) -> None:
        """Round-half-up drifts one way; half-even splits ties between neighbours.

        Every tie is exactly on the half-paisa. Under ROUND_HALF_UP all of them
        round the same direction, so the error grows linearly with the number of
        trades. Under ROUND_HALF_EVEN they alternate with the parity of the digit
        before, so the errors cancel.
        """
        ties = [Decimal(f"{n}.{cents:02d}5") for n in range(1, 101) for cents in (0, 1)]
        exact = sum(ties, Decimal("0"))

        half_even = sum((quantize_paisa(t) for t in ties), Decimal("0"))
        half_up = sum(
            (t.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for t in ties), Decimal("0")
        )

        assert abs(half_even - exact) < abs(half_up - exact)
        assert half_up - exact == Decimal("1.000"), "half-up drifts up on every tie"
        assert half_even - exact == Decimal("0.000"), "half-even cancels"
