"""The measured spread profile - the number that decides whether the edge exists.

The module docstring names the stake: on the M5 study spread was $56,608 against
$12,271 of gross profit, so the spread is not a detail of the result, it largely
is the result. Everything here guards a way that number could come out wrong
while still looking like a measurement.

Three things get the most attention.

**Bad ticks must not become free trades.** A zero or inverted quote (ask <= bid)
is a feed artefact. Averaged in, it drags the measured spread *down*, which makes
a losing strategy look profitable - the one direction an error must never go.

**A missing hour must fall back to something measured.** An hour with no sample
is charged the median across the hours that do have one, never zero and never a
constant from outside the measurement.

**A corrupt cache must be a missing cache.** `load_profile` returning a
half-parsed profile would mis-price every fill in the run silently, which is
worse than re-measuring.

`_hour_of_week` indexes Monday 00:00 UTC as 0, so the fixtures below anchor on
2026-08-24, a Monday.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from algo.core.errors import DataError
from algo.data.mt5_spread import (
    HOURS_IN_WEEK,
    SAMPLE_MINUTES,
    SpreadProfile,
    label_hour,
    load_profile,
    measure_spread_profile,
    save_profile,
)

#: A Monday, so hour-of-week 0 is the first sample.
MONDAY = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
SERVER_OFFSET = timedelta(hours=3)
MEASURED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"

_TICK_DTYPE = np.dtype([("bid", "f8"), ("ask", "f8")])


def _ticks(*pairs: tuple[float, float]) -> NDArray[np.void]:
    return np.array(list(pairs), dtype=_TICK_DTYPE)


class FakeTerminal:
    """Returns a scripted batch of ticks per `copy_ticks_range` call.

    `calls` records every request so the server-time conversion and the sampling
    window can be asserted on the call rather than inferred from the result.
    """

    COPY_TICKS_INFO = 1

    def __init__(self, batches: list[NDArray[np.void] | None]) -> None:
        self.batches = batches
        self.calls: list[tuple[str, datetime, datetime, int]] = []

    def copy_ticks_range(
        self, symbol: str, start: datetime, end: datetime, flags: int
    ) -> NDArray[np.void] | None:
        self.calls.append((symbol, start, end, flags))
        return self.batches[(len(self.calls) - 1) % len(self.batches)]


def _measure(terminal: FakeTerminal, *, samples: int = 1, hours: int = 1) -> SpreadProfile:
    return measure_spread_profile(
        terminal,
        symbol=SYMBOL,
        offset=SERVER_OFFSET,
        start=MONDAY,
        end=MONDAY + timedelta(hours=hours),
        samples=samples,
        now=MEASURED_AT,
    )


def _profile(**overrides: Any) -> SpreadProfile:
    fields: dict[str, Any] = {
        "symbol": SYMBOL,
        "median_by_hour": {0: Decimal("0.30"), 13: Decimal("0.20"), 167: Decimal("0.90")},
        "p90_by_hour": {0: Decimal("0.50"), 13: Decimal("0.30"), 167: Decimal("1.60")},
        "samples": 240,
        "ticks": 91_234,
        "measured_at": MEASURED_AT,
        "fallback": Decimal("0.28"),
    }
    fields.update(overrides)
    return SpreadProfile(**fields)


# ------------------------------------------------------------ hour of week
class TestHourOfWeek:
    def test_monday_midnight_utc_is_hour_zero(self) -> None:
        assert _profile(median_by_hour={0: Decimal("1")}).full_spread_at(MONDAY) == (
            Decimal("1")
        )

    def test_the_week_ends_at_sunday_twenty_three_hundred(self) -> None:
        sunday_late = MONDAY + timedelta(days=6, hours=23)
        assert _profile(median_by_hour={167: Decimal("9")}).full_spread_at(
            sunday_late
        ) == Decimal("9")
        assert HOURS_IN_WEEK == 168

    def test_a_non_utc_timestamp_is_converted_before_bucketing(self) -> None:
        """A fill stamped in another zone must be charged its real UTC hour, not
        the hour its own offset happens to read."""
        from zoneinfo import ZoneInfo

        # 08:30 IST on the Monday is 03:00 UTC, hour-of-week 3.
        ist = datetime(2026, 8, 24, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        profile = _profile(median_by_hour={3: Decimal("0.44")})

        assert profile.full_spread_at(ist) == Decimal("0.44")

    @pytest.mark.parametrize(
        ("hour", "expected"),
        [(0, "Mon 00:00 UTC"), (13, "Mon 13:00 UTC"), (144, "Sun 00:00 UTC"),
         (167, "Sun 23:00 UTC")],
    )
    def test_hours_label_readably(self, hour: int, expected: str) -> None:
        assert label_hour(hour) == expected


# --------------------------------------------------------------- lookups
class TestChargingAFill:
    def test_a_covered_hour_is_charged_what_that_hour_costs(self) -> None:
        """The point of the profile: 13:00 Monday is charged 13:00 Monday's
        spread, not one afternoon's quote."""
        assert _profile().full_spread_at(MONDAY + timedelta(hours=13)) == Decimal("0.20")

    def test_an_uncovered_hour_falls_back_to_the_measured_median(self) -> None:
        """Never zero, and never a constant from outside the measurement."""
        uncovered = MONDAY + timedelta(hours=5)

        assert _profile().full_spread_at(uncovered) == Decimal("0.28")

    def test_half_the_spread_is_what_one_side_crosses(self) -> None:
        assert _profile().half_spread_at(MONDAY) == Decimal("0.15")
        assert _profile().half_spread_at(MONDAY + timedelta(hours=5)) == Decimal("0.14")

    def test_the_median_property_is_the_fallback(self) -> None:
        assert _profile().median == Decimal("0.28")

    def test_the_widest_and_tightest_hours_are_reported(self) -> None:
        """What the panel shows a human: the rollover and the overlap."""
        assert _profile().widest_hour == (167, Decimal("0.90"))
        assert _profile().tightest_hour == (13, Decimal("0.20"))

    def test_an_empty_profile_has_no_widest_or_tightest_hour(self) -> None:
        """None rather than a crash or an invented hour - an empty profile is a
        real state when nothing could be measured."""
        empty = _profile(median_by_hour={}, p90_by_hour={})

        assert empty.widest_hour is None
        assert empty.tightest_hour is None

    def test_describe_says_how_much_evidence_stands_behind_it(self) -> None:
        """The panel repeats this, so the reader can tell a profile from a guess."""
        described = _profile().describe()

        assert "91,234 ticks" in described
        assert "240 samples" in described
        assert "3 covered hours" in described


