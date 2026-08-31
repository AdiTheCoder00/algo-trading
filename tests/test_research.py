"""The research service's own guards: parameters are range-checked, not clamped.

`run_study` needs a live MT5 terminal, so what is asserted here is everything
that happens *before* that - the catalogue, and the validation that turns a
browser's free-text input into something the engine will accept or refuse with
a message a person can act on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from algo.backtest.research import (
    MAX_BARS,
    catalogue,
    run_study,
    run_sweep_study,
    run_walk_forward_study,
)
from algo.backtest.sweep import Cell, Robustness, assess
from algo.core.errors import DataError
from algo.core.instrument import CfdId


class TestCatalogue:
    def test_every_parameter_carries_its_own_range(self) -> None:
        for spec in catalogue()["parameters"]:
            assert spec["minimum"] != ""
            assert spec["maximum"] != ""
            assert spec["help"], f"{spec['name']} has no help text"

    def test_the_bar_cap_is_advertised(self) -> None:
        """The console must be able to render the limit, not discover it by
        being refused."""
        body = catalogue()
        bars = next(p for p in body["parameters"] if p["name"] == "bars")

        assert body["max_bars"] == MAX_BARS
        assert bars["maximum"] == str(MAX_BARS)


class TestParametersAreRefusedNotClamped:
    """Clamping would run a study the caller did not ask for and report it as
    though they had - the same reason the config loader refuses an unquoted
    float rather than rounding it."""

    def test_an_out_of_range_stop_is_refused(self) -> None:
        with pytest.raises(DataError, match="Stop loss"):
            run_study(
                object(),
                strategy="breakout",
                timeframe_minutes=30,
                params={"stop_loss_pct": "500"},
            )

    def test_a_non_numeric_parameter_is_refused(self) -> None:
        with pytest.raises(DataError, match="not a number"):
            run_study(
                object(),
                strategy="breakout",
                timeframe_minutes=30,
                params={"stop_loss_pct": "tight"},
            )

    def test_more_bars_than_the_cap_is_refused(self) -> None:
        with pytest.raises(DataError, match="Bars of history"):
            run_study(
                object(),
                strategy="breakout",
                timeframe_minutes=30,
                params={"bars": str(MAX_BARS + 1)},
            )

    def test_an_unknown_timeframe_is_refused(self) -> None:
        with pytest.raises(DataError, match="timeframe"):
            run_study(object(), strategy="breakout", timeframe_minutes=7)

    def test_an_unknown_strategy_is_refused(self) -> None:
        """Refused before any terminal is touched - `strategy_for` is the same
        lookup the live loop uses, so the console cannot name something the
        engine could not run."""
        with pytest.raises(DataError, match="unknown strategy"):
            run_study(object(), strategy="martingale", timeframe_minutes=30)


class TestWalkForwardIsRefusedBeforeTouchingTheTerminal:
    """Same ordering rule as `run_study`: everything cheap is checked before
    MT5 is attached, so a bad request costs no seconds and reports the real
    problem rather than whichever one surfaced first."""

    def test_an_unknown_axis_is_refused(self) -> None:
        with pytest.raises(DataError, match="not an optimisable axis"):
            run_walk_forward_study(
                object(), strategy="breakout", timeframe_minutes=30, axis="sharpe"
            )

    def test_optimising_channel_length_on_macd_is_refused(self) -> None:
        """MACD has no channel length. Optimising over a knob the strategy
        ignores would search a grid of identical runs and then report a
        'stability' verdict on noise that never varied."""
        with pytest.raises(DataError, match="breakout parameter"):
            run_walk_forward_study(
                object(), strategy="macd", timeframe_minutes=30, axis="lookback"
            )

    def test_two_identical_axes_are_refused(self) -> None:
        with pytest.raises(DataError, match="must differ"):
            run_walk_forward_study(
                object(),
                strategy="breakout",
                timeframe_minutes=30,
                axis="stop_loss_pct",
                second_axis="stop_loss_pct",
            )

    def test_a_too_short_window_is_refused(self) -> None:
        with pytest.raises(DataError, match="at least 7 days"):
            run_walk_forward_study(
                object(),
                strategy="breakout",
                timeframe_minutes=30,
                in_sample_days=3,
            )

    def test_no_axis_at_all_is_refused(self) -> None:
        with pytest.raises(DataError, match="at least one parameter"):
            run_walk_forward_study(
                object(), strategy="breakout", timeframe_minutes=30, axis=""
            )


class TestTheGridIsBoundedNotTruncated:
    def test_too_wide_a_grid_is_refused(self) -> None:
        """Silently searching a smaller grid than asked for would report a
        study the caller did not run."""
        from algo.backtest.cfd_walkforward import MAX_GRID, build_grid

        wide = {f"axis{i}": ("a", "b", "c") for i in range(4)}  # 81 combinations

        with pytest.raises(DataError, match=f"past the {MAX_GRID} cap"):
            build_grid(wide, {})

    def test_a_grid_within_the_cap_is_the_full_product(self) -> None:
        from algo.backtest.cfd_walkforward import build_grid

        grid = build_grid({"lookback": ("10", "20")}, {"stop_loss_pct": "0"})

        assert len(grid) == 2
        assert all(params["stop_loss_pct"] == "0" for params in grid)
        assert {params["lookback"] for params in grid} == {"10", "20"}


class TestSweepIsRefusedBeforeTouchingTheTerminal:
    def test_an_unknown_axis_is_refused(self) -> None:
        with pytest.raises(DataError, match="not a sweep axis"):
            run_sweep_study(object(), strategy="breakout", row_axis="sharpe")

    def test_two_identical_axes_are_refused(self) -> None:
        with pytest.raises(DataError, match="must differ"):
            run_sweep_study(
                object(),
                strategy="breakout",
                row_axis="stop_loss_pct",
                column_axis="stop_loss_pct",
            )

    def test_sweeping_channel_length_on_macd_is_refused(self) -> None:
        with pytest.raises(DataError, match="breakout parameter"):
            run_sweep_study(
                object(), strategy="macd", row_axis="lookback", column_axis="timeframe"
            )


class TestTheSweepArguesWithItsOwnBestCell:
    """A heatmap invites reading off the greenest square, and D-131 is the
    standing evidence that doing so fits noise. These pin the verdicts that
    push back on it."""

    def _grid(
        self, values: dict[tuple[str, str], str]
    ) -> tuple[Robustness, dict[tuple[str, str], Cell]]:
        cells = {
            key: Cell(row=key[0], column=key[1], net_pnl=Decimal(v), trades=20)
            for key, v in values.items()
        }
        rows = sorted({k[0] for k in values})
        cols = sorted({k[1] for k in values})
        return assess(cells, rows, cols), cells

    def test_a_lone_spike_is_called_an_isolated_peak(self) -> None:
        """One cell far above flat neighbours is the signature of a parameter
        fitted to whichever trades fell inside this window."""
        grid = {(r, c): "100" for r in "abc" for c in "xyz"}
        grid[("b", "y")] = "100000"

        robustness, _ = self._grid(grid)

        assert robustness.verdict == "ISOLATED PEAK"
        assert any("spike" in note for note in robustness.notes)

    def test_a_broad_plateau_is_not_flagged(self) -> None:
        grid = {(r, c): "9000" for r in "abc" for c in "xyz"}
        grid[("b", "y")] = "10000"

        robustness, _ = self._grid(grid)

        assert robustness.verdict == "PLATEAU"

    def test_an_all_losing_grid_says_nothing_works(self) -> None:
        """Anchoring the verdict at zero matters: a grid where every setting
        loses must not present its least-bad cell as a find."""
        grid = {(r, c): "-5000" for r in "abc" for c in "xyz"}
        grid[("a", "x")] = "-100"

        robustness, _ = self._grid(grid)

        assert robustness.verdict == "NOTHING WORKS"
        assert any("does not work" in note for note in robustness.notes)

    def test_a_mostly_losing_grid_reports_the_positive_fraction(self) -> None:
        grid = {(r, c): "-5000" for r in "abc" for c in "xyz"}
        grid[("b", "y")] = "8000"
        grid[("b", "x")] = "7000"

        robustness, _ = self._grid(grid)

        assert robustness.positive_cells == 2
        assert any("of 9 cells are profitable" in note for note in robustness.notes)

    def test_every_result_warns_that_a_sweep_has_no_out_of_sample_step(self) -> None:
        robustness, _ = self._grid({(r, c): "100" for r in "ab" for c in "xy"})

        assert any("out-of-sample" in note for note in robustness.notes)


class TestTheSweepGridIsBounded:
    def test_too_many_cells_is_refused_not_truncated(self) -> None:
        from algo.backtest.sweep import MAX_CELLS, run_sweep

        with pytest.raises(DataError, match=f"past the {MAX_CELLS} cap"):
            run_sweep(
                bars_for={},
                strategy="breakout",
                instrument=CfdId(symbol="XAUUSD"),
                row_axis="lookback",
                row_values=[str(i) for i in range(9)],
                column_axis="stop_loss_pct",
                column_values=[str(i) for i in range(9)],
                base={},
                lots=100,
                timeframe_minutes=60,
            )
