"""Black-76, checked against a published reference and against its own identities.

Brief §11 asks for unit tests against hand-computed expected values. Three kinds
appear here:

*   a published worked example, so the formula is right rather than merely
    self-consistent;
*   exact algebraic identities (put-call parity, the delta relation), which hold
    to machine precision and catch sign errors a tolerance-based test would let
    through;
*   the degenerate limits at expiry and zero volatility, which is where an engine
    stepping onto expiry day actually lands.
"""

from __future__ import annotations

import math

import pytest

from algo.core.enums import Right
from algo.core.errors import DomainError
from algo.pricing.black76 import (
    greeks,
    intrinsic,
    norm_cdf,
    norm_pdf,
    norm_ppf,
    price,
    price_bounds,
    strike_for_delta,
)

# Live GOLDM chain, 28 Aug 2026 expiry, captured 19 Aug 2026.
F = 156640.0
T = 9 / 365
R = 0.065
ATM_VOL = 0.21751  # solved from the 156500 call at 2200.50


class TestNormalFunctions:
    def test_cdf_at_known_points(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)
        assert norm_cdf(1.959963985) == pytest.approx(0.975, abs=1e-9)
        assert norm_cdf(-1.959963985) == pytest.approx(0.025, abs=1e-9)

    def test_cdf_is_symmetric(self) -> None:
        for x in (0.1, 0.5, 1.0, 2.5, 4.0):
            assert norm_cdf(x) + norm_cdf(-x) == pytest.approx(1.0, abs=1e-15)

    def test_pdf_at_zero(self) -> None:
        assert norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2 * math.pi))

    def test_ppf_at_known_points(self) -> None:
        assert norm_ppf(0.25) == pytest.approx(-0.6744897502, abs=1e-9)
        assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
        assert norm_ppf(0.975) == pytest.approx(1.9599639845, abs=1e-9)

    def test_ppf_inverts_cdf(self) -> None:
        for p in (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
            assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-12)

    def test_ppf_rejects_out_of_range(self) -> None:
        for bad in (0.0, 1.0, -0.1, 1.5):
            with pytest.raises(DomainError, match="0 < p < 1"):
                norm_ppf(bad)


class TestPublishedReference:
    """Haug, *The Complete Guide to Option Pricing Formulas*, Black-76 example.

    F = 19, K = 19, T = 0.75, r = 0.10, sigma = 0.28  ->  put = 1.7011.
    """

    def test_matches_the_published_put_price(self) -> None:
        assert price(19.0, 19.0, 0.75, 0.28, 0.10, Right.PE) == pytest.approx(1.7011, abs=5e-5)

    def test_call_equals_put_when_f_equals_k(self) -> None:
        """Parity collapses to C == P at the forward. Exact, not approximate."""
        call = price(19.0, 19.0, 0.75, 0.28, 0.10, Right.CE)
        put = price(19.0, 19.0, 0.75, 0.28, 0.10, Right.PE)
        assert call == put


class TestPutCallParity:
    @pytest.mark.parametrize("k", [153000.0, 155000.0, 156640.0, 158000.0, 160500.0])
    def test_parity_holds_at_every_strike(self, k: float) -> None:
        """C - P = (F - K) e^{-rT}, to machine precision."""
        call = price(F, k, T, ATM_VOL, R, Right.CE)
        put = price(F, k, T, ATM_VOL, R, Right.PE)
        assert call - put == pytest.approx((F - k) * math.exp(-R * T), abs=1e-8)

    @pytest.mark.parametrize("k", [153000.0, 156640.0, 160500.0])
    def test_delta_relation(self, k: float) -> None:
        """delta_call - delta_put = e^{-rT}, since N(d1) + N(-d1) = 1."""
        call = greeks(F, k, T, ATM_VOL, R, Right.CE)
        put = greeks(F, k, T, ATM_VOL, R, Right.PE)
        assert call.delta - put.delta == pytest.approx(math.exp(-R * T), abs=1e-12)

    @pytest.mark.parametrize("k", [153000.0, 156640.0, 160500.0])
    def test_gamma_and_vega_are_shared(self, k: float) -> None:
        call = greeks(F, k, T, ATM_VOL, R, Right.CE)
        put = greeks(F, k, T, ATM_VOL, R, Right.PE)
        assert call.gamma == pytest.approx(put.gamma, rel=1e-12)
        assert call.vega == pytest.approx(put.vega, rel=1e-12)


class TestGreekSigns:
    """The signs a short strangle depends on being right."""

    def test_long_options_lose_value_with_time(self) -> None:
        for right in (Right.CE, Right.PE):
            assert greeks(F, 160500.0, T, ATM_VOL, R, right).theta < 0

    def test_a_short_strangle_earns_theta(self) -> None:
        """Selling both legs flips the sign — this is where the credit comes from."""
        call = greeks(F, 160500.0, T, ATM_VOL, R, Right.CE)
        put = greeks(F, 153000.0, T, ATM_VOL, R, Right.PE)
        assert -(call.theta + put.theta) > 0

    def test_a_short_strangle_is_short_gamma_and_short_vega(self) -> None:
        call = greeks(F, 160500.0, T, ATM_VOL, R, Right.CE)
        put = greeks(F, 153000.0, T, ATM_VOL, R, Right.PE)
        assert -(call.gamma + put.gamma) < 0
        assert -(call.vega + put.vega) < 0

    def test_call_delta_positive_put_delta_negative(self) -> None:
        assert greeks(F, 160500.0, T, ATM_VOL, R, Right.CE).delta > 0
        assert greeks(F, 153000.0, T, ATM_VOL, R, Right.PE).delta < 0

    def test_a_strangle_at_matched_deltas_is_near_delta_neutral(self) -> None:
        call_k = strike_for_delta(F, T, ATM_VOL, R, 0.25, Right.CE)
        put_k = strike_for_delta(F, T, ATM_VOL, R, 0.25, Right.PE)
        combined = (
            greeks(F, call_k, T, ATM_VOL, R, Right.CE).delta
            + greeks(F, put_k, T, ATM_VOL, R, Right.PE).delta
        )
        assert abs(combined) < 1e-9