# ----------------------------------------------------------- measurement
class TestMeasuring:
    def test_a_spread_is_the_ask_minus_the_bid(self) -> None:
        terminal = FakeTerminal([_ticks((2000.00, 2000.30), (2000.10, 2000.40))])
        profile = _measure(terminal)

        assert profile.median_by_hour[0] == Decimal("0.30")
        assert profile.ticks == 2
        assert profile.samples == 1

    def test_prices_go_through_str_so_the_spread_is_exact(self) -> None:
        """`Decimal(2000.3) - Decimal(2000.0)` is not `Decimal("0.3")`, and a
        spread wrong in the twelfth decimal compounds over 144 fills."""
        profile = _measure(FakeTerminal([_ticks((2000.0, 2000.3))]))

        assert profile.median_by_hour[0] == Decimal("0.3")

    def test_an_inverted_quote_is_discarded_not_counted_as_free(self) -> None:
        """ask <= bid is a feed artefact. Kept, it would drag the measured spread
        down and make a losing strategy look profitable - the one direction this
        must never fail in."""
        terminal = FakeTerminal([_ticks((2000.50, 2000.20), (2000.00, 2000.40))])
        profile = _measure(terminal)

        assert profile.ticks == 1
        assert profile.median_by_hour[0] == Decimal("0.40")

    def test_a_zero_width_quote_is_discarded_too(self) -> None:
        terminal = FakeTerminal([_ticks((2000.00, 2000.00), (2000.00, 2000.25))])

        assert _measure(terminal).ticks == 1

    def test_ticks_are_requested_in_server_time(self) -> None:
        """Ticks are stamped in server time exactly as bars are; asking in UTC
        would sample a window three hours from the one intended."""
        terminal = FakeTerminal([_ticks((2000.0, 2000.3))])
        _measure(terminal)

        symbol, start, end, flags = terminal.calls[0]
        assert symbol == SYMBOL
        assert start == MONDAY + SERVER_OFFSET
        assert end == MONDAY + SERVER_OFFSET + timedelta(minutes=SAMPLE_MINUTES)
        assert flags == FakeTerminal.COPY_TICKS_INFO

    def test_samples_are_spread_evenly_across_the_window(self) -> None:
        """Evenly, so a long history hits every hour of the week many times
        rather than the profile being dominated by one stretch of days."""
        terminal = FakeTerminal([_ticks((2000.0, 2000.3))])
        _measure(terminal, samples=4, hours=4)

        starts = [call[1] for call in terminal.calls]
        assert starts == [
            MONDAY + SERVER_OFFSET + timedelta(hours=h) for h in range(4)
        ]

    def test_each_sample_lands_in_its_own_hour_of_the_week(self) -> None:
        wide = _ticks((2000.0, 2000.9))
        tight = _ticks((2000.0, 2000.1))
        profile = _measure(FakeTerminal([wide, tight]), samples=2, hours=2)

        assert profile.median_by_hour[0] == Decimal("0.9")
        assert profile.median_by_hour[1] == Decimal("0.1")

    def test_an_empty_sample_is_skipped_without_counting(self) -> None:
        """A window the terminal has no history for is not a sample of zero
        spread - it is not a sample."""
        profile = _measure(
            FakeTerminal([None, _ticks((2000.0, 2000.2))]), samples=2, hours=2
        )

        assert profile.samples == 1
        assert profile.ticks == 1
        assert 0 not in profile.median_by_hour

    def test_the_fallback_is_the_median_across_every_measured_tick(self) -> None:
        profile = _measure(
            FakeTerminal([_ticks((2000.0, 2000.1)), _ticks((2000.0, 2000.9))]),
            samples=2,
            hours=2,
        )

        assert profile.fallback in (Decimal("0.1"), Decimal("0.9"))
        assert profile.full_spread_at(MONDAY + timedelta(hours=50)) == profile.fallback

    def test_the_ninetieth_percentile_keeps_the_tail_visible(self) -> None:
        """The median is what a fill usually pays; the p90 is what it pays when
        the news prints, and averaging that away hides the risk."""
        spikes = _ticks(*[(2000.0, 2000.1)] * 9, (2000.0, 2005.0))
        profile = _measure(FakeTerminal([spikes]))

        assert profile.median_by_hour[0] == Decimal("0.1")
        assert profile.p90_by_hour[0] == Decimal("5.0")

    def test_measured_at_comes_from_the_injected_clock(self) -> None:
        """D-015: the wall clock is read only through the clock module, and `now`
        is the injection point that makes this assertable at all."""
        assert _measure(FakeTerminal([_ticks((2000.0, 2000.3))])).measured_at == (
            MEASURED_AT
        )


