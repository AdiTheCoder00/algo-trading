"""Kotak live chain feed: snapshots from bare quote lists. §2.8, M1.5.

The chain guarantee that matters is chain.py:3-7 — the futures quote and the
option quotes are read in the same request cycle, so they share an instant.
The fake transport replays scripted bare-list payloads; nothing here touches a
socket.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import islice
from typing import Any

import pytest

from algo.core.clock import BacktestClock
from algo.core.enums import Exchange, Right
from algo.core.errors import DataError, RetryableBrokerError
from algo.core.timeutil import utc
from algo.data.kotak_feed import KotakChainFeed
from algo.data.live import SessionWindow
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.master import InstrumentMaster, MasterRow

NOW = utc(2026, 8, 19, 8, 0)  # 13:30 IST — mid-session, US DST regime

MASTER_ROWS = [
    MasterRow(
        symboltoken="578787",
        tradingsymbol="GOLDM25SEP26FUT",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="FUTCOM",
        expiry=date(2026, 9, 25),
        lot_size=Decimal("100"),
        tick_size=Decimal("50"),
    ),
    MasterRow(
        symboltoken="578788",
        tradingsymbol="GOLDM25SEP26148500CE",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="OPTFUT",
        expiry=date(2026, 9, 25),
        strike=Decimal("148500"),
        lot_size=Decimal("100"),
        tick_size=Decimal("50"),
    ),
    MasterRow(
        symboltoken="578789",
        tradingsymbol="GOLDM25SEP26148000PE",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="OPTFUT",
        expiry=date(2026, 9, 25),
        strike=Decimal("148000"),
        lot_size=Decimal("100"),
        tick_size=Decimal("50"),
    ),
]


def _quote(exchange_token: str, *, ltp: str, bid: str, ask: str) -> dict[str, Any]:
    return {
        "exchange_token": exchange_token,
        "ltp": ltp,
        "open_int": "2000",
        "lstup_time": "1729068000",
        "depth": {
            "buy": [{"price": bid, "quantity": "3", "orders": "1"}],
            "sell": [{"price": ask, "quantity": "4", "orders": "2"}],
        },
    }


class FakeQuotesTransport:
    """The Kotak quotes surface: a bare list keyed by exchange_token."""

    def __init__(self) -> None:
        self.quote_payloads: dict[str, dict[str, Any]] = {}
        self.payload: Any = None

    def quotes(self, exchange_segments: list[dict[str, str]]) -> Any:
        if self.payload is not None:
            return self.payload
        return [
            payload
            for token, payload in self.quote_payloads.items()
            if token in {item["instrument_token"] for item in exchange_segments}
        ]


@pytest.fixture
def master() -> InstrumentMaster:
    return InstrumentMaster(MASTER_ROWS, fetched_at=NOW)


@pytest.fixture
def clock() -> BacktestClock:
    return BacktestClock(NOW)


@pytest.fixture
def session() -> SessionWindow:
    return SessionWindow(synthetic_calendar())


EXPIRY = date(2026, 9, 25)


class TestChainFeed:
    def test_snapshot_has_futures_anchor_and_sorted_rows(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeQuotesTransport()
        transport.quote_payloads = {
            "578787": _quote("MCX|578787", ltp="148500", bid="148450", ask="148550"),
            "578788": _quote("MCX|578788", ltp="756", bid="750", ask="762"),
            "578789": _quote("MCX|578789", ltp="612", bid="608", ask="616"),
        }
        feed = KotakChainFeed(
            transport=transport,
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        snapshot = next(islice(feed.snapshots(EXPIRY), 1))
        assert snapshot.underlying == "GOLDM"
        assert snapshot.option_expiry == EXPIRY
        assert snapshot.futures_price == Decimal("148500")
        assert snapshot.futures_quote is not None
        assert snapshot.futures_quote.bid == Decimal("148450")
        # Rows sorted by (strike, right) — the chain invariant.
        assert [r.strike for r in snapshot.rows] == [Decimal("148000"), Decimal("148500")]
        call = snapshot.by_strike(Decimal("148500"), Right.CE)
        assert call is not None
        assert call.quote.ask == Decimal("762")
        assert call.priced_from == "MID"
        assert call.quote.open_interest == 2000

    def test_the_options_underlying_future_carries_the_futures_own_expiry(
        self, clock: BacktestClock, session: SessionWindow
    ) -> None:
        """`algo/core/instrument.py` warns explicitly that `option_expiry` and
        `underlying_future.expiry` are different dates - the option expires
        first - and conflating them "is precisely the error that walks a
        short leg into devolvement." This master deliberately gives the
        futures contract a *later* expiry than the option chain, so a bug
        that reused `option_expiry` for both cannot hide behind coincidental
        matching dates the way `MASTER_ROWS` above would let it."""
        futures_expiry = date(2026, 10, 30)
        option_expiry = date(2026, 9, 25)
        master = InstrumentMaster(
            [
                MasterRow(
                    symboltoken="1",
                    tradingsymbol="GOLDM30OCT26FUT",
                    exch_seg="MCX",
                    name="GOLDM",
                    instrumenttype="FUTCOM",
                    expiry=futures_expiry,
                    lot_size=Decimal("100"),
                    tick_size=Decimal("50"),
                ),
                MasterRow(
                    symboltoken="2",
                    tradingsymbol="GOLDM25SEP26148500CE",
                    exch_seg="MCX",
                    name="GOLDM",
                    instrumenttype="OPTFUT",
                    expiry=option_expiry,
                    strike=Decimal("148500"),
                    lot_size=Decimal("100"),
                    tick_size=Decimal("50"),
                ),
            ],
            fetched_at=NOW,
        )
        transport = FakeQuotesTransport()
        transport.quote_payloads = {
            "1": _quote("MCX|1", ltp="148500", bid="148450", ask="148550"),
            "2": _quote("MCX|2", ltp="756", bid="750", ask="762"),
        }
        feed = KotakChainFeed(
            transport=transport,
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )

        snapshot = next(islice(feed.snapshots(option_expiry), 1))

        call = snapshot.by_strike(Decimal("148500"), Right.CE)
        assert call is not None
        assert call.option.option_expiry == option_expiry
        assert call.option.underlying_future.expiry == futures_expiry

    def test_missing_underlying_quote_is_retryable(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeQuotesTransport()
        transport.quote_payloads = {"578788": _quote("578788", ltp="756", bid="750", ask="762")}
        feed = KotakChainFeed(
            transport=transport,
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        with pytest.raises(RetryableBrokerError, match="underlying"):
            future = master.future_rows("GOLDM", Exchange.MCX)[0]
            options = master.option_rows("GOLDM", Exchange.MCX, EXPIRY)
            feed._poll(future, options)

    def test_error_payload_is_retryable(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeQuotesTransport()
        transport.payload = {"Error": "rate limited"}
        feed = KotakChainFeed(
            transport=transport,
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        with pytest.raises(RetryableBrokerError, match="poll failed"):
            next(islice(feed.snapshots(EXPIRY), 1))

    def test_unreadable_payload_is_retryable(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeQuotesTransport()
        transport.payload = "definitely not json"
        feed = KotakChainFeed(
            transport=transport,
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        with pytest.raises(RetryableBrokerError, match="unreadable"):
            next(islice(feed.snapshots(EXPIRY), 1))

    def test_transport_exception_is_retryable(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        class RaisingTransport(FakeQuotesTransport):
            def quotes(self, exchange_segments: list[dict[str, str]]) -> Any:
                raise ConnectionError("socket gone")

        feed = KotakChainFeed(
            transport=RaisingTransport(),
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        with pytest.raises(RetryableBrokerError, match="socket gone"):
            next(islice(feed.snapshots(EXPIRY), 1))

    def test_unreadable_payload_leaves_untradeable_quotes(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        transport = FakeQuotesTransport()
        transport.quote_payloads = {
            "578787": _quote("578787", ltp="148500", bid="148450", ask="148550"),
            "578788": {"exchange_token": "578788", "ltp": "756", "volume": "junk"},
            "578789": {"exchange_token": "578789"},
        }
        feed = KotakChainFeed(
            transport=transport,
            master=master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        snapshot = next(islice(feed.snapshots(EXPIRY), 1))
        assert len(snapshot.rows) == 2
        for row in snapshot.rows:
            assert not row.is_tradeable  # iv/delta are None at the recorder level

    def test_no_futures_in_master_is_a_data_error(
        self, master: InstrumentMaster, clock: BacktestClock, session: SessionWindow
    ) -> None:
        empty_master = InstrumentMaster(MASTER_ROWS[1:], fetched_at=NOW)
        feed = KotakChainFeed(
            transport=FakeQuotesTransport(),
            master=empty_master,
            underlying="GOLDM",
            clock=clock,
            session=session,
            poll_interval_s=0.0,
        )
        with pytest.raises(DataError, match="no GOLDM futures"):
            next(islice(feed.snapshots(EXPIRY), 1))
