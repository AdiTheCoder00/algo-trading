"""The Kotak Neo adapter. Brief §9, Milestone 7.

Implements the `Broker` protocol (broker.py:106) with the same contract as the
paper adapter: snapshots are what the broker says, never what we believe, and
reconciliation is what decides when the two disagree.

**client_order_id.** The Kotak SDK on PyPI (kotakneoapi 3.0.1) exposes no tag
field on `place_order`, so `order_by_client_id` cannot be answered by the
broker alone. The adapter therefore keeps its **own** persisted ledger mapping
our ids to the broker's order ids (broker.py:9-15 documents this as the open
question; the answer here is "no tag in 3.0.1, so we persist the mapping
ourselves"). When the SDK supports it (main-branch `tag` parameter), the tag is
sent and its `GuiOrdId` echo in the order book is used as a secondary match.
An order placed by anything outside this system gets a synthetic `ext:` id and
surfaces to reconciliation as ORDER_UNKNOWN_TO_US rather than being silently
adopted.

**Empty ack ids.** The place-order ack frequently arrives with an empty
`nOrdNo`. When that happens the adapter reads the order book once and tries to
resolve the id by an unambiguous symbol + side + quantity match; if that fails
it keeps the ledger entry unresolved and lets reconciliation answer
`order_by_client_id` from the next book read. A failed resolution read is never
retried inside `place` — the order is already sent, and retrying would send it
twice.

**Failure semantics.** The SDK returns error payloads and None instead of
raising for most failures. Every call therefore checks the raw response and
turns failures into the two error types the router understands: transient
network failures are `RetryableBrokerError`, everything a broker rejects is
`FatalBrokerError`.

**MCX validity.** The exchange accepts DAY orders only; an IOC order is refused
here with a clear error rather than sent and silently reinterpreted.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from structlog import get_logger

from algo.core.clock import Clock
from algo.core.enums import Exchange, OrderState, OrderType, ProductType, Right, Side, TimeInForce
from algo.core.errors import DataError, FatalBrokerError, RetryableBrokerError
from algo.core.instrument import FutureId, InstrumentId, OptionId
from algo.core.order import BrokerOrderRef, Order
from algo.core.timeutil import IST, ensure_utc, iso
from algo.exchange.master import InstrumentMaster, MasterRow
from algo.execution.broker import (
    BrokerFillSnapshot,
    BrokerHealth,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    Funds,
)

log = get_logger(__name__)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: Kotak order-book status values -> our lifecycle states.
_STATUS_TO_STATE: dict[str, OrderState] = {
    "open": OrderState.SENT,
    "new": OrderState.SENT,
    "pending": OrderState.SENT,
    "trigger pending": OrderState.SENT,
    "trigger_pending": OrderState.SENT,
    "complete": OrderState.FILLED,
    "cancelled": OrderState.CANCELLED,
    "canceled": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}

#: The SDK's order types are nearly ours; only the stop variants differ.
_ORDER_TYPE_TO_API: dict[OrderType, str] = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "L",
    OrderType.STOP_MARKET: "SL-M",
    OrderType.STOP_LIMIT: "SL",
}

_PRODUCT_TO_API: dict[ProductType, str] = {
    ProductType.NRML: "NRML",
    ProductType.INTRADAY: "MIS",
}

#: The Kotak MCX segment code for the engine's Exchange.MCX.
_EXCHANGE_SEGMENT = "mcx_fo"


class KotakCredentials(BaseModel):
    """The values a Kotak Neo session needs. From env, never config.

    `market_data_key` is optional: Kotak issues the script-details endpoints
    (scrip master, quotes) under a separate Market Data API app, whose consumer
    key can differ from the trading app's. When unset, the trading key is used
    for market data too.
    """

    model_config = _FROZEN

    consumer_key: str
    mobile_number: str
    ucc: str
    totp_seed: str
    mpin: str
    market_data_key: str = ""

    def has_all(self) -> bool:
        return all(
            (
                self.consumer_key,
                self.mobile_number,
                self.ucc,
                self.totp_seed,
                self.mpin,
            )
        )

    def missing(self) -> tuple[str, ...]:
        """Which required credentials are absent, by env-var name."""
        required = {
            "ALGO_KOTAK_CONSUMER_KEY": self.consumer_key,
            "ALGO_KOTAK_MOBILE_NUMBER": self.mobile_number,
            "ALGO_KOTAK_UCC": self.ucc,
            "ALGO_KOTAK_TOTP_SEED": self.totp_seed,
            "ALGO_KOTAK_MPIN": self.mpin,
        }
        return tuple(name for name, value in required.items() if not value)


def credentials_from_env(env: dict[str, str] | None = None) -> KotakCredentials:
    """Read `ALGO_KOTAK_*` from the environment.

    Deliberately separate from the hashed config (schema.py): credentials must
    never flow into `config_hash`, which is stamped into every signal id and run
    artefact.
    """
    import os

    source = os.environ if env is None else env

    def pick(name: str) -> str:
        return source.get(f"ALGO_KOTAK_{name}", "")

    return KotakCredentials(
        consumer_key=pick("CONSUMER_KEY"),
        mobile_number=pick("MOBILE_NUMBER"),
        ucc=pick("UCC"),
        totp_seed=pick("TOTP_SEED"),
        mpin=pick("MPIN"),
        market_data_key=pick("MARKET_DATA_KEY"),
    )


@runtime_checkable
class KotakTransport(Protocol):
    """The SDK surface this adapter needs. Fakes implement the same protocol.

    Every method returns the raw response payloads; the adapter maps them to
    snapshots and decides what is an error. The network lives behind this
    boundary so the whole reconciliation suite runs against a fake.
    """

    def totp_login(self, mobile_number: str, ucc: str, totp: str) -> dict[str, Any]: ...
    def totp_validate(self, mpin: str) -> dict[str, Any]: ...
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
    ) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    def order_report(self, order_id: str = "") -> dict[str, Any]: ...
    def trade_report(self) -> dict[str, Any]: ...
    def positions(self) -> dict[str, Any]: ...
    def limits(self) -> dict[str, Any]: ...
    def supports_tag(self) -> bool: ...


class NeoTransport:
    """The real transport: wraps `NeoAPI` from the official Kotak SDK."""

    __slots__ = ("_api", "_tag_supported")

    def __init__(self, consumer_key: str) -> None:
        import inspect

        # The SDK ships py.typed without stubs and mypy's analysis of it does
        # not see its lazy exports, so the import below is ignored explicitly.
        from neo_api_client import NeoAPI  # type: ignore[attr-defined]

        self._api = NeoAPI(consumer_key=consumer_key, environment="prod")
        self._tag_supported = "tag" in inspect.signature(NeoAPI.place_order).parameters

    def totp_login(self, mobile_number: str, ucc: str, totp: str) -> dict[str, Any]:
        response = self._api.totp_login(
            mobile_number=mobile_number, ucc=ucc, totp=totp
        )
        return {str(k): v for k, v in response.items()}

    def totp_validate(self, mpin: str) -> dict[str, Any]:
        response = self._api.totp_validate(mpin=mpin)
        return {str(k): v for k, v in response.items()}

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
        kwargs: dict[str, str] = {
            "exchange_segment": exchange_segment,
            "product": product,
            "price": price,
            "order_type": order_type,
            "quantity": quantity,
            "validity": validity,
            "trading_symbol": trading_symbol,
            "transaction_type": transaction_type,
            "trigger_price": trigger_price,
        }
        if self._tag_supported and tag is not None:
            kwargs["tag"] = tag
        response = self._api.place_order(**kwargs)
        return {str(k): v for k, v in response.items()}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        response = self._api.cancel_order(order_id)
        return {str(k): v for k, v in response.items()}

    def order_report(self, order_id: str = "") -> dict[str, Any]:
        response = self._api.order_report(order_id=order_id or None)
        return {str(k): v for k, v in response.items()}

    def trade_report(self) -> dict[str, Any]:
        response = self._api.trade_report()
        return {str(k): v for k, v in response.items()}

    def positions(self) -> dict[str, Any]:
        response = self._api.positions()
        return {str(k): v for k, v in response.items()}

    def limits(self) -> dict[str, Any]:
        response = self._api.limits()
        return {str(k): v for k, v in response.items()}

    def supports_tag(self) -> bool:
        return self._tag_supported


class _PlacedOrder(BaseModel):
    """One order we placed, as the adapter remembers it.

    The bridge between our client_order_id and the broker's id. Persisted so a
    restart can still answer `order_by_client_id` — the whole point of the
    adapter-level ledger (broker.py:9-15). `broker_order_id` may be empty when
    the ack carried no `nOrdNo` and the follow-up book read could not resolve
    it; `order_by_client_id` then falls back to matching the order itself.
    """

    model_config = _FROZEN

    client_order_id: str
    broker_order_id: str
    instrument_key: str
    symboltoken: str
    side: Side
    lots: int
    qty: Decimal
    placed_at: datetime


TotpFn = Callable[[], str]


def default_totp(seed: str) -> TotpFn:
    """A TOTP generator from the seed. Wrapped in a callable so tests can stub it."""
    import pyotp

    totp = pyotp.TOTP(seed)

    def _now() -> str:
        return totp.now()

    return _now


class KotakBroker:
    """`Broker` protocol implementation over Kotak Neo."""

    __slots__ = (
        "_clock",
        "_connected",
        "_credentials",
        "_exchange",
        "_last_heartbeat",
        "_ledger",
        "_master",
        "_totp",
        "_transport",
    )

    def __init__(
        self,
        *,
        transport: KotakTransport,
        master: InstrumentMaster,
        credentials: KotakCredentials,
        clock: Clock,
        totp: TotpFn | None = None,
        exchange: Exchange = Exchange.MCX,
    ) -> None:
        if not credentials.has_all():
            raise FatalBrokerError(
                "Kotak credentials are incomplete. Set ALGO_KOTAK_CONSUMER_KEY, "
                "ALGO_KOTAK_MOBILE_NUMBER, ALGO_KOTAK_UCC, ALGO_KOTAK_TOTP_SEED "
                "and ALGO_KOTAK_MPIN (in .env or the environment) — a session "
                "cannot be established without all five"
            )
        self._transport = transport
        self._master = master
        self._credentials = credentials
        self._clock = clock
        self._totp = totp or default_totp(credentials.totp_seed)
        self._exchange = exchange
        self._connected = False
        self._last_heartbeat: datetime | None = None
        self._ledger: dict[str, _PlacedOrder] = {}

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        """Establish the trade session: TOTP login, then MPIN validation.

        The SDK cannot restore a trade session from an access token alone (the
        trading endpoints require `edit_token`/`edit_sid`, which only the
        two-step login sets), so every connect is a full login from the seed.

        Rejections here are `FatalBrokerError`: a wrong TOTP seed or MPIN will
        not fix itself on retry, and the router must not hammer the login
        endpoint. Only network failures are retryable.
        """
        try:
            login = self._transport.totp_login(
                self._credentials.mobile_number,
                self._credentials.ucc,
                self._totp(),
            )
            validate = self._transport.totp_validate(self._credentials.mpin)
        except (RetryableBrokerError, FatalBrokerError):
            raise
        except Exception as exc:
            raise RetryableBrokerError(f"totp login failed: {exc}") from exc
        if not _ack_ok(login):
            raise FatalBrokerError(f"Kotak rejected the TOTP login: {_describe(login)}")
        if not _ack_ok(validate):
            raise FatalBrokerError(f"Kotak rejected the MPIN: {_describe(validate)}")
        self._connected = True
        self._last_heartbeat = self._clock.now()
        log.info(
            "kotak session established",
            ucc=self._credentials.ucc,
        )

    def disconnect(self) -> None:
        self._connected = False
        self._last_heartbeat = None

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self._connected,
            last_heartbeat=self._last_heartbeat,
            detail=(
                f"kotak trade session for {self._credentials.ucc}"
                if self._connected
                else "disconnected"
            ),
        )

    def _require_connection(self, action: str) -> None:
        if not self._connected:
            raise RetryableBrokerError(
                f"cannot {action}: the Kotak trade session is not established. "
                "Reconnect before doing anything else, then reconcile."
            )

    def _beat(self) -> None:
        self._last_heartbeat = self._clock.now()

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        """Run one transport call, mapping every failure the SDK can throw.

        The SDK raises untyped network exceptions and returns error payloads for
        business failures. This is the single boundary where "the network
        failed" becomes `RetryableBrokerError` — the only kind the router may
        retry — and nothing else escapes it. Error payload shapes:
        `{"Error Message": ...}` means the session is gone; `{"Error": <exc>}`
        is a validation rejection when the exception is a value error, a
        network failure otherwise; `{"error": <exc>}` is a network failure;
        `{"error": [{"code", "message"}, ...]}` is a business rejection.
        """
        try:
            response = fn()
        except (RetryableBrokerError, FatalBrokerError):
            raise
        except Exception as exc:
            raise RetryableBrokerError(f"{what} failed: {exc}") from exc
        if response is None:
            raise RetryableBrokerError(f"{what} returned nothing")
        if not isinstance(response, dict):
            return response
        if "Error Message" in response:
            raise RetryableBrokerError(
                f"{what} failed: the trade session is gone "
                f"({response.get('Error Message')}); reconnect and reconcile"
            )
        if "Error" in response:
            error = response["Error"]
            if isinstance(error, Exception):
                if isinstance(error, (ValueError, TypeError)):
                    raise FatalBrokerError(f"{what} rejected: {error}")
                raise RetryableBrokerError(f"{what} failed: {error}")
            raise FatalBrokerError(f"{what} rejected: {error}")
        if "error" in response:
            error = response["error"]
            if isinstance(error, list) and error and isinstance(error[0], dict):
                details = "; ".join(
                    str(e.get("message") or e.get("code") or e) for e in error
                )
                raise FatalBrokerError(f"{what} rejected: {details}")
            if isinstance(error, Exception):
                raise RetryableBrokerError(f"{what} failed: {error}")
            raise FatalBrokerError(f"{what} rejected: {error}")
        return response

    # ----------------------------------------------------------------- orders
    def place(self, order: Order) -> BrokerOrderRef:
        self._require_connection("place an order")
        if order.client_order_id in self._ledger:
            raise FatalBrokerError(
                f"duplicate client order id {order.client_order_id}; this adapter "
                "already sent an order with that id"
            )
        if order.tif is TimeInForce.IOC:
            raise FatalBrokerError(
                f"refusing to place {order.client_order_id}: Kotak MCX does not "
                "support IOC orders; only DAY validity is accepted"
            )

        try:
            row = self._master.row_for(order.instrument)
        except DataError as exc:
            raise FatalBrokerError(str(exc)) from exc

        params = self._order_params(order, row)
        response = self._call(
            "place order",
            lambda: self._transport.place_order(**params),
        )
        if not _ack_ok(response):
            raise FatalBrokerError(
                f"Kotak rejected the order: {_describe(response)}"
            )
        broker_order_id = str(response.get("nOrdNo") or "")

        now = self._clock.now()
        placed = _PlacedOrder(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            instrument_key=order.instrument.key,
            symboltoken=row.symboltoken,
            side=order.side,
            lots=order.lots,
            qty=order.qty,
            placed_at=now,
        )
        self._ledger[order.client_order_id] = placed
        self._beat()

        if not broker_order_id:
            resolved = self._resolve_nordno(row, placed)
            if resolved:
                self._ledger[order.client_order_id] = placed.model_copy(
                    update={"broker_order_id": resolved}
                )
                broker_order_id = resolved

        return BrokerOrderRef(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            accepted_at=now,
        )

    def _order_params(self, order: Order, row: MasterRow) -> dict[str, str]:
        params: dict[str, str] = {
            "exchange_segment": _EXCHANGE_SEGMENT,
            "product": _PRODUCT_TO_API[order.product],
            "price": str(order.limit_price) if order.limit_price is not None else "0",
            "order_type": _ORDER_TYPE_TO_API[order.order_type],
            "quantity": str(int(order.qty)),
            "validity": order.tif.value,
            "trading_symbol": row.tradingsymbol,
            "transaction_type": order.side.value,
            "trigger_price": (
                str(order.trigger_price) if order.trigger_price is not None else "0"
            ),
        }
        if self._transport.supports_tag():
            params["tag"] = order.client_order_id
        return params

    def _resolve_nordno(
        self, row: MasterRow, placed: _PlacedOrder
    ) -> str:
        """One book read to learn the order's nOrdNo when the ack left it empty.

        The match is symbol + side + quantity, made unambiguous by a recency
        guard around the place instant. Failure of this read is logged and the
        order left unresolved — the order is already sent, and raising here
        would make the router send it again.
        """
        try:
            book = _ok_data(
                self._call("read order report", lambda: self._transport.order_report()),
                "order report",
            )
        except RetryableBrokerError as exc:
            log.warning(
                "order placed but id resolution failed; reconciliation will "
                "resolve it from the next book read",
                client_order_id=placed.client_order_id,
                error=str(exc),
            )
            return ""

        cutoff = placed.placed_at - timedelta(minutes=2)
        matches = [
            entry
            for entry in book
            if str(entry.get("trdSym", "")) == row.tradingsymbol
            and _side_of(entry.get("trnsTp")) is placed.side
            and (_decimal(entry.get("qty")) == placed.qty)
            and (
                _parse_book_time(entry.get("exCfmTm") or entry.get("hsUpTm"))
                or placed.placed_at
            )
            >= cutoff
        ]
        if len(matches) == 1:
            return str(matches[0].get("nOrdNo") or "")
        if len(matches) > 1:
            raise FatalBrokerError(
                f"cannot resolve the broker id of {placed.client_order_id}: "
                f"{len(matches)} recent {row.tradingsymbol} {placed.side.value} "
                f"orders match its size; refusing to guess which one we sent"
            )
        return ""

    def cancel(self, client_order_id: str) -> None:
        self._require_connection("cancel an order")
        placed = self._ledger.get(client_order_id)
        if placed is None:
            raise FatalBrokerError(
                f"cannot cancel {client_order_id}: this adapter has no record of "
                "placing it, so it has no broker order id to cancel"
            )
        if not placed.broker_order_id:
            raise FatalBrokerError(
                f"cannot cancel {client_order_id}: we never learned the broker "
                "order id for it (the ack carried none). Reconcile first, then "
                "cancel the order by hand"
            )
        response = self._call(
            "cancel order", lambda: self._transport.cancel_order(placed.broker_order_id)
        )
        if not _ack_ok(response):
            raise FatalBrokerError(
                f"Kotak rejected the cancellation of {placed.broker_order_id}: "
                f"{_describe(response)}"
            )
        self._beat()

    # ------------------------------------------------------------------ reads
    def open_orders(self) -> list[BrokerOrderSnapshot]:
        self._require_connection("read open orders")
        book = _ok_data(
            self._call("read order report", lambda: self._transport.order_report()),
            "order report",
        )
        snapshots = [
            self._snapshot_from_book_entry(entry)
            for entry in book
            if _STATUS_TO_STATE.get(str(entry.get("ordSt") or entry.get("stat", "")).lower())
            not in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)
        ]
        self._beat()
        return snapshots

    def order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        self._require_connection("look up an order")
        placed = self._ledger.get(client_order_id)
        if placed is None:
            return None
        book = _ok_data(
            self._call("read order report", lambda: self._transport.order_report()),
            "order report",
        )
        entry = self._match_book_entry(placed, book)
        self._beat()
        if entry is None:
            return None
        return self._snapshot_from_book_entry(entry)

    def _match_book_entry(
        self, placed: _PlacedOrder, book: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Find the book row that is `placed`.

        Tried in order: the broker id we learned; the `GuiOrdId` echo (when the
        SDK supports tags); and, for ledger entries that never learned a broker
        id, an unambiguous symbol + side + quantity match with a recency guard.
        An ambiguous fallback raises — guessing which of two identical orders is
        ours is exactly the error reconciliation exists to catch.
        """
        for entry in book:
            if placed.broker_order_id and str(entry.get("nOrdNo", "")) == placed.broker_order_id:
                return entry
            if str(entry.get("GuiOrdId", "")) == placed.client_order_id:
                return entry
        if placed.broker_order_id:
            return None
        cutoff = placed.placed_at - timedelta(minutes=2)
        matches = [
            entry
            for entry in book
            if str(entry.get("trdSym", "")) == self._row_symbol(placed)
            and _side_of(entry.get("trnsTp")) is placed.side
            and (_decimal(entry.get("qty")) == placed.qty)
            and (
                _parse_book_time(entry.get("exCfmTm") or entry.get("hsUpTm"))
                or placed.placed_at
            )
            >= cutoff
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FatalBrokerError(
                f"cannot resolve the broker id of {placed.client_order_id}: "
                f"{len(matches)} recent {self._row_symbol(placed)} {placed.side.value} "
                "orders match its size; refusing to guess which one we sent"
            )
        return None

    def _row_symbol(self, placed: _PlacedOrder) -> str:
        row = self._master.row_by_token(placed.symboltoken)
        return row.tradingsymbol if row is not None else ""

    def executions(self, since: datetime) -> list[BrokerFillSnapshot]:
        self._require_connection("read executions")
        cutoff = ensure_utc(since)
        fills: list[BrokerFillSnapshot] = []
        trade_book = _ok_data(
            self._call("read trade report", lambda: self._transport.trade_report()),
            "trade report",
        )
        for entry in trade_book:
            filled_at = _parse_book_time(entry.get("exTm") or entry.get("hsUpTm"))
            if filled_at is None or filled_at < cutoff:
                continue
            order_id = str(entry.get("nOrdNo") or entry.get("exOrdId") or "")
            fill_id = str(entry.get("flId") or f"{order_id}:{entry.get('exTm')}")
            symboltoken = str(entry.get("tok", ""))
            tradingsymbol = str(entry.get("trdSym", ""))
            instrument_key = self._instrument_key_for(symboltoken, tradingsymbol)
            side = _side_of(entry.get("trnsTp"))
            qty = _decimal(entry.get("fldQty"))
            price = _decimal(entry.get("avgPrc"))
            if instrument_key is None or side is None or qty is None or price is None:
                log.warning("dropping unreadable trade-report row", entry=entry)
                continue
            fills.append(
                BrokerFillSnapshot(
                    fill_id=fill_id,
                    client_order_id=self._client_id_for(entry, order_id),
                    broker_order_id=order_id,
                    instrument_key=instrument_key,
                    side=side,
                    lots=self._lots_for(symboltoken, qty),
                    qty=qty,
                    price=price,
                    ts=filled_at,
                )
            )
        fills.sort(key=lambda f: f.ts)
        self._beat()
        return fills

    def positions(self) -> list[BrokerPositionSnapshot]:
        self._require_connection("read positions")
        positions: list[BrokerPositionSnapshot] = []
        position_book = _ok_data(
            self._call("read positions", lambda: self._transport.positions()),
            "position book",
        )
        for entry in position_book:
            symboltoken = str(entry.get("tok", ""))
            tradingsymbol = str(entry.get("trdSym", ""))
            instrument_key = self._instrument_key_for(symboltoken, tradingsymbol)
            qty = _net_qty(entry)
            average = _position_average(entry)
            if instrument_key is None or qty is None or qty == 0 or average is None:
                log.warning("dropping unreadable position row", entry=entry)
                continue
            positions.append(
                BrokerPositionSnapshot(
                    instrument_key=instrument_key,
                    qty=qty,
                    lots=self._lots_for(symboltoken, abs(qty)) * (-1 if qty < 0 else 1),
                    average_price=average,
                )
            )
        self._beat()
        return positions

    def funds(self) -> Funds:
        self._require_connection("read funds")
        data = self._call("read funds", self._transport.limits)
        if not isinstance(data, dict) or not _ack_ok(data):
            raise FatalBrokerError(f"Kotak rejected the funds read: {_describe(data)}")
        cash = _decimal(data.get("Net")) or Decimal("0")
        margin_used = _decimal(data.get("MarginUsed")) or Decimal("0")
        return Funds(
            cash=cash,
            margin_used=margin_used,
            margin_available=max(cash - margin_used, Decimal("0")),
        )

    # ------------------------------------------------------------- snapshots
    def _snapshot_from_book_entry(self, entry: dict[str, Any]) -> BrokerOrderSnapshot:
        order_id = str(entry.get("nOrdNo") or entry.get("exOrdId") or "")
        symboltoken = str(entry.get("tok", ""))
        instrument_key = self._instrument_key_for(
            symboltoken, str(entry.get("trdSym", ""))
        )
        side = _side_of(entry.get("trnsTp"))
        state = _STATUS_TO_STATE.get(str(entry.get("ordSt") or entry.get("stat", "")).lower())
        if instrument_key is None or side is None or state is None:
            raise FatalBrokerError(
                f"order book row {entry} cannot be mapped to a snapshot; "
                "refusing to guess what the broker meant"
            )
        qty = _decimal(entry.get("qty")) or Decimal("0")
        filled = _decimal(entry.get("fldQty")) or Decimal("0")
        average = _decimal(entry.get("avgPrc"))
        return BrokerOrderSnapshot(
            client_order_id=self._client_id_for(entry, order_id),
            broker_order_id=order_id,
            instrument_key=instrument_key,
            side=side,
            lots=self._lots_for(symboltoken, qty),
            state=state,
            filled_qty=filled,
            average_price=average if average is not None else None,
            message=str(entry.get("rejRsn") or entry.get("ordSt") or entry.get("stat") or ""),
            updated_at=_parse_book_time(entry.get("hsUpTm") or entry.get("exCfmTm"))
            or self._clock.now(),
        )

    # -------------------------------------------------------------- identity
    def _client_id_for(self, entry: dict[str, Any], order_id: str) -> str:
        """Our id for a broker order, or a synthetic one if we never placed it.

        The synthetic `ext:` id makes an order placed outside this system surface
        to reconciliation as ORDER_UNKNOWN_TO_US instead of vanishing.
        """
        for placed in self._ledger.values():
            if placed.broker_order_id and placed.broker_order_id == order_id:
                return placed.client_order_id
        gui = str(entry.get("GuiOrdId") or "")
        for placed in self._ledger.values():
            if gui and placed.client_order_id == gui:
                return placed.client_order_id
        return f"ext:{order_id or gui or 'unknown'}"

    def _instrument_key_for(self, symboltoken: str, tradingsymbol: str) -> str | None:
        row = self._master.row_by_token(symboltoken)
        if row is None and tradingsymbol:
            row = self._master.row_by_symbol(tradingsymbol)
        if row is None:
            log.warning(
                "broker row unknown to master snapshot",
                symboltoken=symboltoken,
                tradingsymbol=tradingsymbol,
            )
            return None
        exchange = self._exchange
        if row.instrumenttype.startswith("OPT"):
            right = Right.CE if tradingsymbol.upper().endswith("CE") else Right.PE
            if row.strike is None or row.expiry is None:
                return None
            instrument: InstrumentId = OptionId(
                underlying_future=FutureId(
                    underlying=row.name, expiry=row.expiry, exchange=exchange
                ),
                option_expiry=row.expiry,
                strike=row.strike,
                right=right,
                exchange=exchange,
            )
        else:
            if row.expiry is None:
                return None
            instrument = FutureId(underlying=row.name, expiry=row.expiry, exchange=exchange)
        return instrument.key

    def _lots_for(self, symboltoken: str, qty: Decimal) -> int:
        """Units -> lots, via the master's lot size. Falls back to 1:1 units."""
        row = self._master.row_by_token(symboltoken)
        if row is not None and row.lot_size:
            return max(int(qty / row.lot_size), 0)
        return int(qty)

    # ------------------------------------------------- persistence for restarts
    def save(self, path: Path | str) -> None:
        """Freeze the adapter's ledger so a restart can still answer
        `order_by_client_id`. The broker itself is the other half of the books;
        this is the half that survives our crash."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "orders": [o.model_dump(mode="json") for o in self._ledger.values()],
                },
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
            f"KotakBroker(connected={self._connected}, "
            f"ledger={len(self._ledger)}, "
            f"heartbeat={iso(self._last_heartbeat) if self._last_heartbeat else 'never'})"
        )


def _ok_data(response: dict[str, Any], what: str) -> list[dict[str, Any]]:
    """Pull the `data` list out of a Kotak response, or raise mapping the error.

    The successful envelope is `{"stat": "Ok", "stCode": 200, "data": [...]}`
    (`stat` may be lowercased). An error envelope names its failure and becomes
    `FatalBrokerError`; an unreadable payload is retryable — the network may
    have truncated it.
    """
    if not isinstance(response, dict):
        raise RetryableBrokerError(
            f"Kotak {what} call returned an unreadable payload: {response!r}"
        )
    if not _ack_ok(response):
        raise FatalBrokerError(f"Kotak {what} call failed: {_describe(response)}")
    data = response.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _ack_ok(response: Any) -> bool:
    """True when a response carries a success envelope (stat Ok / stCode 200)."""
    if not isinstance(response, dict):
        return False
    if str(response.get("stat", "")).lower() == "ok":
        return True
    return int(str(response.get("stCode", "0"))) == 200


def _describe(response: Any) -> str:
    """A one-line account of a failed payload, for error messages."""
    if not isinstance(response, dict):
        return repr(response)
    for key in ("Error Message", "error", "Error", "errMsg"):
        value = response.get(key)
        if value:
            if isinstance(value, Exception):
                return str(value)
            if isinstance(value, list):
                return "; ".join(
                    str(e.get("message") or e.get("code") or e)
                    for e in value
                    if isinstance(e, dict)
                ) or str(value)
            return str(value)
    return str(response)


def _side_of(value: object) -> Side | None:
    text = str(value).upper()
    if text in ("B", "BUY"):
        return Side.BUY
    if text in ("S", "SELL"):
        return Side.SELL
    return None


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _net_qty(entry: dict[str, Any]) -> Decimal | None:
    """Position quantity, signed (negative = short). Total buy minus total sell,
    where totals include carry-forward (cf) and intraday (fl) legs."""
    buy = (_decimal(entry.get("cfBuyQty")) or Decimal("0")) + (
        _decimal(entry.get("flBuyQty")) or Decimal("0")
    )
    sell = (_decimal(entry.get("cfSellQty")) or Decimal("0")) + (
        _decimal(entry.get("flSellQty")) or Decimal("0")
    )
    return buy - sell


def _position_average(entry: dict[str, Any]) -> Decimal | None:
    """Average price per the documented Kotak formula.

    Buy Avg = (cfBuyAmt + buyAmt) / (TotalBuyQty x multiplier x genNum/genDen
    x prcNum/prcDen); Sell Avg likewise; the average is the buy side's when
    buy quantity exceeds sell quantity, the sell side's otherwise.
    """
    multiplier = _decimal(entry.get("multiplier"))
    gen = _ratio(entry.get("genNum"), entry.get("genDen"))
    prc = _ratio(entry.get("prcNum"), entry.get("prcDen"))
    if multiplier is None or gen is None or prc is None or multiplier * gen * prc == 0:
        return None
    scale = multiplier * gen * prc
    buy_qty = (_decimal(entry.get("cfBuyQty")) or Decimal("0")) + (
        _decimal(entry.get("flBuyQty")) or Decimal("0")
    )
    sell_qty = (_decimal(entry.get("cfSellQty")) or Decimal("0")) + (
        _decimal(entry.get("flSellQty")) or Decimal("0")
    )
    buy_amt = (_decimal(entry.get("cfBuyAmt")) or Decimal("0")) + (
        _decimal(entry.get("buyAmt")) or Decimal("0")
    )
    sell_amt = (_decimal(entry.get("cfSellAmt")) or Decimal("0")) + (
        _decimal(entry.get("sellAmt")) or Decimal("0")
    )
    buy_avg = buy_amt / (buy_qty * scale) if buy_qty > 0 and scale else None
    sell_avg = sell_amt / (sell_qty * scale) if sell_qty > 0 and scale else None
    if buy_qty > sell_qty:
        return buy_avg
    if sell_qty > buy_qty:
        return sell_avg
    return buy_avg or sell_avg


def _ratio(num: object, den: object) -> Decimal | None:
    numerator = _decimal(num)
    denominator = _decimal(den)
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _parse_book_time(value: object) -> datetime | None:
    """Kotak timestamps are IST wall-clock strings in either
    `%Y/%m/%d %H:%M:%S` (hsUpTm) or `%d-%b-%Y %H:%M:%S` (exTm, exCfmTm).
    Return aware UTC, or None when unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%d-%b-%Y %H:%M:%S"):
        try:
            # No timezone in the format; IST is attached deliberately below.
            naive = datetime.strptime(value.strip(), fmt)  # noqa: DTZ007
        except ValueError:
            continue
        return naive.replace(tzinfo=IST).astimezone(UTC)
    return None