class TestMeasurementRefusals:
    def test_an_end_before_the_start_is_refused(self) -> None:
        with pytest.raises(DataError, match="is not after start"):
            measure_spread_profile(
                FakeTerminal([None]),
                symbol=SYMBOL,
                offset=SERVER_OFFSET,
                start=MONDAY,
                end=MONDAY - timedelta(hours=1),
                now=MEASURED_AT,
            )

    def test_an_equal_start_and_end_is_refused(self) -> None:
        with pytest.raises(DataError, match="is not after start"):
            measure_spread_profile(
                FakeTerminal([None]),
                symbol=SYMBOL,
                offset=SERVER_OFFSET,
                start=MONDAY,
                end=MONDAY,
                now=MEASURED_AT,
            )

    @pytest.mark.parametrize("samples", [0, -5])
    def test_a_non_positive_sample_count_is_refused(self, samples: int) -> None:
        with pytest.raises(DataError, match="samples must be at least 1"):
            _measure(FakeTerminal([None]), samples=samples)

    def test_no_usable_ticks_says_what_to_do_about_it(self) -> None:
        """Refused rather than returning an empty profile that would charge
        nothing - a zero spread is the most flattering possible wrong answer."""
        with pytest.raises(DataError, match="scroll the symbol's chart"):
            _measure(FakeTerminal([None]), samples=3, hours=3)

    def test_ticks_that_are_all_inverted_count_as_no_usable_ticks(self) -> None:
        with pytest.raises(DataError, match="no usable ticks"):
            _measure(FakeTerminal([_ticks((2000.5, 2000.1))]))