class TestStrikeForDelta:
    def test_round_trips(self) -> None:
        for target in (0.10, 0.20, 0.25, 0.30, 0.45):
            for right in (Right.CE, Right.PE):
                k = strike_for_delta(F, T, ATM_VOL, R, target, right)
                assert abs(greeks(F, k, T, ATM_VOL, R, right).delta) == pytest.approx(
                    target, abs=1e-9
                )

    def test_the_observed_chain_strikes(self) -> None:
        """The claim made from the live chain: 0.25 delta sits near 160,500 / 153,000."""
        call_k = strike_for_delta(F, T, ATM_VOL, R, 0.25, Right.CE)
        put_k = strike_for_delta(F, T, ATM_VOL, R, 0.25, Right.PE)
        assert call_k == pytest.approx(160377.0, abs=1.0)
        assert put_k == pytest.approx(153168.6, abs=1.0)
        assert round(call_k / 500) * 500 == 160500
        assert round(put_k / 500) * 500 == 153000

    def test_a_fixed_delta_moves_further_out_as_expiry_lengthens(self) -> None:
        """The liquidity trade-off: entering earlier in the cycle asks for a
        strike further into the thin tail, not closer to the money."""
        near = strike_for_delta(F, 9 / 365, ATM_VOL, R, 0.25, Right.CE)
        far = strike_for_delta(F, 30 / 365, ATM_VOL, R, 0.25, Right.CE)
        assert far > near
        assert far - near > 2000

    def test_lower_delta_is_further_out(self) -> None:
        tens = strike_for_delta(F, T, ATM_VOL, R, 0.10, Right.CE)
        forties = strike_for_delta(F, T, ATM_VOL, R, 0.40, Right.CE)
        assert tens > forties

    def test_rejects_impossible_targets(self) -> None:
        for bad in (0.0, 1.0, -0.5, 1.2):
            with pytest.raises(DomainError, match="target delta"):
                strike_for_delta(F, T, ATM_VOL, R, bad, Right.CE)


class TestDegenerateCases:
    def test_at_expiry_price_is_discounted_intrinsic(self) -> None:
        assert price(F, 150000.0, 0.0, ATM_VOL, R, Right.CE) == pytest.approx(6640.0)
        assert price(F, 160000.0, 0.0, ATM_VOL, R, Right.CE) == pytest.approx(0.0)
        assert price(F, 160000.0, 0.0, ATM_VOL, R, Right.PE) == pytest.approx(3360.0)

    def test_zero_volatility_is_discounted_intrinsic(self) -> None:
        assert price(F, 150000.0, T, 0.0, R, Right.CE) == pytest.approx(
            math.exp(-R * T) * 6640.0
        )

    def test_expiry_deltas_are_binary(self) -> None:
        deep_itm = greeks(F, 150000.0, 0.0, ATM_VOL, R, Right.CE)
        deep_otm = greeks(F, 170000.0, 0.0, ATM_VOL, R, Right.CE)
        assert deep_itm.delta == pytest.approx(1.0)
        assert deep_otm.delta == 0.0

    def test_cannot_invert_delta_at_expiry(self) -> None:
        with pytest.raises(DomainError, match="zero time or zero volatility"):
            strike_for_delta(F, 0.0, ATM_VOL, R, 0.25, Right.CE)

    @pytest.mark.parametrize(
        ("f", "k", "t", "vol"),
        [(-1.0, 100.0, 0.1, 0.2), (100.0, 0.0, 0.1, 0.2), (100.0, 100.0, -0.1, 0.2),
         (100.0, 100.0, 0.1, -0.2)],
    )
    def test_bad_inputs_are_refused(self, f: float, k: float, t: float, vol: float) -> None:
        with pytest.raises(DomainError):
            price(f, k, t, vol, R, Right.CE)


class TestBounds:
    def test_price_sits_inside_the_no_arbitrage_bounds(self) -> None:
        for k in (150000.0, 156640.0, 165000.0):
            for right in (Right.CE, Right.PE):
                lower, upper = price_bounds(F, k, T, R, right)
                actual = price(F, k, T, ATM_VOL, R, right)
                assert lower <= actual <= upper

    def test_intrinsic_matches_payoff(self) -> None:
        assert intrinsic(F, 150000.0, Right.CE) == 6640.0
        assert intrinsic(F, 160000.0, Right.CE) == 0.0
        assert intrinsic(F, 160000.0, Right.PE) == 3360.0
        assert intrinsic(F, 150000.0, Right.PE) == 0.0
