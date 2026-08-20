"""CSV and parquet feeds.

The property that matters is a lossless round trip. If a price changes by a
fraction of a paisa on the way through storage, brief §2.5 is already broken
before the engine starts, and the symptom will surface several layers away as a
tick-grid rejection with no visible cause.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from algo.core.bar import M1, M30, Bar
from algo.core.errors import DataError
from algo.core.instrument import FutureId
from algo.data.csv_feed import CsvBarFeed, read_csv_bars, write_csv_bars
from algo.data.feed import BarFeed, InMemoryBarFeed
from algo.data.parquet_feed import ParquetBarFeed, read_parquet_bars, write_parquet_bars
from algo.data.resample import resample
from algo.data.synthetic import one_minute_session
from algo.exchange.calendar import MarketCalendar
from tests.conftest import SUMMER_DAY

AWKWARD_PRICES = ("156640.05", "0.05", "99999.95", "1.15", "2.675")


def _sample(calendar: MarketCalendar) -> list[Bar]:
    return resample(
        one_minute_session(calendar, SUMMER_DAY, seed=21), calendar=calendar, timeframe=M30
    )


class TestCsvRoundTrip:
    def test_bars_survive_unchanged(self, calendar: MarketCalendar, tmp_path: Path) -> None:
        original = _sample(calendar)
        path = tmp_path / "bars.csv"
        write_csv_bars(original, path)
        assert read_csv_bars(path, M30) == original

    def test_awkward_decimals_survive(self, tmp_path: Path, calendar: MarketCalendar) -> None:
        """Prices that a float would mangle."""
        opened = calendar.session_open(SUMMER_DAY)
        original = [
            Bar(
                # datetime.timedelta, not pandas': pandas 2.3 deprecates the
                # kwargs form here and warnings are errors in this suite.
                ts=opened + timedelta(minutes=i + 1),
                timeframe=M1,
                open=Decimal(p),
                high=Decimal(p),
                low=Decimal(p),
                close=Decimal(p),
                volume=1,
            )
            for i, p in enumerate(AWKWARD_PRICES)
        ]
        path = tmp_path / "awkward.csv"
        write_csv_bars(original, path)
        for restored, expected in zip(read_csv_bars(path, M1), original, strict=True):
            assert restored.close == expected.close
            assert str(restored.close) == str(expected.close)

    def test_partial_flag_survives(self, calendar: MarketCalendar, tmp_path: Path) -> None:
        from tests.conftest import WINTER_DAY

        original = resample(
            one_minute_session(calendar, WINTER_DAY, seed=22), calendar=calendar, timeframe=M30
        )
        assert original[-1].is_partial
        path = tmp_path / "winter.csv"
        write_csv_bars(original, path)
        assert read_csv_bars(path, M30)[-1].is_partial

    def test_missing_file_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="not found"):
            read_csv_bars(tmp_path / "nope.csv", M30)

    def test_missing_columns_are_named(self, tmp_path: Path) -> None:
        path = tmp_path / "short.csv"
        path.write_text("ts,open,high\n2026-08-19T04:00:00+00:00,1,2\n", encoding="utf-8")
        with pytest.raises(DataError, match="missing required columns: low, close"):
            read_csv_bars(path, M30)

    def test_a_bad_row_reports_its_line_number(self, tmp_path: Path) -> None:
        """Brief §12: the exception is re-raised with the one fact it lacked."""
        path = tmp_path / "bad.csv"
        path.write_text(
            "ts,open,high,low,close\n"
            "2026-08-19T04:00:00+00:00,1,2,0.5,1.5\n"
            "2026-08-19T04:30:00+00:00,1,2,0.5,NOT_A_PRICE\n",
            encoding="utf-8",
        )
        with pytest.raises(DataError, match=r"bad\.csv:3"):
            read_csv_bars(path, M30)


class TestParquetRoundTrip:
    def test_bars_survive_unchanged(self, calendar: MarketCalendar, tmp_path: Path) -> None:
        original = _sample(calendar)
        path = tmp_path / "bars.parquet"
        write_parquet_bars(original, path)
        assert read_parquet_bars(path, M30) == original

    def test_prices_are_stored_as_strings(self, calendar: MarketCalendar, tmp_path: Path) -> None:
        path = tmp_path / "bars.parquet"
        write_parquet_bars(_sample(calendar), path)
        frame = pd.read_parquet(path)
        for column in ("open", "high", "low", "close"):
            assert not pd.api.types.is_float_dtype(frame[column])

    def test_a_float_price_column_is_refused_with_a_remedy(self, tmp_path: Path) -> None:
        """Silently converting would undo §2.5 at the point of entry."""
        path = tmp_path / "floats.parquet"
        pd.DataFrame(
            {
                "ts": [pd.Timestamp("2026-08-19T04:00:00Z")],
                "open": [1.1],
                "high": [1.2],
                "low": [1.0],
                "close": [1.15],
                "volume": [1],
            }
        ).to_parquet(path, index=False)
        with pytest.raises(DataError, match="re-export with write_parquet_bars"):
            read_parquet_bars(path, M30)


class TestFeedProtocol:
    def test_csv_feed_satisfies_the_protocol(
        self, calendar: MarketCalendar, tmp_path: Path, goldm_future: FutureId
    ) -> None:
        path = tmp_path / "bars.csv"
        write_csv_bars(_sample(calendar), path)
        feed = CsvBarFeed(goldm_future, M30, path)
        assert isinstance(feed, BarFeed)
        assert len(list(feed)) == 29
        assert feed.timeframe == M30

    def test_parquet_feed_satisfies_the_protocol(
        self, calendar: MarketCalendar, tmp_path: Path, goldm_future: FutureId
    ) -> None:
        path = tmp_path / "bars.parquet"
        write_parquet_bars(_sample(calendar), path)
        feed = ParquetBarFeed(goldm_future, M30, path)
        assert isinstance(feed, BarFeed)
        assert len(list(feed)) == 29

    def test_in_memory_feed_copies_its_input(
        self, calendar: MarketCalendar, goldm_future: FutureId
    ) -> None:
        """A test that mutates the list afterwards must not change the feed."""
        bars = _sample(calendar)
        feed = InMemoryBarFeed(goldm_future, M30, bars)
        bars.clear()
        assert len(list(feed)) == 29

    def test_feeds_agree_with_each_other(
        self, calendar: MarketCalendar, tmp_path: Path, goldm_future: FutureId
    ) -> None:
        """CSV and parquet must be interchangeable, or backtests differ by format."""
        original = _sample(calendar)
        csv_path, pq_path = tmp_path / "b.csv", tmp_path / "b.parquet"
        write_csv_bars(original, csv_path)
        write_parquet_bars(original, pq_path)
        assert list(CsvBarFeed(goldm_future, M30, csv_path)) == list(
            ParquetBarFeed(goldm_future, M30, pq_path)
        )