# ----------------------------------------------------------- persistence
class TestPersistence:
    def test_a_profile_round_trips_through_disk_exactly(self, tmp_path: Path) -> None:
        """Decimals must survive as Decimals. A profile that reloads as float is
        a profile that mis-prices by a fraction of a tick on every fill."""
        path = tmp_path / "spread.json"
        original = _profile()
        save_profile(original, path)
        loaded = load_profile(SYMBOL, path)

        assert loaded is not None
        assert loaded.median_by_hour == original.median_by_hour
        assert loaded.p90_by_hour == original.p90_by_hour
        assert loaded.fallback == original.fallback
        assert loaded.measured_at == original.measured_at
        assert loaded.samples == original.samples
        assert loaded.ticks == original.ticks
        assert isinstance(loaded.fallback, Decimal)

    def test_saving_creates_the_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "spread.json"
        save_profile(_profile(), path)

        assert path.exists()

    def test_a_missing_file_is_no_profile(self, tmp_path: Path) -> None:
        assert load_profile(SYMBOL, tmp_path / "absent.json") is None

    def test_a_corrupt_file_is_no_profile_never_a_wrong_one(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "spread.json"
        path.write_text("{ not json at all", encoding="utf-8")

        assert load_profile(SYMBOL, path) is None

    def test_a_profile_for_another_symbol_is_not_borrowed(self, tmp_path: Path) -> None:
        """EURUSD's spread says nothing about XAUUSD's, and charging one for the
        other would be wrong by an order of magnitude."""
        path = tmp_path / "spread.json"
        save_profile(_profile(symbol="EURUSD"), path)

        assert load_profile(SYMBOL, path) is None
        assert load_profile("EURUSD", path) is not None

    def test_a_file_missing_a_field_is_no_profile(self, tmp_path: Path) -> None:
        path = tmp_path / "spread.json"
        payload = json.loads(json.dumps({"symbol": SYMBOL, "median_by_hour": {}}))
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert load_profile(SYMBOL, path) is None

    def test_a_file_with_an_unparseable_decimal_is_no_profile(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "spread.json"
        save_profile(_profile(), path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["fallback"] = "not-a-number"
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert load_profile(SYMBOL, path) is None

    def test_a_measured_profile_survives_the_round_trip(self, tmp_path: Path) -> None:
        """End to end: measure, persist, reload, and charge the same fill the
        same amount - which is the whole reason the cache exists."""
        path = tmp_path / "spread.json"
        measured = _measure(FakeTerminal([_ticks((2000.0, 2000.35))]))
        save_profile(measured, path)
        reloaded = load_profile(SYMBOL, path)

        assert reloaded is not None
        assert reloaded.full_spread_at(MONDAY) == measured.full_spread_at(MONDAY)
        assert reloaded.half_spread_at(MONDAY) == Decimal("0.175")


class TestTheQuantileItself:
    """`_quantile` is private and `measure_spread_profile` filters empty buckets
    before it is reached, so these call it directly. Its definition decides what
    "median spread" and "p90 spread" mean, which is worth stating rather than
    leaving implied by whichever fixture happened to be used above."""

    def test_it_picks_an_order_statistic_rather_than_interpolating(self) -> None:
        """No averaging of neighbours: the value returned is one that was actually
        observed, so a reported spread is always a spread that really occurred."""
        from algo.data.mt5_spread import _quantile

        values = [Decimal(str(v)) for v in (0.1, 0.2, 0.3, 0.4, 0.5)]

        assert _quantile(values, 0.5) == Decimal("0.3")
        assert _quantile(values, 0.9) == Decimal("0.5")
        assert _quantile(values, 0.0) == Decimal("0.1")

    def test_it_sorts_before_indexing(self) -> None:
        from algo.data.mt5_spread import _quantile

        assert _quantile([Decimal("9"), Decimal("1"), Decimal("5")], 0.5) == Decimal("5")

    def test_the_top_quantile_cannot_run_off_the_end(self) -> None:
        """`int(1.0 * n)` is n, one past the last index; the clamp is what stops
        p100 raising IndexError."""
        from algo.data.mt5_spread import _quantile

        assert _quantile([Decimal("1"), Decimal("2")], 1.0) == Decimal("2")

    def test_no_values_is_refused_rather_than_returning_zero(self) -> None:
        """Unreachable through `measure_spread_profile`, which filters empty
        buckets first. Kept as a guard because a quantile of nothing returning
        zero would be a free trade."""
        from algo.data.mt5_spread import _quantile

        with pytest.raises(DataError, match="no values to take a quantile of"):
            _quantile([], 0.5)
