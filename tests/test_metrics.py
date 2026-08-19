"""Performance metrics. Brief §10.

The behaviour under test is mostly about honesty: a metric that cannot be
computed must come back as `None` rather than as a number that reads like an
answer. A Sharpe of 0.0 says "no edge"; `None` says "not enough data", and with
twelve trades a year the second is almost always the truthful one.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from algo.core.timeutil import utc
from algo.portfolio.book import EquityPoint
from algo.reporting.metrics import compute

START = Decimal("1000000")


def _curve(equities: list[str], *, exposed_from: int | None = None) -> tuple[EquityPoint, ...]:
    base = utc(2026, 8, 19, 4, 0)
    return tuple(
        EquityPoint(
            ts=base + timedelta(minutes=30 * i),
            cash=Decimal(e),
            market_value=Decimal("0"),
            equity=Decimal(e),
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("0"),
            charges=Decimal("0"),
            open_positions=1 if exposed_from is not None and i >= exposed_from else 0,
        )
        for i, e in enumerate(equities)
    )


class TestBasics:
    def test_net_pnl_and_return(self) -> None:
        m = compute(_curve(["1000000", "1010000"]), trade_count=1, total_cost=Decimal("500"))
        assert m.net_pnl == Decimal("10000")
        assert m.return_pct == Decimal("1")
        assert m.gross_pnl == Decimal("10500")

    def test_cost_drag_is_a_share_of_gross(self) -> None:
        m = compute(_curve(["1000000", "1009000"]), trade_count=1, total_cost=Decimal("1000"))
        assert m.gross_pnl == Decimal("10000")
        assert m.cost_drag_pct == Decimal("10")

    def test_cost_drag_is_none_when_gross_is_zero(self) -> None:
        """Rather than dividing by zero and reporting something."""
        m = compute(_curve(["1000000", "999000"]), trade_count=1, total_cost=Decimal("1000"))
        assert m.gross_pnl == Decimal("0")
        assert m.cost_drag_pct is None

    def test_exposure_counts_bars_with_a_position(self) -> None:
        m = compute(
            _curve(["1000000"] * 10, exposed_from=5), trade_count=1, total_cost=Decimal("0")
        )
        assert m.exposure_pct == Decimal("50")


class TestDrawdown:
    def test_depth_and_duration(self) -> None:
        """Brief §10 asks for both — a shallow drawdown lasting two years is a
        different experience from a deep one lasting a week."""
        m = compute(
            _curve(["1000000", "1100000", "1050000", "900000", "1200000"]),
            trade_count=4,
            total_cost=Decimal("0"),
        )
        assert m.max_drawdown is not None
        assert m.max_drawdown.depth == Decimal("200000")
        assert m.max_drawdown.peak_equity == Decimal("1100000")
        assert m.max_drawdown.trough_equity == Decimal("900000")
        assert m.max_drawdown.duration == timedelta(minutes=60)

    def test_a_curve_that_only_rises_has_no_drawdown(self) -> None:
        m = compute(_curve(["100", "110", "120"]), trade_count=2, total_cost=Decimal("0"))
        assert m.max_drawdown is None
        assert m.calmar is None


class TestRatiosRefuseToGuess:
    def test_too_few_observations_gives_none(self) -> None:
        m = compute(_curve(["100", "101", "102"]), trade_count=1, total_cost=Decimal("0"))
        assert m.sharpe is None
        assert m.sortino is None

    def test_a_constant_curve_has_no_sharpe(self) -> None:
        """Zero deviation is undefined, not infinite."""
        m = compute(_curve(["100"] * 10), trade_count=1, total_cost=Decimal("0"))
        assert m.sharpe is None

    def test_no_losing_periods_gives_no_sortino(self) -> None:
        m = compute(
            _curve(["100", "101", "102", "103", "104", "105"]),
            trade_count=5,
            total_cost=Decimal("0"),
        )
        assert m.sortino is None

    def test_sharpe_is_computed_when_there_is_enough_variation(self) -> None:
        m = compute(
            _curve(["1000", "1010", "1005", "1020", "1015", "1030"]),
            trade_count=5,
            total_cost=Decimal("0"),
        )
        assert m.sharpe is not None
        assert m.sharpe > 0

    def test_sortino_penalises_only_the_downside(self) -> None:
        m = compute(
            _curve(["1000", "1010", "1005", "1020", "1015", "1030", "1025", "1040"]),
            trade_count=7,
            total_cost=Decimal("0"),
        )
        assert m.sortino is not None
        assert m.sharpe is not None
        assert m.sortino > m.sharpe, "downside deviation is smaller than total deviation here"


class TestSummaryIsHonest:
    def test_small_samples_carry_the_caveat(self) -> None:
        m = compute(
            _curve(["1000", "1010", "1005", "1020"]), trade_count=3, total_cost=Decimal("1")
        )
        text = m.summary()
        assert "3 trades" in text
        assert "cannot distinguish skill from luck" in text

    def test_uncomputable_ratios_render_as_not_available(self) -> None:
        m = compute(_curve(["100", "100"]), trade_count=1, total_cost=Decimal("0"))
        assert "n/a" in m.summary()

    def test_large_samples_drop_the_caveat(self) -> None:
        equities = [str(1000 + (i * 7) % 23) for i in range(60)]
        m = compute(_curve(equities), trade_count=40, total_cost=Decimal("10"))
        assert "cannot distinguish" not in m.summary()
