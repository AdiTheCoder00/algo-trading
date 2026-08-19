"""Walk-forward analysis. Brief §9 Milestone 5.

The run function is a stub throughout, and deliberately so. These tests are about
whether the *analysis* behaves — whether it flags an unstable parameter, whether it
notices that optimising beat nothing, whether it refuses to draw a conclusion from
four trades. Driving it with a real backtest would make every assertion a statement
about the fixture instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import ClassVar

import pytest

from algo.backtest.walkforward import (
    Confidence,
    ParameterSet,
    Stability,
    assess_feasibility,
    assess_stability,
    rolling_windows,
    run_walk_forward,
)
from algo.core.errors import DomainError
from algo.reporting.metrics import Metrics


def _metrics(*, net: str, trades: int) -> Metrics:
    return Metrics(
        trade_count=trades,
        bars=100,
        net_pnl=Decimal(net),
        gross_pnl=Decimal(net),
        total_cost=Decimal("0"),
        cost_drag_pct=None,
        starting_equity=Decimal("1000000"),
        final_equity=Decimal("1000000") + Decimal(net),
        return_pct=Decimal(net) / Decimal("10000"),
        max_drawdown=None,
        sharpe=None,
        sortino=None,
        calmar=None,
        exposure_pct=Decimal("50"),
        periods_per_year=Decimal("250"),
    )


class TestWindows:
    def test_windows_tile_the_validation_period(self) -> None:
        windows = rolling_windows(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            in_sample_days=90,
            out_of_sample_days=30,
        )
        assert len(windows) == 9
        for earlier, later in pairwise(windows):
            assert earlier.out_of_sample_end < later.out_of_sample_start

    def test_validation_follows_training_immediately(self) -> None:
        window = rolling_windows(
            start=date(2026, 1, 1),
            end=date(2026, 6, 30),
            in_sample_days=90,
            out_of_sample_days=30,
        )[0]
        assert window.in_sample_end == date(2026, 3, 31)
        assert window.out_of_sample_start == date(2026, 4, 1)
        assert window.out_of_sample_end == date(2026, 4, 30)

    def test_overlapping_validation_periods_are_refused(self) -> None:
        """The same trade counted twice is evidence counted twice."""
        with pytest.raises(DomainError, match="counting the same trades twice"):
            rolling_windows(
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                in_sample_days=90,
                out_of_sample_days=30,
                step_days=15,
            )

    def test_skipping_data_is_refused(self) -> None:
        with pytest.raises(DomainError, match="skip data entirely"):
            rolling_windows(
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                in_sample_days=90,
                out_of_sample_days=30,
                step_days=60,
            )

    def test_a_period_too_short_for_one_window_yields_none(self) -> None:
        assert (
            rolling_windows(
                start=date(2026, 1, 1),
                end=date(2026, 2, 1),
                in_sample_days=90,
                out_of_sample_days=30,
            )
            == []
        )


class TestParameterStability:
    """Brief §9: "Flag any parameter whose optimal value jumps around between
    windows — that is curve fitting, not signal.\""""

    def test_a_constant_parameter_is_stable(self) -> None:
        chosen = [{"delta": "0.25"}] * 5
        assert assess_stability(chosen)[0].verdict is Stability.STABLE

    def test_a_parameter_that_changes_every_window_is_unstable(self) -> None:
        chosen: list[ParameterSet] = [
            {"delta": "0.20"},
            {"delta": "0.30"},
            {"delta": "0.20"},
            {"delta": "0.35"},
            {"delta": "0.25"},
        ]
        stability = assess_stability(chosen)[0]
        assert stability.verdict is Stability.UNSTABLE
        assert stability.flip_rate == Decimal("1")

    def test_a_parameter_that_moved_once_and_settled_is_only_drifting(self) -> None:
        """A single regime change is not the same thing as fitting to noise, and
        collapsing the two would make the flag useless."""
        chosen: list[ParameterSet] = [
            {"delta": "0.25"},
            {"delta": "0.25"},
            {"delta": "0.30"},
            {"delta": "0.30"},
            {"delta": "0.30"},
        ]
        stability = assess_stability(chosen)[0]
        assert stability.verdict is Stability.DRIFTING
        assert stability.distinct == 2

    def test_the_sequence_is_shown_not_just_the_verdict(self) -> None:
        chosen: list[ParameterSet] = [{"delta": "0.20"}, {"delta": "0.30"}]
        described = assess_stability(chosen)[0].describe()
        assert "0.20 -> 0.30" in described

    def test_every_parameter_is_assessed_independently(self) -> None:
        chosen: list[ParameterSet] = [
            {"delta": "0.25", "dte": "5"},
            {"delta": "0.25", "dte": "20"},
            {"delta": "0.25", "dte": "5"},
        ]
        by_name = {s.name: s for s in assess_stability(chosen)}
        assert by_name["delta"].verdict is Stability.STABLE
        assert by_name["dte"].verdict is Stability.UNSTABLE

    def test_no_windows_is_no_verdict(self) -> None:
        assert assess_stability([]) == ()


class TestFeasibility:
    def test_a_handful_of_trades_supports_nothing(self) -> None:
        verdict = assess_feasibility(2, 4, [2, 2])
        assert verdict.confidence is Confidence.INSUFFICIENT
        assert not verdict.supports_a_conclusion
        assert "coin flip" in verdict.message

    def test_the_realistic_case_for_this_strategy_is_insufficient(self) -> None:
        """Two years of recorded data at roughly twelve trades a year, split into
        four windows. This is the honest expectation, not a pathological case."""
        verdict = assess_feasibility(4, 24, [6, 6, 6, 6])
        assert verdict.confidence is Confidence.THIN
        assert not verdict.supports_a_conclusion
        assert "30" in verdict.message

    def test_enough_trades_is_adequate(self) -> None:
        verdict = assess_feasibility(6, 60, [10] * 6)
        assert verdict.confidence is Confidence.ADEQUATE
        assert verdict.supports_a_conclusion

    def test_the_thinnest_window_is_reported(self) -> None:
        assert assess_feasibility(3, 40, [30, 8, 2]).min_trades_in_a_window == 2


class TestRunWalkForward:
    """A stub `run` lets each scenario be constructed exactly."""

    GRID: ClassVar[list[ParameterSet]] = [
        {"delta": "0.20"},
        {"delta": "0.25"},
        {"delta": "0.30"},
    ]

    def _windows(self) -> list:  # type: ignore[type-arg]
        return rolling_windows(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            in_sample_days=60,
            out_of_sample_days=30,
        )

    def test_it_optimises_in_sample_and_validates_out_of_sample(self) -> None:
        """0.25 always wins in sample; the report must apply it out of sample."""

        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del start, end
            return _metrics(net="100" if params["delta"] == "0.25" else "10", trades=8)

        report = run_walk_forward(
            windows=self._windows(),
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
        )
        assert all(r.chosen["delta"] == "0.25" for r in report.results)
        assert report.stability[0].verdict is Stability.STABLE

    def test_in_sample_and_out_of_sample_are_kept_apart(self) -> None:
        """The whole point. In sample flatters; out of sample does not."""

        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del params
            in_sample = (end - start).days < 35
            return _metrics(net="-50" if in_sample else "500", trades=8)

        report = run_walk_forward(
            windows=self._windows(),
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
        )
        assert report.in_sample_net_pnl > 0
        assert report.out_of_sample_net_pnl < 0
        assert not hasattr(report, "overall")
        assert not hasattr(report, "combined_net_pnl")

    def test_it_notices_when_optimising_did_not_help(self) -> None:
        """The most informative line in the report, and the easiest to omit."""
        windows = self._windows()
        in_sample_ranges = {(w.in_sample_start, w.in_sample_end) for w in windows}

        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            if (start, end) in in_sample_ranges:
                # In sample, 0.30 looks best. It is noise.
                return _metrics(net="900" if params["delta"] == "0.30" else "100", trades=9)
            # Out of sample the advantage vanishes and reverses.
            return _metrics(net="-200" if params["delta"] == "0.30" else "50", trades=9)

        report = run_walk_forward(
            windows=windows,
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
            baseline={"delta": "0.25"},
        )
        assert report.optimisation_beat_doing_nothing is False
        assert "That is curve fitting." in report.summary()

    def test_no_baseline_means_no_verdict_rather_than_a_guess(self) -> None:
        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del params, start, end
            return _metrics(net="100", trades=9)

        report = run_walk_forward(
            windows=self._windows(),
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
        )
        assert report.baseline_net_pnl is None
        assert report.optimisation_beat_doing_nothing is None

    def test_an_objective_can_exclude_candidates_and_says_so(self) -> None:
        """A parameter set that produced three trades has not earned a score."""

        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del start, end
            trades = 2 if params["delta"] == "0.20" else 12
            return _metrics(net="100", trades=trades)

        report = run_walk_forward(
            windows=self._windows(),
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl if m.trade_count >= 5 else None,
        )
        first = report.results[0]
        excluded = [c for c in first.candidates if c.score is None]
        assert excluded
        assert all(c.excluded_because for c in excluded)
        assert first.chosen["delta"] != "0.20"

    def test_everything_excluded_raises_rather_than_lowering_the_bar(self) -> None:
        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del params, start, end
            return _metrics(net="100", trades=1)

        with pytest.raises(DomainError, match="rather than lowering the bar"):
            run_walk_forward(
                windows=self._windows(),
                grid=self.GRID,
                run=run,
                objective=lambda m: m.net_pnl if m.trade_count >= 5 else None,
            )

    def test_an_unstable_parameter_is_flagged_in_the_summary(self) -> None:
        """A different parameter wins every window — the definition of noise."""
        windows = self._windows()
        rotation = ["0.20", "0.30", "0.25"]
        winners = {
            (w.in_sample_start, w.in_sample_end): rotation[i % len(rotation)]
            for i, w in enumerate(windows)
        }

        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            winner = winners.get((start, end))
            if winner is None:
                return _metrics(net="10", trades=8)
            return _metrics(net="100" if params["delta"] == winner else "1", trades=8)

        report = run_walk_forward(
            windows=windows,
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
        )
        assert [r.chosen["delta"] for r in report.results][:3] == ["0.20", "0.30", "0.25"]
        assert report.unstable_parameters
        assert "fitting to noise" in report.summary()

    def test_the_summary_leads_with_the_feasibility_verdict(self) -> None:
        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del params, start, end
            return _metrics(net="100", trades=2)

        report = run_walk_forward(
            windows=self._windows(),
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
        )
        summary = report.summary()
        assert "THIN" in summary or "INSUFFICIENT" in summary
        assert summary.index("net P&L") > summary.index(report.feasibility.confidence.value)

    def test_out_of_sample_is_labelled_more_loudly_than_in_sample(self) -> None:
        """Brief §10: out-of-sample metrics reported "separately and prominently"."""

        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del params, start, end
            return _metrics(net="100", trades=8)

        summary = run_walk_forward(
            windows=self._windows(),
            grid=self.GRID,
            run=run,
            objective=lambda m: m.net_pnl,
        ).summary()
        assert "OUT OF SAMPLE" in summary
        assert "in-sample" in summary

    def test_ties_break_deterministically(self) -> None:
        def run(params: ParameterSet, start: date, end: date) -> Metrics:
            del params, start, end
            return _metrics(net="100", trades=8)

        first = run_walk_forward(
            windows=self._windows(), grid=self.GRID, run=run, objective=lambda m: m.net_pnl
        )
        second = run_walk_forward(
            windows=self._windows(), grid=self.GRID, run=run, objective=lambda m: m.net_pnl
        )
        assert [r.chosen for r in first.results] == [r.chosen for r in second.results]

    def test_an_empty_grid_is_refused(self) -> None:
        with pytest.raises(DomainError, match="at least one parameter set"):
            run_walk_forward(
                windows=self._windows(),
                grid=[],
                run=lambda p, s, e: _metrics(net="0", trades=0),
                objective=lambda m: m.net_pnl,
            )
