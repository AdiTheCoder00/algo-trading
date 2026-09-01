"""The MetaTrader 5 adapter. Implements `Broker` against a running terminal.

Same contract as the Kotak and paper adapters: snapshots are what the broker
says, never what we believe, and reconciliation decides when the two disagree.
Four things about MT5 shape this one, all found by probing the live Vantage demo
account rather than by reading documentation (D-122).

## The comment field is not a tag

MT5 lets an order carry a `comment`, which looks like the client-order-id slot
the Kotak SDK lacks. It is not: **the terminal overwrites it**. Deals pulled from
the same account carry comments MT5 wrote itself - `[sl 4641.92]`,
`[tp 4635.00]`, `closePosition` - in place of whatever was sent. A client order
id put there survives until the first stop-out and then is gone.

So the same answer as Kotak: the adapter keeps its **own persisted ledger**
mapping our ids to MT5 tickets. `magic` carries a constant identifying this
system's orders, which is what separates ours from the ones already on the
account.

## The account is HEDGING, and the engine nets

`positions_get()` returns an independent ticket per trade; `Portfolio` and
`BrokerPositionSnapshot` assume one signed net position per instrument. Tickets
are therefore aggregated. That is the right arithmetic for exposure and P&L, but
it does lose something real: two opposing tickets net to zero while both still
pay financing and both still hold spread. `opposing_tickets` reports it rather
than letting the netting hide it.

## Sizes are in ounces here and lots there

One engine lot is one troy ounce; MT5 sizes XAUUSD in 100-ounce lots with a 0.01
step (see `algo/exchange/data/spec_xauusd.yaml`). The conversion is
`volume = lots / 100`, it lives only in this file, and it is tested in both
directions - reading a size in the wrong unit is a hundredfold position error.

## Filling is IOC on this symbol

`symbol_info("XAUUSD").filling_mode` reports IOC only. Sending FOK gets the order
rejected, so the mode is read from the symbol rather than assumed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from structlog import get_logger

from algo.core.clock import Clock
from algo.core.enums import OrderState, OrderType, Side
from algo.core.errors import DataError, FatalBrokerError, RetryableBrokerError
from algo.core.instrument import CfdId
from algo.core.order import BrokerOrderRef, Order
from algo.core.timeutil import ensure_utc, iso
from algo.execution.broker import (
    AccountSnapshot,
    BrokerFillSnapshot,
    BrokerHealth,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    Funds,
)

log = get_logger(__name__)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: Ounces in one MT5 lot of XAUUSD. One engine lot is one ounce, so this is the
#: whole of the unit conversion. See the module docstring.
OUNCES_PER_MT5_LOT = Decimal("100")

#: Stamped on every order this system places, so `positions_get` can tell ours
#: from the ones already on the account. Arbitrary but fixed: changing it
#: orphans every open position from the adapter's point of view.
MAGIC = 20260828

#: MT5 deal `entry` values. 0 opens a position, 1 closes one.
_DEAL_ENTRY_IN = 0
_DEAL_ENTRY_OUT = 1

#: MT5 deal/order `type` values for the two market directions.
_TYPE_BUY = 0
_TYPE_SELL = 1

_RETCODE_DONE = 10009
_RETCODE_PLACED = 10008

#: MT5 `account_info().trade_mode`. The only one of these that is not play money
#: is REAL, which is why `account()` reports the distinction rather than leaving
#: it to whoever remembers which terminal is logged in.
_TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}


@runtime_checkable
class Mt5Trader(Protocol):
    """The MetaTrader5 surface this adapter needs, so tests need no terminal."""

    def initialize(self, *args: Any, **kwargs: Any) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple[int, str]: ...
    def account_info(self) -> Any: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def symbol_info_tick(self, symbol: str) -> Any: ...
    def symbol_select(self, symbol: str, enable: bool) -> bool: ...
    def order_send(self, request: dict[str, Any]) -> Any: ...
    def positions_get(self, **kwargs: Any) -> Any: ...
    def orders_get(self, **kwargs: Any) -> Any: ...
    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any: ...


class _PlacedOrder(BaseModel):
    """One order we placed, as the adapter remembers it.

    The bridge between our `client_order_id` and MT5's ticket, persisted because
    MT5 overwrites the comment and so cannot carry the id itself.
    """

    model_config = _FROZEN

    client_order_id: str
    broker_order_id: str
    instrument_key: str
    symbol: str
    side: Side
    lots: int
    qty: Decimal
    state: OrderState
    average_price: Decimal | None = None
    placed_at: datetime
    message: str = ""


def lots_to_volume(lots: int) -> Decimal:
    """Engine lots (ounces) -> MT5 volume (100-ounce lots)."""
    return Decimal(lots) / OUNCES_PER_MT5_LOT


def volume_to_lots(volume: float | Decimal) -> int:
    """MT5 volume -> engine lots. Exact: MT5's 0.01 step is one ounce.

    Raises rather than rounding on a volume finer than the step, because a
    silently rounded size is a silently wrong position.
    """
    ounces = Decimal(str(volume)) * OUNCES_PER_MT5_LOT
    if ounces != ounces.to_integral_value():
        raise DataError(
            f"MT5 reported a volume of {volume}, which is {ounces} ounces and not "
            "a whole number of engine lots. Refusing to round a position size."
        )
    return int(ounces)


class Mt5Broker:
    """Places and reads orders on a running MT5 terminal."""

    __slots__ = ("_clock", "_connected", "_last_heartbeat", "_ledger", "_symbol", "_terminal")

    def __init__(
        self, *, terminal: Mt5Trader, symbol: str, clock: Clock
    ) -> None:
        self._terminal = terminal
        self._symbol = symbol
        self._clock = clock
        self._connected = False
        self._last_heartbeat: datetime | None = None
        self._ledger: dict[str, _PlacedOrder] = {}

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        """Attach to the terminal and make the symbol tradeable.

        MT5 has no login step here - the terminal is already signed in, which is
        the security property worth noting: **no credential passes through this
        process at all.** Unlike the Kotak adapter there is no TOTP seed and no
        MPIN to leak.
        """
        if not self._terminal.initialize():
            raise FatalBrokerError(
                f"could not attach to the MT5 terminal: {self._terminal.last_error()}. "
                "The terminal must be running and logged in."
            )
        account = self._terminal.account_info()
        if account is None:
            raise FatalBrokerError(
                f"attached to MT5 but no account is logged in: {self._terminal.last_error()}"
            )
        if not self._terminal.symbol_select(self._symbol, True):
            raise FatalBrokerError(
                f"{self._symbol} could not be selected in Market Watch: "
                f"{self._terminal.last_error()}"
            )
        if not getattr(account, "trade_allowed", True):
            raise FatalBrokerError(
                "the terminal reports trading is not allowed on this account "
                "(check Algo Trading is enabled in the terminal)."
            )
        self._connected = True
        self._last_heartbeat = self._clock.now()
        log.info(
            "mt5 session attached",
            login=getattr(account, "login", None),
            server=getattr(account, "server", None),
        )

    def disconnect(self) -> None:
        self._connected = False
        self._last_heartbeat = None

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self._connected,
            last_heartbeat=self._last_heartbeat,
            detail=f"mt5 {self._symbol}" if self._connected else "not attached",
        )

    def _require_connection(self, what: str) -> None:
        if not self._connected:
            raise FatalBrokerError(f"cannot {what}: not attached to the MT5 terminal")

    # ------------------------------------------------------------------ send
    def place(self, order: Order) -> BrokerOrderRef:
        """Send one order. Market only; a CFD stop lives with the position."""
        self._require_connection("place an order")
        if order.order_type is not OrderType.MARKET:
            raise FatalBrokerError(
                f"{order.order_type} is not wired for MT5 yet; only MARKET is. "
                "Refusing to translate an order type into something else."
            )
        existing = self._ledger.get(order.client_order_id)
        if existing is not None:
            # At most once, ever. The router enforces this too; the adapter does
            # not rely on that, because a duplicate here is a duplicate position.
            return BrokerOrderRef(
                client_order_id=existing.client_order_id,
                broker_order_id=existing.broker_order_id,
                accepted_at=existing.placed_at,
            )

        tick = self._terminal.symbol_info_tick(self._symbol)
        info = self._terminal.symbol_info(self._symbol)
        if tick is None or info is None:
            raise RetryableBrokerError(
                f"no quote for {self._symbol}: {self._terminal.last_error()}"
            )
        request = {
            "action": getattr(self._terminal, "TRADE_ACTION_DEAL", 1),
            "symbol": self._symbol,
            "volume": float(lots_to_volume(order.lots)),
            "type": _TYPE_BUY if order.side is Side.BUY else _TYPE_SELL,
            "price": float(tick.ask if order.side is Side.BUY else tick.bid),
            "deviation": 20,
            "magic": MAGIC,
            # Sent anyway, because it is useful in the terminal's own UI while it
            # survives. Nothing reads it back - see the module docstring.
            "comment": order.client_order_id[:31],
            "type_time": getattr(self._terminal, "ORDER_TIME_GTC", 0),
            "type_filling": self._filling_mode(info),
        }
        result = self._terminal.order_send(request)
        if result is None:
            raise RetryableBrokerError(
                f"MT5 returned nothing for {order.client_order_id}: "
                f"{self._terminal.last_error()}. Whether it arrived is unknown."
            )
        retcode = int(getattr(result, "retcode", 0))
        if retcode not in (_RETCODE_DONE, _RETCODE_PLACED):
            raise FatalBrokerError(
                f"MT5 rejected {order.client_order_id}: retcode {retcode} "
                f"{getattr(result, 'comment', '')}"
            )
        at = self._clock.now()
        entry = _PlacedOrder(
            client_order_id=order.client_order_id,
            broker_order_id=str(getattr(result, "order", "") or ""),
            instrument_key=order.instrument.key,
            symbol=self._symbol,
            side=order.side,
            lots=order.lots,
            qty=order.qty,
            state=OrderState.FILLED if retcode == _RETCODE_DONE else OrderState.SENT,
            average_price=(
                Decimal(str(result.price)) if getattr(result, "price", None) else None
            ),
            placed_at=at,
            message=str(getattr(result, "comment", "")),
        )
        self._ledger[entry.client_order_id] = entry
        return BrokerOrderRef(
            client_order_id=entry.client_order_id,
            broker_order_id=entry.broker_order_id,
            accepted_at=at,
        )

    def _filling_mode(self, info: Any) -> int:
        """Read from the symbol, not assumed. XAUUSD on this account is IOC only."""
        mask = int(getattr(info, "filling_mode", 0))
        ioc = getattr(self._terminal, "ORDER_FILLING_IOC", 1)
        fok = getattr(self._terminal, "ORDER_FILLING_FOK", 0)
        if mask & 2:
            return int(ioc)
        if mask & 1:
            return int(fok)
        raise FatalBrokerError(
            f"{self._symbol} advertises filling_mode {mask}, which is neither FOK "
            "nor IOC. Refusing to guess how the venue wants to be filled."
        )

    def cancel(self, client_order_id: str) -> None:
        """Nothing to cancel: every order this adapter sends is a market IOC.

        Raising is more honest than a silent no-op - a caller that believes it
        cancelled something would be reasoning about a position that is open.
        """
        raise FatalBrokerError(
            f"cannot cancel {client_order_id}: this adapter sends market IOC orders "
            "only, which are filled or rejected immediately and never rest."
        )

    # -------------------------------------------------------------- snapshots
    def order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        """Answered from the adapter's ledger, because MT5 cannot answer it.

        The comment MT5 would have to echo is overwritten by the terminal, so
        there is nothing on the broker side to match our id against.
        """
        entry = self._ledger.get(client_order_id)
        if entry is None:
            return None
        return BrokerOrderSnapshot(
            client_order_id=entry.client_order_id,
            broker_order_id=entry.broker_order_id,
            instrument_key=entry.instrument_key,
            side=entry.side,
            lots=entry.lots,
            state=entry.state,
            filled_qty=entry.qty if entry.state is OrderState.FILLED else Decimal("0"),
            average_price=entry.average_price,
            message=entry.message,
            updated_at=entry.placed_at,
        )

    def open_orders(self) -> list[BrokerOrderSnapshot]:
        """Pending orders. Empty in normal operation - market IOC never rests."""
        self._require_connection("read open orders")
        raw = self._terminal.orders_get(symbol=self._symbol) or ()
        out: list[BrokerOrderSnapshot] = []
        for row in raw:
            if int(getattr(row, "magic", 0)) != MAGIC:
                continue
            out.append(
                BrokerOrderSnapshot(
                    client_order_id=self._client_id_for(str(row.ticket)),
                    broker_order_id=str(row.ticket),
                    instrument_key=self._instrument_key(),
                    side=Side.BUY if int(row.type) == _TYPE_BUY else Side.SELL,
                    lots=volume_to_lots(row.volume_current),
                    state=OrderState.SENT,
                    updated_at=datetime.fromtimestamp(int(row.time_setup), UTC),
                )
            )
        return out

    def _our_tickets(self, why: str) -> tuple[Any, ...]:
        """Open tickets on this symbol that **we** opened.

        An MT5 account is shared ground. Another EA, or the person at the
        terminal, can hold positions in the same symbol at the same time, and
        `positions_get` reports all of them without distinction. `open_orders`
        and `executions` already filtered on `magic`; these did not, which meant
        a second robot's exposure was read back as ours.

        That is not a cosmetic mismatch. The strategy reads its position from
        the context (D-041), so a foreign long would make it believe it is
        already long: it would decline its own entry, and on the opposing
        breakout it would send a close for a position it does not own. Kill
        switch flatten would do the same, deliberately.

        `magic` is the only thing that separates them - MT5 overwrites the
        comment, which is why the ledger exists at all.
        """
        self._require_connection(why)
        tickets = self._terminal.positions_get(symbol=self._symbol) or ()
        return tuple(t for t in tickets if int(getattr(t, "magic", 0)) == MAGIC)

    def positions(self) -> list[BrokerPositionSnapshot]:
        """Open exposure **we** hold, netted per symbol.

        The account is HEDGING, so MT5 holds a ticket per trade. Netting them is
        the arithmetic the engine expects; `opposing_tickets` exists because
        netting alone would hide two positions that cancel on paper while both
        still pay financing.
        """
        tickets = self._our_tickets("read positions")
        net_ounces = Decimal("0")
        weighted = Decimal("0")
        for row in tickets:
            signed = Decimal(volume_to_lots(row.volume))
            if int(row.type) == _TYPE_SELL:
                signed = -signed
            net_ounces += signed
            weighted += signed * Decimal(str(row.price_open))
        if net_ounces == 0:
            return []
        return [
            BrokerPositionSnapshot(
                instrument_key=self._instrument_key(),
                qty=net_ounces,
                lots=int(net_ounces),
                # Signed-volume-weighted, so it is the average entry of the net
                # exposure rather than of the ticket count.
                average_price=(weighted / net_ounces).quantize(Decimal("0.01")),
            )
        ]

    def opposing_tickets(self) -> tuple[int, int]:
        """(long tickets, short tickets) open right now.

        Netting is what the engine wants and it is also what hides a hedged pair.
        Both sides open at once means real financing on both, so this is exposed
        for the live loop to report rather than left implicit.
        """
        tickets = self._our_tickets("read positions")
        longs = sum(1 for r in tickets if int(r.type) == _TYPE_BUY)
        shorts = sum(1 for r in tickets if int(r.type) == _TYPE_SELL)
        return longs, shorts

    def executions(self, since: datetime) -> list[BrokerFillSnapshot]:
        """Deals since `since`, ours only."""
        self._require_connection("read executions")
        start = ensure_utc(since)
        deals = (
            self._terminal.history_deals_get(start, self._clock.now() + timedelta(days=1))
            or ()
        )
        out: list[BrokerFillSnapshot] = []
        for deal in deals:
            if int(getattr(deal, "magic", 0)) != MAGIC:
                continue
            if getattr(deal, "symbol", "") != self._symbol:
                continue
            if int(getattr(deal, "entry", -1)) not in (_DEAL_ENTRY_IN, _DEAL_ENTRY_OUT):
                continue
            out.append(
                BrokerFillSnapshot(
                    fill_id=str(deal.ticket),
                    client_order_id=self._client_id_for(str(deal.order)),
                    broker_order_id=str(deal.order),
                    instrument_key=self._instrument_key(),
                    side=Side.BUY if int(deal.type) == _TYPE_BUY else Side.SELL,
                    lots=volume_to_lots(deal.volume),
                    qty=Decimal(volume_to_lots(deal.volume)),
                    price=Decimal(str(deal.price)),
                    ts=datetime.fromtimestamp(int(deal.time), UTC),
                )
            )
        return out

    def funds(self) -> Funds:
        self._require_connection("read funds")
        account = self._terminal.account_info()
        if account is None:
            raise RetryableBrokerError(
                f"MT5 returned no account info: {self._terminal.last_error()}"
            )
        return Funds(
            cash=Decimal(str(account.balance)),
            margin_used=Decimal(str(account.margin)),
            margin_available=Decimal(str(account.margin_free)),
        )

    def account(self) -> AccountSnapshot:
        """Everything the terminal knows about the account, for the dashboard.

        Separate from `funds()` on purpose: `funds()` is the router's question
        ("can this order be paid for") and is deliberately three numbers wide.
        This is the operator's question, and answering it with the router's
        model would mean either starving the dashboard or widening a type the
        routing path has to keep simple.

        `open_tickets` counts the whole account, not just this adapter's symbol.
        A margin level is an account-wide fact, and a panel that reported "0
        open" beside a margin level of 300% would be describing two different
        accounts.
        """
        self._require_connection("read the account")
        account = self._terminal.account_info()
        if account is None:
            raise RetryableBrokerError(
                f"MT5 returned no account info: {self._terminal.last_error()}"
            )
        level = Decimal(str(getattr(account, "margin_level", 0) or 0))
        mode = int(getattr(account, "trade_mode", -1))
        return AccountSnapshot(
            login=str(getattr(account, "login", "")),
            server=str(getattr(account, "server", "")),
            currency=str(getattr(account, "currency", "")),
            trade_mode=_TRADE_MODES.get(mode, f"unknown({mode})"),
            # Anything we cannot positively identify as demo is treated as not
            # demo. The failure that matters here is one-directional: calling a
            # real account a demo is how play money becomes real money.
            is_demo=mode in (0, 1),
            leverage=int(getattr(account, "leverage", 0) or 0),
            balance=Decimal(str(account.balance)),
            equity=Decimal(str(account.equity)),
            margin_used=Decimal(str(account.margin)),
            margin_free=Decimal(str(account.margin_free)),
            margin_level=level if level > 0 else None,
            floating_pnl=Decimal(str(getattr(account, "profit", 0) or 0)),
            # Deliberately every ticket on the account, ours or not, and
            # unfiltered by symbol - unlike `positions`. This snapshot
            # describes the *account*: the balance, equity and margin beside it
            # are already account-wide, and a foreign robot's tickets consume
            # the same margin ours do. Filtering here would report a margin
            # level no set of positions explains.
            open_tickets=len(self._terminal.positions_get() or ()),
            trade_allowed=bool(getattr(account, "trade_allowed", False)),
        )

    # ------------------------------------------------------------------ guts
    def _instrument_key(self) -> str:
        return CfdId(symbol=self._symbol).key

    def _client_id_for(self, broker_order_id: str) -> str:
        """Our id for an MT5 ticket, or a synthetic `ext:` one.

        An order placed by hand in the terminal, or by another tool, has no entry
        in our ledger. It gets an `ext:` id so reconciliation surfaces it as
        unknown-to-us rather than silently adopting it - the account already
        carries positions this system did not open.
        """
        for entry in self._ledger.values():
            if entry.broker_order_id == broker_order_id:
                return entry.client_order_id
        return f"ext:{broker_order_id}"

    # ----------------------------------------------------------- persistence
    def save(self, path: Path | str) -> None:
        """Freeze the ledger so a restart can still answer `order_by_client_id`."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"orders": [o.model_dump(mode="json") for o in self._ledger.values()]},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def restore(self, path: Path | str) -> None:
        source = Path(path)
        if not source.exists():
            return
        raw = json.loads(source.read_text(encoding="utf-8"))
        self._ledger = {
            entry["client_order_id"]: _PlacedOrder.model_validate(entry)
            for entry in raw.get("orders", [])
        }

    def __repr__(self) -> str:
        return (
            f"Mt5Broker(symbol={self._symbol}, connected={self._connected}, "
            f"ledger={len(self._ledger)}, "
            f"heartbeat={iso(self._last_heartbeat) if self._last_heartbeat else None})"
        )
