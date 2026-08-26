"""The Kotak Neo broker adapter. Brief §9, Milestone 7.

The fake implements the same `KotakTransport` surface the real SDK does, so
the whole adapter — connect, orders, ledger, reconciliation, crash recovery —
runs against scripted payloads. Nothing here touches a socket.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from algo.core.clock import BacktestClock
from algo.core.enums import Exchange, OrderState, OrderType, Right, Side, TimeInForce
from algo.core.errors import FatalBrokerError, RetryableBrokerError
from algo.core.instrument import FutureId, OptionId
from algo.core.order import Order
from algo.core.timeutil import utc
from algo.exchange.master import InstrumentMaster, MasterRow
from algo.execution.kotak import KotakBroker, KotakCredentials, _PlacedOrder
from algo.execution.router import OrderRouter, Outcome
from algo.persistence.journal import OrderJournal

NOW = utc(2026, 8, 19, 4, 0)  # 19-AUG-2026 09:30:00 IST

EXPIRY = date(2026, 9, 25)

FUTURE = FutureId(underlying="GOLDM", expiry=EXPIRY, exchange=Exchange.MCX)
CALL = OptionId(
    underlying_future=FUTURE,
    option_expiry=EXPIRY,
    strike=Decimal("148500"),
    right=Right.CE,
)

MASTER_ROWS = [
    MasterRow(
        symboltoken="578787",
        tradingsymbol="GOLDM25SEP26FUT",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="FUTCOM",
        expiry=EXPIRY,
        lot_size=Decimal("100"),
        tick_size=Decimal("50"),
        multiplier=Decimal("1"),
        precision=2,
        gen_num=Decimal("1"),
        gen_den=Decimal("1"),
        price_num=Decimal("1"),
        price_den=Decimal("1"),
    ),
    MasterRow(
        symboltoken="578788",
        tradingsymbol="GOLDM25SEP26148500CE",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="OPTFUT",
        expiry=EXPIRY,
        strike=Decimal("148500"),
        lot_size=Decimal("100"),
        tick_size=Decimal("50"),
        multiplier=Decimal("1"),
        precision=2,
        gen_num=Decimal("1"),
        gen_den=Decimal("1"),
        price_num=Decimal("1"),
        price_den=Decimal("1"),
    ),
    MasterRow(
        symboltoken="578789",
        tradingsymbol="GOLDM25SEP26148000PE",
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="OPTFUT",
        expiry=EXPIRY,
        strike=Decimal("148000"),
        lot_size=Decimal("100"),
        tick_size=Decimal("50"),
        multiplier=Decimal("1"),
        precision=2,
        gen_num=Decimal("1"),
        gen_den=Decimal("1"),
        price_num=Decimal("1"),
        price_den=Decimal("1"),
    ),
]

CREDS = KotakCredentials(
    consumer_key="consumer-key",
    mobile_number="+919000000000",
    ucc="UCC0001",
    totp_seed="JBSWY3DPEHPK3PXP",
    mpin="1234",
)


def _order(suffix: str = "0", side: Side = Side.SELL, **kw: Any) -> Order:
    params: dict[str, Any] = {"order_type": OrderType.MARKET, "created_at": NOW}
    params.update(kw)
    return Order(
        client_order_id=f"strat.sig123.{suffix}.0",
        signal_id="sig123",
        instrument=CALL,
        side=side,
        lots=1,
        qty=Decimal("100"),
        **params,
    )


def _book_entry(
    n_ord_no: str,
    *,
    side: str = "S",
    qty: str = "100",
    fld_qty: str = "0",
    state: str = "open",
    gui: str = "",
    hs_up_tm: str = "2026/08/19 09:30:05",
    rej: str = "",
) -> dict[str, Any]:
    return {
        "nOrdNo": n_ord_no,
        "exOrdId": f"EX{n_ord_no}",
        "tok": "578788",
        "trdSym": "GOLDM25SEP26148500CE",
        "trnsTp": side,
        "qty": qty,
        "fldQty": fld_qty,
        "avgPrc": "",
        "prc": "756",
        "prcTp": "L",
        "ordSt": state,
        "stat": state,
        "vldt": "DAY",
        "prod": "NRML",
        "lotSz": "100",
        "hsUpTm": hs_up_tm,
        "exCfmTm": "19-Aug-2026 09:30:05",
        "GuiOrdId": gui,
        "rejRsn": rej,
    }


class FakeKotakTransport:
    """The Kotak SDK surface, fully scripted."""

    def __init__(self) -> None:
        self.login_ok = True
        self.validate_ok = True
        self.validate_response: dict[str, Any] | None = None
        self.positions_response: dict[str, Any] | None = None
        self.login_error: Exception | None = None
        self.place_error: Exception | None = None
        self.place_ack: dict[str, Any] | None = None
        self.cancel_reject = False
        self.tag_supported = False
        self.book: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.position_rows: list[dict[str, Any]] = []
        self.limits_payload: dict[str, Any] = {
            "Net": "1000000",
            "MarginUsed": "75600",
            "stCode": 200,
            "stat": "Ok",
        }
        self.place_calls: list[dict[str, str]] = []
        self.cancel_calls: list[str] = []
        self.login_calls: list[tuple[str, str, str]] = []
        self.validate_calls: list[str] = []

    def totp_login(self, mobile_number: str, ucc: str, totp: str) -> dict[str, Any]:
        self.login_calls.append((mobile_number, ucc, totp))
        if self.login_error is not None:
            raise self.login_error
        if not self.login_ok:
            return {"Error Message": "Enter valid TOTP"}
        # The real envelope (D-113): no top-level stat/stCode, everything under
        # `data`, and the first stage carries only View scope. The fake used to
        # return a flat `stat: Ok`, which is a shape Kotak never sends for this
        # endpoint - and is why `_ack_ok` was broken for so long without a test
        # noticing.
        return {
            "data": {
                "token": "vt",
                "sid": "s",
                "ucc": ucc,
                "status": "success",
                "kType": "View",
            }
        }

    def totp_validate(self, mpin: str) -> dict[str, Any]:
        self.validate_calls.append(mpin)
        if not self.validate_ok:
            return {"Error Message": "MPIN invalid"}
        if self.validate_response is not None:
            return self.validate_response
        # MPIN validation is what upgrades the session to Trade scope.
        return {"data": {"token": "et", "sid": "es", "status": "success", "kType": "Trade"}}

    def place_order(
        self,
        *,
        exchange_segment: str,
        product: str,
        price: str,
        order_type: str,
        quantity: str,
        validity: str,
        trading_symbol: str,
        transaction_type: str,
        trigger_price: str = "0",
        tag: str | None = None,
    ) -> dict[str, Any]:
        self.place_calls.append(
            {
                "es": exchange_segment,
                "product": product,
                "price": price,
                "pt": order_type,
                "qt": quantity,
                "rt": validity,
                "ts": trading_symbol,
                "tt": transaction_type,
                "tp": trigger_price,
                "tag": tag or "",
            }
        )
        if self.place_error is not None:
            raise self.place_error
        if self.place_ack is not None:
            return self.place_ack
        return {"stat": "Ok", "nOrdNo": "1000042", "stCode": 200}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.cancel_calls.append(order_id)
        if self.cancel_reject:
            return {"stat": "Failed", "stCode": 400, "errMsg": "order already complete"}
        return {"stat": "Ok", "stCode": 200}

    def order_report(self, order_id: str = "") -> dict[str, Any]:
        return {"stat": "Ok", "stCode": 200, "data": list(self.book)}

    def trade_report(self) -> dict[str, Any]:
        return {"stat": "Ok", "stCode": 200, "data": list(self.trades)}

    def positions(self) -> dict[str, Any]:
        if self.positions_response is not None:
            return self.positions_response
        return {"stat": "Ok", "stCode": 200, "data": list(self.position_rows)}

    def limits(self) -> dict[str, Any]:
        return dict(self.limits_payload)

    def supports_tag(self) -> bool:
        return self.tag_supported


@pytest.fixture
def transport() -> FakeKotakTransport:
    return FakeKotakTransport()


@pytest.fixture
def master() -> InstrumentMaster:
    return InstrumentMaster(MASTER_ROWS, fetched_at=NOW)


@pytest.fixture
def clock() -> BacktestClock:
    return BacktestClock(NOW)


def _broker(
    transport: FakeKotakTransport,
    master: InstrumentMaster,
    clock: BacktestClock,
) -> KotakBroker:
    broker = KotakBroker(
        transport=transport,
        master=master,
        credentials=CREDS,
        clock=clock,
        totp=lambda: "123456",
    )
    broker.connect()
    return broker


class TestConnect:
    def test_session_established_via_totp_and_mpin(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        assert broker.health().connected
        assert transport.login_calls == [("+919000000000", "UCC0001", "123456")]
        assert transport.validate_calls == ["1234"]

    def test_rejected_totp_is_fatal(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.login_ok = False
        broker = KotakBroker(
            transport=transport, master=master, credentials=CREDS, clock=clock, totp=lambda: "x"
        )
        with pytest.raises(FatalBrokerError, match="TOTP"):
            broker.connect()
        assert not broker.health().connected

    def test_rejected_mpin_is_fatal(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.validate_ok = False
        broker = KotakBroker(
            transport=transport, master=master, credentials=CREDS, clock=clock, totp=lambda: "x"
        )
        with pytest.raises(FatalBrokerError, match="MPIN"):
            broker.connect()

    def test_network_failure_is_retryable(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.login_error = ConnectionError("socket gone")
        broker = KotakBroker(
            transport=transport, master=master, credentials=CREDS, clock=clock, totp=lambda: "x"
        )
        with pytest.raises(RetryableBrokerError, match="socket gone"):
            broker.connect()

    def test_incomplete_credentials_are_refused_at_construction(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        partial = CREDS.model_copy(update={"mpin": ""})
        with pytest.raises(FatalBrokerError, match="incomplete"):
            KotakBroker(transport=transport, master=master, credentials=partial, clock=clock)

    def test_calls_without_a_session_are_retryable(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = KotakBroker(
            transport=transport,
            master=master,
            credentials=CREDS,
            clock=clock,
            totp=lambda: "123456",
        )
        with pytest.raises(RetryableBrokerError, match="not established"):
            broker.open_orders()


class TestPlace:
    def test_params_are_mapped_to_the_kotak_contract(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        ref = broker.place(_order())
        assert ref.client_order_id == "strat.sig123.0.0"
        assert ref.broker_order_id == "1000042"
        call = transport.place_calls[-1]
        assert call["es"] == "mcx_fo"
        assert call["ts"] == "GOLDM25SEP26148500CE"
        assert call["tt"] == "SELL"
        assert call["pt"] == "MKT"
        assert call["qt"] == "100"
        assert call["rt"] == "DAY"
        assert call["price"] == "0"
        assert call["tp"] == "0"
        assert call["tag"] == ""

    def test_limit_order_carries_price_and_trigger(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(
            _order(
                order_type=OrderType.STOP_LIMIT,
                limit_price=Decimal("760"),
                trigger_price=Decimal("750"),
            )
        )
        call = transport.place_calls[-1]
        assert call["pt"] == "SL"
        assert call["price"] == "760"
        assert call["tp"] == "750"

    def test_tag_is_sent_when_the_sdk_supports_it(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.tag_supported = True
        broker = _broker(transport, master, clock)
        broker.place(_order())
        assert transport.place_calls[-1]["tag"] == "strat.sig123.0.0"

    def test_ioc_is_refused(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        with pytest.raises(FatalBrokerError, match="IOC"):
            broker.place(_order(tif=TimeInForce.IOC))
        assert not transport.place_calls

    def test_duplicate_client_id_is_refused(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(_order())
        with pytest.raises(FatalBrokerError, match="duplicate"):
            broker.place(_order())
        assert len(transport.place_calls) == 1

    def test_rejected_ack_is_fatal(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Failed", "stCode": 400, "errMsg": "no margin"}
        broker = _broker(transport, master, clock)
        with pytest.raises(FatalBrokerError, match="no margin"):
            broker.place(_order())

    def test_network_failure_is_retryable(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_error = ConnectionError("socket gone")
        broker = _broker(transport, master, clock)
        with pytest.raises(RetryableBrokerError, match="socket gone"):
            broker.place(_order())

    def test_empty_ack_is_resolved_from_the_book(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        transport.book = [_book_entry("1000099")]
        broker = _broker(transport, master, clock)
        ref = broker.place(_order())
        assert ref.broker_order_id == "1000099"

    def test_empty_ack_that_cannot_be_resolved_stays_unresolved(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        broker = _broker(transport, master, clock)
        ref = broker.place(_order())
        assert ref.broker_order_id == ""
        ledger_entry = broker._ledger["strat.sig123.0.0"]
        assert ledger_entry.broker_order_id == ""

    def test_ambiguous_resolution_is_fatal(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        transport.book = [
            _book_entry("1000099", hs_up_tm="2026/08/19 09:30:05"),
            _book_entry("1000100", hs_up_tm="2026/08/19 09:30:10"),
        ]
        broker = _broker(transport, master, clock)
        with pytest.raises(FatalBrokerError, match="refusing to guess"):
            broker.place(_order())

    def test_resolution_read_failure_leaves_the_order_sent_once(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        class FailingRead(FakeKotakTransport):
            def order_report(self, order_id: str = "") -> dict[str, Any]:
                raise ConnectionError("socket gone")

        failing = FailingRead()
        failing.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        broker = _broker(failing, master, clock)
        ref = broker.place(_order())
        assert ref.broker_order_id == ""
        assert len(broker._ledger) == 1


class TestCancel:
    def test_cancels_by_the_learned_broker_id(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(_order())
        broker.cancel("strat.sig123.0.0")
        assert transport.cancel_calls == ["1000042"]

    def test_unknown_order_cannot_be_cancelled(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        with pytest.raises(FatalBrokerError, match="no record"):
            broker.cancel("never.placed.0.0")

    def test_unresolved_order_cannot_be_cancelled(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        broker = _broker(transport, master, clock)
        broker.place(_order())
        with pytest.raises(FatalBrokerError, match="Reconcile first"):
            broker.cancel("strat.sig123.0.0")

    def test_rejected_cancel_is_fatal(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.cancel_reject = True
        broker = _broker(transport, master, clock)
        broker.place(_order())
        with pytest.raises(FatalBrokerError, match="already complete"):
            broker.cancel("strat.sig123.0.0")


class TestReads:
    def test_open_orders_excludes_terminal_states(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.book = [
            _book_entry("1", state="open"),
            _book_entry("2", state="complete", fld_qty="100"),
            _book_entry("3", state="rejected", rej="bad symbol"),
            _book_entry("4", state="cancelled"),
        ]
        broker = _broker(transport, master, clock)
        open_orders = broker.open_orders()
        assert [s.broker_order_id for s in open_orders] == ["1"]
        assert open_orders[0].state is OrderState.SENT

    def test_order_by_client_id_via_learned_id(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(_order())
        transport.book = [_book_entry("1000042", state="complete", fld_qty="100")]
        snapshot = broker.order_by_client_id("strat.sig123.0.0")
        assert snapshot is not None
        assert snapshot.state is OrderState.FILLED
        assert snapshot.filled_qty == Decimal("100")
        assert snapshot.client_order_id == "strat.sig123.0.0"

    def test_order_by_client_id_via_gui_echo(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.tag_supported = True
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        broker = _broker(transport, master, clock)
        broker.place(_order())
        transport.book = [_book_entry("1000042", gui="strat.sig123.0.0")]
        snapshot = broker.order_by_client_id("strat.sig123.0.0")
        assert snapshot is not None
        assert snapshot.broker_order_id == "1000042"

    def test_order_by_client_id_falls_back_for_unresolved_orders(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        broker = _broker(transport, master, clock)
        broker.place(_order())
        transport.book = [_book_entry("1000042")]
        snapshot = broker.order_by_client_id("strat.sig123.0.0")
        assert snapshot is not None
        assert snapshot.broker_order_id == "1000042"

    def test_ambiguous_fallback_is_fatal(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.place_ack = {"stat": "Ok", "nOrdNo": "", "stCode": 200}
        broker = _broker(transport, master, clock)
        broker.place(_order())
        transport.book = [
            _book_entry("1000042", hs_up_tm="2026/08/19 09:30:05"),
            _book_entry("1000043", hs_up_tm="2026/08/19 09:30:10"),
        ]
        with pytest.raises(FatalBrokerError, match="refusing to guess"):
            broker.order_by_client_id("strat.sig123.0.0")

    def test_unknown_client_id_returns_none(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        assert broker.order_by_client_id("never.seen.0.0") is None

    def test_executions_filter_by_since_and_map_fills(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(_order())
        transport.trades = [
            {
                "nOrdNo": "1000042",
                "flId": "9001",
                "tok": "578788",
                "trdSym": "GOLDM25SEP26148500CE",
                "trnsTp": "S",
                "fldQty": "100",
                "avgPrc": "756",
                "exTm": "19-Aug-2026 09:30:12",
                "hsUpTm": "2026/08/19 09:30:12",
            }
        ]
        fills = broker.executions(NOW - timedelta(minutes=5))
        assert len(fills) == 1
        fill = fills[0]
        assert fill.fill_id == "9001"
        assert fill.client_order_id == "strat.sig123.0.0"
        assert fill.broker_order_id == "1000042"
        assert fill.side is Side.SELL
        assert fill.qty == Decimal("100")
        assert fill.price == Decimal("756")
        assert fill.lots == 1
        assert fill.instrument_key == CALL.key
        assert fills[0].ts == utc(2026, 8, 19, 4, 0, 12)

    def test_executions_skip_stale_rows(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        transport.trades = [
            {
                "nOrdNo": "1000000",
                "flId": "9000",
                "tok": "578788",
                "trdSym": "GOLDM25SEP26148500CE",
                "trnsTp": "S",
                "fldQty": "100",
                "avgPrc": "750",
                "exTm": "19-Aug-2026 09:20:00",
            }
        ]
        assert broker.executions(NOW - timedelta(minutes=5)) == []

    def test_executions_without_a_token_resolve_by_symbol(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        transport.trades = [
            {
                "nOrdNo": "1000042",
                "flId": "9001",
                "tok": "",
                "trdSym": "GOLDM25SEP26148500CE",
                "trnsTp": "S",
                "fldQty": "100",
                "avgPrc": "756",
                "exTm": "19-Aug-2026 09:30:12",
            }
        ]
        fills = broker.executions(NOW - timedelta(minutes=5))
        assert len(fills) == 1
        assert fills[0].instrument_key == CALL.key

    def test_unknown_executions_get_ext_ids(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        transport.trades = [
            {
                "nOrdNo": "7770001",
                "flId": "9002",
                "tok": "578788",
                "trdSym": "GOLDM25SEP26148500CE",
                "trnsTp": "B",
                "fldQty": "100",
                "avgPrc": "760",
                "exTm": "19-Aug-2026 09:30:12",
            }
        ]
        fills = broker.executions(NOW - timedelta(minutes=5))
        assert fills[0].client_order_id == "ext:7770001"

    def test_positions_use_the_documented_average_formula(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.position_rows = [
            {
                "tok": "578788",
                "trdSym": "GOLDM25SEP26148500CE",
                "cfBuyQty": "0",
                "flBuyQty": "100",
                "cfSellQty": "0",
                "flSellQty": "0",
                "cfBuyAmt": "0",
                "buyAmt": "75600",
                "cfSellAmt": "0",
                "sellAmt": "0",
                "multiplier": "1",
                "genNum": "1",
                "genDen": "1",
                "prcNum": "1",
                "prcDen": "1",
                "lotSz": "100",
                "type": "OPTFUT",
            }
        ]
        broker = _broker(transport, master, clock)
        positions = broker.positions()
        assert len(positions) == 1
        assert positions[0].instrument_key == CALL.key
        assert positions[0].qty == Decimal("100")
        assert positions[0].lots == 1
        assert positions[0].average_price == Decimal("756")

    def test_positions_apply_generation_scaling(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.position_rows = [
            {
                "tok": "578788",
                "trdSym": "GOLDM25SEP26148500CE",
                "cfBuyQty": "0",
                "flBuyQty": "100",
                "cfSellQty": "0",
                "flSellQty": "0",
                "cfBuyAmt": "0",
                "buyAmt": "189000",
                "cfSellAmt": "0",
                "sellAmt": "0",
                "multiplier": "1",
                "genNum": "5",
                "genDen": "2",
                "prcNum": "1",
                "prcDen": "1",
                "lotSz": "100",
            }
        ]
        broker = _broker(transport, master, clock)
        assert broker.positions()[0].average_price == Decimal("756")

    def test_short_positions_carry_negative_signs(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.position_rows = [
            {
                "tok": "578788",
                "trdSym": "GOLDM25SEP26148500CE",
                "cfBuyQty": "0",
                "flBuyQty": "0",
                "cfSellQty": "0",
                "flSellQty": "100",
                "cfBuyAmt": "0",
                "buyAmt": "0",
                "cfSellAmt": "0",
                "sellAmt": "75600",
                "multiplier": "1",
                "genNum": "1",
                "genDen": "1",
                "prcNum": "1",
                "prcDen": "1",
                "lotSz": "100",
            }
        ]
        broker = _broker(transport, master, clock)
        assert broker.positions()[0].qty == Decimal("-100")
        assert broker.positions()[0].lots == -1
        assert broker.positions()[0].average_price == Decimal("756")

    def test_funds_map_net_and_margin_used(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)
        funds = broker.funds()
        assert funds.cash == Decimal("1000000")
        assert funds.margin_used == Decimal("75600")
        assert funds.margin_available == Decimal("924400")


class TestLedgerPersistence:
    def test_save_and_restore_round_trip(
        self,
        transport: FakeKotakTransport,
        master: InstrumentMaster,
        clock: BacktestClock,
        tmp_path: Path,
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(_order())
        path = tmp_path / "state" / "kotak_broker.json"
        broker.save(path)

        restored = KotakBroker(
            transport=transport,
            master=master,
            credentials=CREDS,
            clock=clock,
            totp=lambda: "123456",
        )
        restored.restore(path)
        placed = restored._ledger["strat.sig123.0.0"]
        assert isinstance(placed, _PlacedOrder)
        assert placed.broker_order_id == "1000042"
        assert placed.side is Side.SELL


class TestRouterIntegration:
    def test_place_flows_through_the_router(
        self,
        transport: FakeKotakTransport,
        master: InstrumentMaster,
        clock: BacktestClock,
        tmp_path: Path,
    ) -> None:
        broker = _broker(transport, master, clock)
        with OrderJournal(tmp_path / "journal.db") as journal:
            router = OrderRouter(broker=broker, journal=journal, clock=clock)
            assert router.reconcile().safe_to_trade
            assert router.place(_order()).outcome is Outcome.PLACED
            assert len(transport.place_calls) == 1

    def test_reconciliation_adopts_a_completed_order(
        self,
        transport: FakeKotakTransport,
        master: InstrumentMaster,
        clock: BacktestClock,
        tmp_path: Path,
    ) -> None:
        broker = _broker(transport, master, clock)
        with OrderJournal(tmp_path / "journal.db") as journal:
            router = OrderRouter(broker=broker, journal=journal, clock=clock)
            router.reconcile()
            router.place(_order())

            transport.book = [_book_entry("1000042", state="complete", fld_qty="100")]
            transport.trades = [
                {
                    "nOrdNo": "1000042",
                    "flId": "9001",
                    "tok": "578788",
                    "trdSym": "GOLDM25SEP26148500CE",
                    "trnsTp": "S",
                    "fldQty": "100",
                    "avgPrc": "756",
                    "exTm": "19-Aug-2026 09:30:12",
                }
            ]
            router.reconcile(since=NOW - timedelta(days=1))
            entry = journal.get("strat.sig123.0.0")
            assert entry is not None and entry.state is OrderState.FILLED
            assert len(journal.fills()) == 1

    def test_restarted_broker_answers_order_by_client_id(
        self,
        transport: FakeKotakTransport,
        master: InstrumentMaster,
        clock: BacktestClock,
        tmp_path: Path,
    ) -> None:
        broker = _broker(transport, master, clock)
        broker.place(_order())
        path = tmp_path / "kotak_broker.json"
        broker.save(path)

        transport.book = [_book_entry("1000042", state="complete", fld_qty="100")]
        restored = KotakBroker(
            transport=transport,
            master=master,
            credentials=CREDS,
            clock=clock,
            totp=lambda: "123456",
        )
        restored.restore(path)
        restored.connect()
        snapshot = restored.order_by_client_id("strat.sig123.0.0")
        assert snapshot is not None
        assert snapshot.state is OrderState.FILLED


class TestTheLoginEnvelope:
    """D-113. Kotak answers the login endpoints with a shape `_ack_ok` did not
    know, so `connect()` read a successful login as a rejection and the adapter
    could never establish a real session. Found the first time it was pointed at
    the live API; these pin the real shapes so it cannot regress.
    """

    def test_the_real_nested_success_envelope_is_accepted(self) -> None:
        from algo.execution.kotak import _ack_ok

        # Exactly what the live API returns: no stat, no stCode.
        assert _ack_ok({"data": {"status": "success", "token": "x", "kType": "Trade"}})

    def test_the_flat_envelope_still_works(self) -> None:
        """Order and lookup endpoints do use the flat shape - the fix must not
        have traded one for the other."""
        from algo.execution.kotak import _ack_ok

        assert _ack_ok({"stat": "Ok"})
        assert _ack_ok({"stCode": 200})

    def test_a_nested_failure_is_not_accepted(self) -> None:
        from algo.execution.kotak import _ack_ok

        assert not _ack_ok({"data": {"status": "failure"}})
        assert not _ack_ok({"data": {"status": ""}})
        assert not _ack_ok({"data": {"token": "x"}})  # no status at all
        assert not _ack_ok({"data": "not-a-dict"})
        assert not _ack_ok({})
        assert not _ack_ok(None)

    def test_a_view_only_session_is_refused_at_connect(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        """The first login stage succeeds with View scope; only MPIN validation
        upgrades it. Accepting a View session would surface as a rejected order
        mid-strategy, with a leg possibly already open."""
        transport.validate_response = {
            "data": {"token": "et", "sid": "es", "status": "success", "kType": "View"}
        }
        # Constructed directly: `_broker` connects for you, which would raise
        # before pytest.raises could see it.
        broker = KotakBroker(
            transport=transport,
            master=master,
            credentials=CREDS,
            clock=clock,
            totp=lambda: "123456",
        )

        with pytest.raises(FatalBrokerError, match="not trading-scoped"):
            broker.connect()

        assert not broker.health().connected

    def test_a_trade_scoped_session_connects(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        broker = _broker(transport, master, clock)

        broker.connect()

        assert broker.health().connected


class TestAnEmptyBookIsNotAFailure:
    """D-113. Kotak reports an empty book as an *error* envelope. The first live
    run died on `Kotak trade report call failed: No Data` - which just meant the
    account had not traded that day, the state every session starts in.
    """

    #: Exactly what the live API returns for an empty trade/order/position book.
    EMPTY: ClassVar[dict[str, Any]] = {
        "stat": "Not_Ok",
        "stCode": 5203,
        "errMsg": "No Data",
        "desc": "",
    }

    def test_the_empty_envelope_reads_as_no_rows(self) -> None:
        from algo.execution.kotak import _ok_data

        assert _ok_data(dict(self.EMPTY), "trade report") == []

    def test_a_real_failure_still_raises(self) -> None:
        """The narrow check must not have turned every Not_Ok into silence."""
        from algo.execution.kotak import _ok_data

        with pytest.raises(FatalBrokerError):
            _ok_data({"stat": "Not_Ok", "stCode": 400, "errMsg": "Bad request"}, "orders")

    def test_a_different_no_data_code_still_raises(self) -> None:
        from algo.execution.kotak import _ok_data

        with pytest.raises(FatalBrokerError):
            _ok_data({"stat": "Not_Ok", "stCode": 9999, "errMsg": "No Data"}, "orders")

    def test_the_code_alone_is_not_enough(self) -> None:
        from algo.execution.kotak import _ok_data

        with pytest.raises(FatalBrokerError):
            _ok_data({"stat": "Not_Ok", "stCode": 5203, "errMsg": "Session expired"}, "orders")

    def test_a_payload_carrying_data_is_never_treated_as_empty(self) -> None:
        """If rows came back, they must be read - not discarded because the
        envelope also looked like the empty one. Believing the account is flat
        when it is not is the worst thing this adapter can do."""
        from algo.execution.kotak import _ok_data

        payload = dict(self.EMPTY)
        payload["data"] = [{"nOrdNo": "1"}]

        with pytest.raises(FatalBrokerError):
            _ok_data(payload, "orders")

    def test_positions_reads_empty_rather_than_raising(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        """The path reconciliation actually takes on a fresh account."""
        transport.positions_response = dict(self.EMPTY)
        broker = _broker(transport, master, clock)

        assert broker.positions() == []


class TestABridgeOutageIsRetryable:
    """D-114. Kotak's `limits` endpoint answered `stCode: 300015`,
    "bridge API error out", consistently, while positions and the order book
    answered normally. That is a backend component being down, not a rejection
    on the merits - and this module's rule is that only rejections are fatal.
    """

    OUTAGE: ClassVar[dict[str, Any]] = {
        "stat": "bridge API error out",
        "stCode": "300015",
        "errMsg": "bridge API error out",
    }

    def test_a_book_read_raises_retryable_not_fatal(self) -> None:
        """Fatal would kill a live session over an outage that may clear."""
        from algo.execution.kotak import _ok_data

        with pytest.raises(RetryableBrokerError, match="backend outage"):
            _ok_data(dict(self.OUTAGE), "trade report")

    def test_the_funds_read_is_retryable_too(
        self, transport: FakeKotakTransport, master: InstrumentMaster, clock: BacktestClock
    ) -> None:
        transport.limits_payload = dict(self.OUTAGE)
        broker = _broker(transport, master, clock)

        with pytest.raises(RetryableBrokerError, match="backend outage"):
            broker.funds()

    def test_the_string_status_code_is_parsed(self) -> None:
        """The live payload sends stCode as a string, unlike the numeric 5203
        seen elsewhere - so the check must not depend on the JSON type."""
        from algo.execution.kotak import _is_bridge_outage

        assert _is_bridge_outage({"stCode": "300015"})
        assert _is_bridge_outage({"stCode": 300015})

    def test_other_failures_are_untouched(self) -> None:
        from algo.execution.kotak import _is_bridge_outage

        assert not _is_bridge_outage({"stCode": 5203, "errMsg": "No Data"})
        assert not _is_bridge_outage({"stCode": 400})
        assert not _is_bridge_outage({"stCode": "not-a-number"})
        assert not _is_bridge_outage({})

    def test_an_empty_book_is_still_empty_not_an_outage(self) -> None:
        """The two live error shapes must not be confused for one another."""
        from algo.execution.kotak import _ok_data

        assert _ok_data({"stat": "Not_Ok", "stCode": 5203, "errMsg": "No Data"}, "x") == []
