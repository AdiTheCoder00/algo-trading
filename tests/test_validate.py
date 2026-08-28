"""The data quality gate (D-115).

`validate_bars` is 158 lines whose whole job is catching "the failure modes that
produce wrong backtests" - and until now nothing referenced it in the test suite
at all. A gate nobody tests is a gate that can be silently broken by an edit
somewhere else, which is precisely what it exists to prevent elsewhere.

Each test names the wrong backtest the finding would otherwise produce.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from algo.core.bar import Bar, Timeframe
from algo.data.validate import QualityReport, Severity, validate_bars
from algo.exchange.calendar import synthetic_calendar

TF = Timeframe(minutes=30)
CAL = synthetic_calendar()
DAY = date(2026, 8, 19)


def _bar(offset: int, *, volume: int = 10, high: str = "101", low: str = "99") -> Bar:
    return Bar(
        ts=CAL.session_open(DAY) + timedelta(minutes=30 * (offset + 1)),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("100"),
        volume=volume,
        timeframe=TF,
    )


def _report(bars: list[Bar], **kwargs: object) -> QualityReport:
    return validate_bars(bars, calendar=CAL, timeframe=TF, **kwargs)  # type: ignore[arg-type]


def _codes(bars: list[Bar], **kwargs: object) -> list[str]:
    return [f.code for f in _report(bars, **kwargs).findings]


class TestTheErrorsThatInvalidateARun:
    def test_a_repeated_timestamp_is_an_error(self) -> None:
        """Two bars at one instant double-count a bar's move."""
        codes = _codes([_bar(0), _bar(0)], expect_full_sessions=False)

        assert "DUPLICATE_TS" in codes

    def test_bars_going_backwards_is_an_error(self) -> None:
        """Out-of-order bars let a strategy see a price it could not have."""
        codes = _codes([_bar(3), _bar(1)], expect_full_sessions=False)

        assert "NON_MONOTONIC" in codes

    def test_errors_make_the_report_unclean(self) -> None:
        report = _report([_bar(0), _bar(0)], expect_full_sessions=False)

        assert not report.is_clean
        assert all(f.severity is Severity.ERROR for f in report.errors)


class TestTheWarningsThatChangeWhatIsTradeable:
    def test_a_zero_volume_bar_is_flagged(self) -> None:
        """Nothing may be filled against it - the distinction the whole strangle
        strategy turns on."""
        codes = _codes([_bar(0, volume=0)], expect_full_sessions=False)

        assert "ZERO_VOLUME" in codes

    def test_a_flat_bar_with_volume_is_flagged_as_a_possible_stale_print(self) -> None:
        codes = _codes([_bar(0, high="100", low="100")], expect_full_sessions=False)

        assert "FLAT_BAR" in codes

    def test_a_flat_bar_with_no_volume_is_only_zero_volume(self) -> None:
        """One bar must not raise two findings for the same fact."""
        codes = _codes([_bar(0, volume=0, high="100", low="100")], expect_full_sessions=False)

        assert codes.count("ZERO_VOLUME") == 1
        assert "FLAT_BAR" not in codes

    def test_warnings_alone_leave_the_report_clean(self) -> None:
        """`is_clean` gates a run; a thin bar is worth knowing about but is not a
        reason to refuse the whole dataset."""
        report = _report([_bar(0, volume=0)], expect_full_sessions=False)

        assert report.is_clean
        assert report.findings


class TestSessionCoverage:
    def test_a_full_session_is_clean(self) -> None:
        """The real shape: a resampled session must validate without findings,
        or every honest run would look broken."""
        from algo.data.resample import resample
        from algo.data.synthetic import one_minute_session

        bars = resample(
            one_minute_session(CAL, DAY, seed=20260819), calendar=CAL, timeframe=TF
        )

        report = validate_bars(bars, calendar=CAL, timeframe=TF)

        assert report.is_clean, report.summary()

    def test_a_short_session_is_noticed(self) -> None:
        """Missing bars are how a backtest silently skips a chunk of the day."""
        report = _report([_bar(0), _bar(1)])

        assert not report.is_clean or report.findings

    def test_coverage_can_be_switched_off_for_a_fragment(self) -> None:
        assert _report([_bar(0)], expect_full_sessions=False).findings == ()


class TestTheReportItself:
    def test_it_counts_what_it_checked(self) -> None:
        assert _report([_bar(0), _bar(1)], expect_full_sessions=False).bars_checked == 2

    def test_an_empty_series_is_not_an_exception(self) -> None:
        report = _report([], expect_full_sessions=False)

        assert report.bars_checked == 0
        assert report.is_clean

    def test_the_summary_is_readable(self) -> None:
        assert isinstance(_report([_bar(0)], expect_full_sessions=False).summary(), str)
