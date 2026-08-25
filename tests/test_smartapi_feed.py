"""Live bar feed: closed bars from the SmartAPI candle API. §2.8, M1.5.

The guarantee that matters is the one feed.py states: bars are **closed** and
strictly increasing. The fake transport replays scripted payloads; nothing here
touches a socket. Tick aggregation (`TickBarBuilder`) and session windows
(`SessionWindow`) live in `algo/data/live.py`; their tests stay here because
they were born here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from algo.core.bar import M30
from algo.core.clock import BacktestClock
from algo.core.enums import Exchange
from algo.core.errors import DataError, RetryableBrokerError
from algo.core.instrument import FutureId
from algo.core.timeutil import utc
from algo.data.live import SessionWindow, TickBarBuilder
from algo.data.smartapi_feed import SmartApiBarFeed
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.master import InstrumentMaster, MasterRow

NOW = utc(2026, 8, 19, 8, 0)  # 13:30 IST — mid-session, US DST regime

FUTURE = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX)

MASTER_ROWS = [
    MasterRow(
        symboltoken="20001",
        tradingsymbol="GOLDM04SEP26FUT",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="FUTCUR",
        expiry=date(2026, 9, 4),
        lot_size=Decimal("100"),
        tick_size=Decimal("0.5"),
    ),
]


class FakeDataTransport:
    """The candle surface, fully scripted."""

    def __init__(self) -> None:
        self.candle_rows: list[list[Any]] = []

    def candles(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"status": True, "data": list(self.candle_rows)}


@pytest.fixture
def master() -> InstrumentMaster:
    return InstrumentMaster(MASTER_ROWS, fetched_at=NOW)


@pytest.fixture
def clock() -> BacktestClock:
    return BacktestClock(NOW)


@pytest.fixture
def session() -> SessionWindow:
    return SessionWindow(synthetic_calendar())


def _bar_payload(ts: str, o: str, h: str, lo: str, c: str, v: int) -> list[Any]:
    return [ts, o, h, lo, c, v]


class TestBarFeed:
    def test_yields_only_closed_bars(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeDataTransport()
        transport.candle_rows = [
            _bar_payload("2026-08-19 09:30:00", "156000", "156500", "155900", "156400", 10),
            _bar_payload("2026-08-19 10:00:00", "156400", "156800", "156200", "156600", 8),
        ]
        feed = SmartApiBarFeed(
            transport=transport,
            master=master,
            instrument=FUTURE,
            timeframe=M30,
            clock=clock,
            session=session,
        )
        bars = list(feed)
        assert [b.ts for b in bars] == [
            utc(2026, 8, 19, 4, 0),
            utc(2026, 8, 19, 4, 30),
        ]
        assert all(not b.is_partial for b in bars)
        assert bars[0].open == Decimal("156000")
        assert bars[0].close == Decimal("156400")
        assert bars[1].volume == 8

    def test_drops_in_progress_candle(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        """The candle API includes the still-running bar; the feed must not."""
        transport = FakeDataTransport()
        transport.candle_rows = [
            _bar_payload("2026-08-19 09:30:00", "156000", "156500", "155900", "156400", 10),
            # 13:30:00 IST is the slot forming right now (NOW = 13:30 IST) —
            # the candle API would include it, but it is still running.
            _bar_payload("2026-08-19 13:30:00", "156400", "156900", "156300", "156800", 5),
        ]
        feed = SmartApiBarFeed(
            transport=transport,
            master=master,
            instrument=FUTURE,
            timeframe=M30,
            clock=clock,
            session=session,
        )
        assert len(list(feed)) == 1

    def test_non_increasing_timestamps_are_a_data_error(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeDataTransport()
        transport.candle_rows = [
            _bar_payload("2026-08-19 10:00:00", "156000", "156500", "155900", "156400", 10),
            _bar_payload("2026-08-19 09:30:00", "156400", "156800", "156200", "156600", 8),
        ]
        feed = SmartApiBarFeed(
            transport=transport,
            master=master,
            instrument=FUTURE,
            timeframe=M30,
            clock=clock,
            session=session,
        )
        with pytest.raises(DataError, match="non-increasing"):
            list(feed)

    def test_api_failure_is_retryable(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        feed = SmartApiBarFeed(
            transport=FakeDataTransport(),
            master=master,
            instrument=FUTURE,
            timeframe=M30,
            clock=clock,
            session=session,
        )

        class BrokenTransport(FakeDataTransport):
            def candles(self, params: dict[str, Any]) -> dict[str, Any]:
                return {"status": False, "message": "limit exceeded"}

        feed._transport = BrokenTransport()
        with pytest.raises(RetryableBrokerError, match="candle"):
            list(feed)

    def test_candle_failure_raises_as_retryable(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        feed = SmartApiBarFeed(
            transport=FakeDataTransport(),
            master=master,
            instrument=FUTURE,
            timeframe=M30,
            clock=clock,
            session=session,
        )

        class RaisingTransport(FakeDataTransport):
            def candles(self, params: dict[str, Any]) -> dict[str, Any]:
                raise ConnectionError("socket gone")

        feed._transport = RaisingTransport()
        with pytest.raises(RetryableBrokerError, match="socket gone"):
            list(feed)


class TestTickBarBuilder:
    def test_ticks_aggregate_into_closed_bars(self) -> None:
        builder = TickBarBuilder(M30)
        builder.feed(utc(2026, 8, 19, 4, 1), Decimal("156000"), 3)
        builder.feed(utc(2026, 8, 19, 4, 15), Decimal("156400"), 5)
        builder.feed(utc(2026, 8, 19, 4, 25), Decimal("156200"), 2)
        # First boundary is 04:31; the tick at 04:31 closes the bar.
        builder.feed(utc(2026, 8, 19, 4, 31), Decimal("156300"), 1)
        bars = builder.drained()
        assert len(bars) == 1
        bar = bars[0]
        # Labelled by its close instant (bar.py:56).
        assert bar.ts == utc(2026, 8, 19, 4, 31)
        assert (bar.open, bar.high, bar.low, bar.close) == (
            Decimal("156000"),
            Decimal("156400"),
            Decimal("156000"),
            Decimal("156200"),
        )
        assert bar.volume == 10
        assert not bar.is_partial
        # A second drain is empty — drained() consumed the bar list.
        assert builder.drained() == []

    def test_partial_bar_flagged_on_session_end(self) -> None:
        builder = TickBarBuilder(M30)
        builder.feed(utc(2026, 8, 19, 4, 1), Decimal("156000"), 3)
        builder.close_partial()
        bars = builder.drained()
        assert len(bars) == 1
        assert bars[0].is_partial

    def test_close_partial_is_idempotent(self) -> None:
        builder = TickBarBuilder(M30)
        builder.feed(utc(2026, 8, 19, 4, 1), Decimal("156000"), 3)
        builder.close_partial()
        builder.close_partial()
        assert len(builder.drained()) == 1

    def test_empty_builder_closes_nothing(self) -> None:
        builder = TickBarBuilder(M30)
        builder.close_partial()
        assert builder.drained() == []


class TestSessionWindow:
    def test_open_and_close_are_utc_aware(self, session: SessionWindow) -> None:
        day = date(2026, 8, 19)
        assert session.open_at(day) == utc(2026, 8, 19, 3, 30)  # 09:00 IST
        assert session.close_at(day) == utc(2026, 8, 19, 18, 0)  # 23:30 IST, US DST
        assert session.is_live(utc(2026, 8, 19, 12, 0))
        assert not session.is_live(utc(2026, 8, 19, 2, 0))
        assert not session.is_live(utc(2026, 8, 19, 18, 30))


class TestTheSdkCannotLogCredentials:
    """The SDK logs full request bodies - password and TOTP included - at ERROR
    level on any failed call, to stderr and to a `logs/<date>/app.log` file it
    creates itself. Confirmed by hitting it directly: one failed login wrote a
    real MPIN and TOTP code to disk in plaintext. `_silence_smartapi_logger`
    (called from `SmartConnectTransport.__init__`, before the SDK object is ever
    constructed) is the fix; this asserts it actually leaves the logger inert."""

    def test_the_logger_is_disabled_after_silencing(self) -> None:
        import logging

        import logzero

        from algo.data.smartapi_feed import _silence_smartapi_logger

        # Start from a state the SDK's own logging would produce, so the test
        # would fail if silencing did nothing.
        logzero.logger.disabled = False
        logzero.logger.setLevel(logging.INFO)

        _silence_smartapi_logger()

        assert logzero.logger.disabled is True
        assert logzero.logger.handlers == []
        assert logzero.logger.level > logging.CRITICAL
        # An error logged after silencing must not reach any handler.
        logzero.logger.error("password=hunter2 totp=123456")

    def test_the_sdk_cannot_re_attach_a_file_handler(self, tmp_path: Path) -> None:
        """`SmartConnect.__init__` calls `logzero.logfile(path, ...)` itself,
        after silencing runs. That call must be a no-op, or the file sink comes
        back the moment the SDK is constructed."""
        import logzero

        from algo.data.smartapi_feed import _silence_smartapi_logger

        _silence_smartapi_logger()
        logzero.logfile(str(tmp_path / "app.log"), loglevel=10)

        assert not (tmp_path / "app.log").exists()
        assert logzero.logger.handlers == []
