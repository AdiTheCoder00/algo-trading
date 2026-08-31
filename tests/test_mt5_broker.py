"""The MT5 adapter (D-122).

Three properties carry real money here.

**The unit conversion.** One engine lot is one ounce; MT5 sizes in 100-ounce
lots. Reading a size in the wrong unit is a hundredfold position error, so the
conversion is tested in both directions and at the venue's own limits.

**At most once.** A duplicated send is a duplicated position. The router enforces
this too; the adapter does not rely on that.

**Foreign orders stay foreign.** The account already carries positions this
system did not open and 72 deals it did not place. Anything without our magic
number must not be adopted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.clock import Clock
from algo.core.enums import OrderState, OrderType, ProductType, Side, TimeInForce
from algo.core.errors import DataError, FatalBrokerError, RetryableBrokerError
from algo.core.instrument import CfdId
from algo.core.order import Order
from algo.execution.mt5_broker import (
    MAGIC,
    Mt5Broker,
    lots_to_volume,
    volume_to_lots,
)

NOW = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)
XAUUSD = CfdId(symbol="XAUUSD")


class FrozenClock(Clock):
    def now(self) -> datetime:
        return NOW


class Row:
    """A generic MT5 result row - the API returns namedtuple-likes."""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class FakeTerminal:
    """Stands in for the MetaTrader5 module."""

    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1

    def __init__(
        self,
        *,
        filling_mask: int = 2,  # IOC only, as Vantage reports for XAUUSD
        retcode: int = 10009,
        trade_allowed: bool = True,
        account: bool = True,
        tick: bool = True,
    ) -> None:
        self.sent: list[dict[str, object]] = []
        self.positions: list[Row] = []
        self.orders: list[Row] = []
        self.deals: list[Row] = []
        self._filling_mask = filling_mask
        self._retcode = retcode
        self._trade_allowed = trade_allowed
        self._account = account
        self._tick = tick

    def initialize(self, *args: object, **kwargs: object) -> bool:
        return True

    def shutdown(self) -> None: ...

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake")

    def account_info(self) -> Row | None:
        if not self._account:
            return None
        return Row(
            login=25804244,
            server="VantageMarkets-Demo",
            balance=108805.15,
            margin=0.31,
            margin_free=108812.76,
            trade_allowed=self._trade_allowed,
        )

    def symbol_info(self, symbol: str) -> Row:
        return Row(filling_mode=self._filling_mask)

    def symbol_info_tick(self, symbol: str) -> Row | None:
        if not self._tick:
            return None
        return Row(bid=4458.82, ask=4459.10, time=int(NOW.timestamp()))

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def order_send(self, request: dict[str, object]) -> Row:
        self.sent.append(request)
        return Row(retcode=self._retcode, order=987654, price=4459.10, comment="Done")

    def positions_get(self, **kwargs: object) -> tuple[Row, ...]:
        return tuple(self.positions)

    def orders_get(self, **kwargs: object) -> tuple[Row, ...]:
        return tuple(self.orders)

    def history_deals_get(self, *args: object, **kwargs: object) -> tuple[Row, ...]:
        return tuple(self.deals)


def _broker(terminal: FakeTerminal | None = None) -> Mt5Broker:
    broker = Mt5Broker(
        terminal=terminal or FakeTerminal(),
        symbol="XAUUSD",
        clock=FrozenClock(),
    )
    broker.connect()
    return broker


def _order(coid: str = "c-1", side: Side = Side.BUY, lots: int = 100) -> Order:
    return Order(
        client_order_id=coid,
        signal_id="s-1",
        instrument=XAUUSD,
        side=side,
        lots=lots,
        qty=Decimal(lots),
        order_type=OrderType.MARKET,
        product=ProductType.NRML,
        tif=TimeInForce.DAY,
        created_at=NOW,
    )


class TestTheUnitConversion:
    """One engine lot is one ounce. MT5 sizes in 100-ounce lots. Confusing the
    two is a hundredfold position error."""

    @pytest.mark.parametrize(
        "lots,volume", [(1, "0.01"), (10, "0.1"), (100, "1"), (250, "2.5"), (10000, "100")]
    )
    def test_lots_to_volume(self, lots: int, volume: str) -> None:
        assert lots_to_volume(lots) == Decimal(volume)

    @pytest.mark.parametrize("lots", [1, 7, 100, 250, 10000])
    def test_it_round_trips(self, lots: int) -> None:
        assert volume_to_lots(lots_to_volume(lots)) == lots

    def test_the_venue_minimum_is_one_engine_lot(self) -> None:
        """MT5 volume_min 0.01 == 1 ounce == the smallest thing we can trade."""
        assert volume_to_lots(0.01) == 1

    def test_the_venue_maximum(self) -> None:
        assert volume_to_lots(100.0) == 10000

    def test_a_volume_finer_than_the_step_is_refused_not_rounded(self) -> None:
        """A silently rounded size is a silently wrong position."""
        with pytest.raises(DataError, match="Refusing to round"):
            volume_to_lots(0.015)


class TestPlacingAnOrder:
    def test_the_volume_sent_is_in_mt5_units(self) -> None:
        terminal = FakeTerminal()
        _broker(terminal).place(_order(lots=100))

        assert terminal.sent[0]["volume"] == 1.0, "100 ounces is one MT5 lot"

    def test_our_magic_is_stamped_on_it(self) -> None:
        terminal = FakeTerminal()
        _broker(terminal).place(_order())

        assert terminal.sent[0]["magic"] == MAGIC

    def test_the_filling_mode_is_read_from_the_symbol(self) -> None:
        """Vantage reports IOC-only for XAUUSD; sending FOK gets it rejected."""
        terminal = FakeTerminal(filling_mask=2)
        _broker(terminal).place(_order())

        assert terminal.sent[0]["type_filling"] == FakeTerminal.ORDER_FILLING_IOC

    def test_an_unfillable_symbol_is_refused_rather_than_guessed(self) -> None:
        broker = _broker(FakeTerminal(filling_mask=0))

        with pytest.raises(FatalBrokerError, match="neither FOK nor IOC"):
            broker.place(_order())

    def test_a_sell_sends_the_bid(self) -> None:
        terminal = FakeTerminal()
        _broker(terminal).place(_order(side=Side.SELL))

        assert terminal.sent[0]["price"] == 4458.82
        assert terminal.sent[0]["type"] == 1

    def test_a_rejection_is_fatal_and_names_the_code(self) -> None:
        broker = _broker(FakeTerminal(retcode=10013))

        with pytest.raises(FatalBrokerError, match="retcode 10013"):
            broker.place(_order())

    def test_only_market_orders_are_translated(self) -> None:
        broker = _broker()
        limit = _order().model_copy(update={"order_type": OrderType.LIMIT})

        with pytest.raises(FatalBrokerError, match="only MARKET"):
            broker.place(limit)


class TestAtMostOnce:
    """A duplicated send is a duplicated position."""

    def test_replaying_the_same_id_does_not_send_again(self) -> None:
        terminal = FakeTerminal()
        broker = _broker(terminal)

        first = broker.place(_order("c-1"))
        second = broker.place(_order("c-1"))

        assert len(terminal.sent) == 1
        assert first.broker_order_id == second.broker_order_id

    def test_a_different_id_does_send(self) -> None:
        terminal = FakeTerminal()
        broker = _broker(terminal)

        broker.place(_order("c-1"))
        broker.place(_order("c-2"))

        assert len(terminal.sent) == 2


class TestTheLedgerSurvivesARestart:
    """MT5 overwrites the order comment, so the broker cannot answer
    `order_by_client_id` - only our own ledger can."""

    def test_it_answers_from_the_ledger(self) -> None:
        broker = _broker()
        broker.place(_order("c-9"))

        snapshot = broker.order_by_client_id("c-9")

        assert snapshot is not None
        assert snapshot.broker_order_id == "987654"
        assert snapshot.state is OrderState.FILLED

    def test_an_unknown_id_is_none(self) -> None:
        assert _broker().order_by_client_id("never-sent") is None

    def test_it_round_trips_through_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "mt5.json"
        first = _broker()
        first.place(_order("c-5"))
        first.save(path)

        restarted = _broker()
        assert restarted.order_by_client_id("c-5") is None
        restarted.restore(path)

        snapshot = restarted.order_by_client_id("c-5")
        assert snapshot is not None
        assert snapshot.broker_order_id == "987654"

    def test_restoring_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        broker = _broker()
        broker.restore(tmp_path / "absent.json")

        assert broker.order_by_client_id("anything") is None

    def test_the_saved_file_is_readable_json(self, tmp_path: Path) -> None:
        path = tmp_path / "mt5.json"
        broker = _broker()
        broker.place(_order("c-1"))
        broker.save(path)

        assert json.loads(path.read_text(encoding="utf-8"))["orders"][0][
            "client_order_id"
        ] == "c-1"


class TestHedgingIsNettedForTheEngine:
    """The account holds a ticket per trade; `Portfolio` wants one signed net
    position per instrument."""

    def test_two_longs_net_to_their_sum(self) -> None:
        terminal = FakeTerminal()
        terminal.positions = [
            Row(type=0, volume=1.0, price_open=4400.0),
            Row(type=0, volume=0.5, price_open=4500.0),
        ]

        position = _broker(terminal).positions()[0]

        assert position.qty == Decimal("150")  # 150 ounces
        assert position.lots == 150

    def test_the_average_is_weighted_by_size(self) -> None:
        terminal = FakeTerminal()
        terminal.positions = [
            Row(type=0, volume=1.0, price_open=4400.0),
            Row(type=0, volume=0.5, price_open=4500.0),
        ]

        # (100*4400 + 50*4500) / 150
        assert _broker(terminal).positions()[0].average_price == Decimal("4433.33")

    def test_a_short_is_negative(self) -> None:
        terminal = FakeTerminal()
        terminal.positions = [Row(type=1, volume=0.2, price_open=4459.0)]

        assert _broker(terminal).positions()[0].qty == Decimal("-20")

    def test_opposing_tickets_that_cancel_report_no_position(self) -> None:
        terminal = FakeTerminal()
        terminal.positions = [
            Row(type=0, volume=1.0, price_open=4400.0),
            Row(type=1, volume=1.0, price_open=4450.0),
        ]

        assert _broker(terminal).positions() == []

    def test_but_the_hedged_pair_is_still_reported(self) -> None:
        """Netting to zero hides two tickets that both still pay financing."""
        terminal = FakeTerminal()
        terminal.positions = [
            Row(type=0, volume=1.0, price_open=4400.0),
            Row(type=1, volume=1.0, price_open=4450.0),
        ]

        assert _broker(terminal).opposing_tickets() == (1, 1)

    def test_a_flat_account_reports_nothing(self) -> None:
        assert _broker().positions() == []


class TestForeignActivityStaysForeign:
    """The account already carries positions this system did not open."""

    def test_a_deal_without_our_magic_is_ignored(self) -> None:
        terminal = FakeTerminal()
        terminal.deals = [
            Row(
                ticket=1, order=2, magic=0, symbol="XAUUSD", entry=0, type=0,
                volume=0.2, price=4609.0, time=int(NOW.timestamp()),
            )
        ]

        assert _broker(terminal).executions(NOW - timedelta(days=1)) == []

    def test_a_deal_on_another_symbol_is_ignored(self) -> None:
        terminal = FakeTerminal()
        terminal.deals = [
            Row(
                ticket=1, order=2, magic=MAGIC, symbol="FixedVol100", entry=0, type=0,
                volume=0.1, price=4616.66, time=int(NOW.timestamp()),
            )
        ]

        assert _broker(terminal).executions(NOW - timedelta(days=1)) == []

    def test_our_own_deal_comes_back(self) -> None:
        terminal = FakeTerminal()
        terminal.deals = [
            Row(
                ticket=55, order=987654, magic=MAGIC, symbol="XAUUSD", entry=0, type=0,
                volume=1.0, price=4459.1, time=int(NOW.timestamp()),
            )
        ]
        broker = _broker(terminal)
        broker.place(_order("c-1"))

        fills = broker.executions(NOW - timedelta(days=1))

        assert len(fills) == 1
        assert fills[0].client_order_id == "c-1", "matched back through the ledger"
        assert fills[0].lots == 100
        assert fills[0].price == Decimal("4459.1")

    def test_a_deal_we_cannot_match_gets_an_ext_id(self) -> None:
        """Surfaces to reconciliation as unknown-to-us rather than being adopted."""
        terminal = FakeTerminal()
        terminal.deals = [
            Row(
                ticket=55, order=111, magic=MAGIC, symbol="XAUUSD", entry=1, type=1,
                volume=0.5, price=4635.0, time=int(NOW.timestamp()),
            )
        ]

        fills = _broker(terminal).executions(NOW - timedelta(days=1))

        assert fills[0].client_order_id == "ext:111"


class TestConnectionAndFunds:
    def test_funds_come_from_the_account(self) -> None:
        funds = _broker().funds()

        assert funds.cash == Decimal("108805.15")
        assert funds.margin_available == Decimal("108812.76")

    def test_reading_before_attaching_is_refused(self) -> None:
        broker = Mt5Broker(
            terminal=FakeTerminal(),
            symbol="XAUUSD",
            clock=FrozenClock(),
        )

        with pytest.raises(FatalBrokerError, match="not attached"):
            broker.positions()

    def test_a_terminal_with_no_account_is_refused(self) -> None:
        broker = Mt5Broker(
            terminal=FakeTerminal(account=False),
            symbol="XAUUSD",
            clock=FrozenClock(),
        )

        with pytest.raises(FatalBrokerError, match="no account is logged in"):
            broker.connect()

    def test_algo_trading_switched_off_is_refused_at_connect(self) -> None:
        """Better than discovering it when the first order is rejected."""
        broker = Mt5Broker(
            terminal=FakeTerminal(trade_allowed=False),
            symbol="XAUUSD",
            clock=FrozenClock(),
        )

        with pytest.raises(FatalBrokerError, match="trading is not allowed"):
            broker.connect()

    def test_health_reports_the_session(self) -> None:
        broker = _broker()

        assert broker.health().connected is True
        assert "XAUUSD" in broker.health().detail


class TestCancelIsHonestAboutBeingImpossible:
    def test_it_raises_rather_than_pretending(self) -> None:
        """A caller believing it cancelled something would be reasoning about a
        position that is open."""
        with pytest.raises(FatalBrokerError, match="market IOC orders"):
            _broker().cancel("c-1")


class TestNoQuote:
    def test_placing_without_a_tick_is_retryable_not_fatal(self) -> None:
        # A constructor flag rather than a monkeypatched lambda, the same shape
        # as `account` above and as `tests/test_mt5_feed.py`'s own fake - so the
        # fake keeps satisfying `Mt5Trader` instead of being reassigned into a
        # shape the protocol does not describe.
        broker = _broker(FakeTerminal(tick=False))

        with pytest.raises(RetryableBrokerError, match="no quote"):
            broker.place(_order())
