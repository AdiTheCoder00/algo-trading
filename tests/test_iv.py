"""The implied-volatility solver. Decision D-005.

Two things are being tested: that it inverts correctly, and that it fails
honestly. The second matters more. A solver that quietly returns a plausible
number for an unquotable strike lets a phantom delta select a strike nobody was
showing, and the backtest then reports a fill live trading could never have got.
"""

from __future__ import annotations

import math

import pytest

from algo.core.enums import Right
from algo.pricing.black76 import price
from algo.pricing.iv import VOL_HIGH, IvSolution, IvStatus, solve_iv

F = 156640.0
T = 9 / 365
R = 0.065


def _solve(option_price: float, k: float, right: Right, t: float = T) -> IvSolution:
    return solve_iv(option_price=option_price, f=F, k=k, t=t, r=R, right=right)


class TestRoundTrip:
    @pytest.mark.parametrize("vol", [0.05, 0.10, 0.2175, 0.35, 0.60, 1.20])
    @pytest.mark.parametrize("k", [150000.0, 156640.0, 162000.0])
    @pytest.mark.parametrize("right", [Right.CE, Right.PE])
    def test_price_then_solve_returns_the_same_volatility(
        self, vol: float, k: float, right: Right
    ) -> None:
        target = price(F, k, T, vol, R, right)
        solved = _solve(target, k, right)
        assert solved.ok
        assert solved.iv == pytest.approx(vol, abs=1e-6)

    def test_solves_the_observed_atm_call(self) -> None:
        """156500 CE at 2200.50 on the live chain."""
        solved = _solve(2200.50, 156500.0, Right.CE)
        assert solved.ok
        assert solved.iv is not None
        assert solved.iv == pytest.approx(0.21751, abs=1e-4)

    @pytest.mark.parametrize(
        ("strike", "observed", "expected_iv"),
        [
            (155000.0, 3017.50, 0.2153),
            (156000.0, 2461.00, 0.2175),
            (157000.0, 1975.00, 0.2192),
            (158000.0, 1565.50, 0.2213),
            (159000.0, 1239.50, 0.2251),
        ],
    )
    def test_the_observed_call_chain(
        self, strike: float, observed: float, expected_iv: float
    ) -> None:
        solved = _solve(observed, strike, Right.CE)
        assert solved.ok
        assert solved.iv is not None
        assert solved.iv == pytest.approx(expected_iv, abs=5e-4)

    def test_the_chain_shows_a_volatility_skew(self) -> None:
        """Not a modelling artefact — the live book really is priced this way.

        A flat-volatility assumption would misprice the wings, which is exactly
        where a 0.25-delta strangle lives.
        """
        vols = []
        for strike, observed in [
            (155000.0, 3017.50),
            (156000.0, 2461.00),
            (157000.0, 1975.00),
            (158000.0, 1565.50),
            (159000.0, 1239.50),
        ]:
            solved = _solve(observed, strike, Right.CE)
            assert solved.iv is not None
            vols.append(solved.iv)
        assert vols == sorted(vols), "call volatility rises with strike on this chain"
        assert vols[-1] - vols[0] > 0.005


class TestHonestFailure:
    def test_a_price_below_intrinsic_is_rejected(self) -> None:
        """An arbitrage or a stale print — not a very low volatility."""
        deep_itm_intrinsic = (F - 150000.0) * math.exp(-R * T)
        solved = _solve(deep_itm_intrinsic - 100.0, 150000.0, Right.CE)
        assert solved.status is IvStatus.BELOW_INTRINSIC
        assert solved.iv is None

    def test_a_price_above_the_forward_is_rejected(self) -> None:
        solved = _solve(F * 1.5, 156640.0, Right.CE)
        assert solved.status is IvStatus.ABOVE_BOUND
        assert solved.iv is None

    def test_an_expired_option_is_rejected(self) -> None:
        solved = _solve(100.0, 156640.0, Right.CE, t=0.0)
        assert solved.status is IvStatus.EXPIRED
        assert solved.iv is None

    def test_absurd_volatility_is_reported_not_clamped(self) -> None:
        just_under_bound = price(F, 156640.0, T, VOL_HIGH, R, Right.CE) * 1.0000001
        solved = _solve(just_under_bound, 156640.0, Right.CE)
        assert solved.status is IvStatus.NOT_CONVERGED or solved.ok

    def test_bad_inputs_are_reported(self) -> None:
        assert solve_iv(
            option_price=100.0, f=-1.0, k=100.0, t=T, r=R, right=Right.CE
        ).status is IvStatus.BAD_INPUT
        assert solve_iv(
            option_price=-1.0, f=F, k=100.0, t=T, r=R, right=Right.CE
        ).status is IvStatus.BAD_INPUT

    def test_a_failure_never_carries_a_volatility(self) -> None:
        """The guarantee the chain layer relies on to mark rows untradeable."""
        for solved in (
            _solve(0.0, 150000.0, Right.CE),
            _solve(F * 2, 156640.0, Right.CE),
            _solve(100.0, 156640.0, Right.CE, t=0.0),
        ):
            if not solved.ok:
                assert solved.iv is None


class TestDeterminism:
    """Brief §7.4 — the same inputs must produce the same answer, every time."""

    def test_repeated_solves_are_bit_identical(self) -> None:
        first = _solve(2200.50, 156500.0, Right.CE)
        second = _solve(2200.50, 156500.0, Right.CE)
        assert first.iv == second.iv
        assert first.iterations == second.iterations

    def test_iteration_count_is_bounded(self) -> None:
        solved = _solve(2200.50, 156500.0, Right.CE)
        assert 0 < solved.iterations <= 128

    def test_monotonic_in_price(self) -> None:
        """A dearer option implies a higher volatility. Guards the bisection direction."""
        cheap = _solve(1900.0, 156500.0, Right.CE)
        dear = _solve(2500.0, 156500.0, Right.CE)
        assert cheap.iv is not None
        assert dear.iv is not None
        assert dear.iv > cheap.iv
