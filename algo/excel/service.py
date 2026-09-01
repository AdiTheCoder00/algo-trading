"""The refresh loop: one tick reads the sheet, calls Kotak, and writes back.

Four independent sections — quotes, chain, portfolio, orders — run in that order
and each is wrapped on its own. A market-data hiccup must not stop the order sheet
from being drained, and a broker that has not been connected must not blank the
quotes an operator is watching. Whatever failed is reported in `Status!LAST_ERROR`
rather than raised, because a bridge that exits on the first bad tick is a bridge
that is never running when it matters.

**Quotes need no trade session; everything else does.** `quotes()` authenticates on
the consumer key alone, so a bridge started without credentials still shows live
prices and simply reports NOT CONNECTED for positions and orders. That split is
already in the codebase — `algo/data/kotak_feed.py` and `algo/execution/kotak.py`
are deliberately separate transports — and the sheet inherits it.

**The order section runs last** so that a row armed against a price the operator
just read has the freshest possible view of the book above it when it fires.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from structlog import get_logger

from algo.core.clock import Clock
from algo.core.enums import Exchange, Mode
from algo.core.errors import AlgoError, DataError
from algo.core.order import BrokerOrderRef, Order
from algo.core.quote import Quote
from algo.data.kotak_feed import MAX_TOKENS_PER_CALL, KotakChainFeed, _QuoteBuilder
from algo.excel import layout, orders, sync
from algo.excel.io import SheetIO
from algo.exchange.master import InstrumentMaster, MasterRow
from algo.execution.broker import BrokerHealth, BrokerPositionSnapshot, Funds

# `_QuoteBuilder` is imported from the feed module rather than reimplemented on
# purpose: it is the one place that knows how tolerantly a Kotak quote payload has
# to be read (nested depth, fallback keys, unreadable numbers left as None). A
# second copy in this package is exactly how the two would drift apart.

log = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")

#: The Kotak Neo segment every MCX futures-and-options token lives in.
_SEGMENT = "mcx_fo"


@runtime_checkable
class BridgeBroker(Protocol):
    """The four methods the workbook needs from a trade session.

    Narrower than `algo.execution.broker.Broker` on purpose: the bridge never
    cancels, reconciles or reads executions, and a protocol that named those
    would force every fake to implement behaviour no sheet can reach.
    """

    def place(self, order: Order) -> BrokerOrderRef: ...
    def positions(self) -> list[BrokerPositionSnapshot]: ...
    def funds(self) -> Funds: ...
    def health(self) -> BrokerHealth: ...


class ExcelBridge:
    """One workbook, wired to one Kotak account."""

    __slots__ = (
        "_broker",
        "_chain_feed_for",
        "_clock",
        "_exchange",
        "_io",
        "_last_error",
        "_master",
        "_mode",
        "_quote_builder",
        "_quotes",
    )

    def __init__(
        self,
        *,
        io: SheetIO,
        master: InstrumentMaster,
        quotes: Any,
        clock: Clock,
        mode: Mode,
        broker: BridgeBroker | None = None,
        chain_feed_for: Callable[[str], KotakChainFeed] | None = None,
        exchange: Exchange = Exchange.MCX,
    ) -> None:
        self._io = io
        self._master = master
        self._quotes = quotes
        self._clock = clock
        self._mode = mode
        self._broker = broker
        # A factory rather than a feed, because `KotakChainFeed` fixes its
        # underlying at construction while the operator picks it in the sheet and
        # may change it between ticks. Building one per refresh costs nothing —
        # the feed holds no connection, only the transport it was handed.
        self._chain_feed_for = chain_feed_for
        self._exchange = exchange
        self._quote_builder = _QuoteBuilder(clock)
        self._last_error = ""

    # ------------------------------------------------------------------ tick
    def refresh(self) -> None:
        """One full pass. Never raises: every section reports into LAST_ERROR."""
        self._last_error = ""
        marks: dict[str, Decimal] = {}

        try:
            marks = self._refresh_quotes()
        except Exception as exc:  # noqa: BLE001 - one bad section must not end the run
            self._note("quotes", exc)
        try:
            self._refresh_chain()
        except Exception as exc:  # noqa: BLE001
            self._note("chain", exc)
        try:
            self._refresh_portfolio(marks)
        except Exception as exc:  # noqa: BLE001
            self._note("portfolio", exc)
        try:
            self._drain_orders()
        except Exception as exc:  # noqa: BLE001
            self._note("orders", exc)

        self._write_status()

    def _note(self, section: str, exc: Exception) -> None:
        message = f"{section}: {exc}"
        # Appended rather than replaced: two sections failing for different
        # reasons is the case where seeing only the last one misleads.
        self._last_error = f"{self._last_error} | {message}" if self._last_error else message
        log.warning("excel bridge section failed", section=section, error=str(exc))

    def _now_ist(self) -> datetime:
        return self._clock.now().astimezone(IST)

    # ---------------------------------------------------------------- quotes
    def _fetch_quotes(self, rows: Sequence[MasterRow]) -> dict[str, Quote]:
        """Quotes for `rows`, keyed by symboltoken, chunked under the API's cap."""
        out: dict[str, Quote] = {}
        tokens = [row.symboltoken for row in rows]
        for start in range(0, len(tokens), MAX_TOKENS_PER_CALL):
            chunk = tokens[start : start + MAX_TOKENS_PER_CALL]
            payload = self._quotes.quotes(
                [{"exchange_segment": _SEGMENT, "instrument_token": token} for token in chunk]
            )
            if isinstance(payload, dict):
                raise DataError(
                    f"quote poll failed: {payload.get('Error') or payload.get('error') or payload}"
                )
            if not isinstance(payload, list):
                raise DataError(f"quote poll returned an unreadable payload: {payload!r}")
            entries = [entry for entry in payload if isinstance(entry, dict)]
            for token in chunk:
                match = _payload_for(entries, token)
                if match is not None:
                    out[token] = self._quote_builder.build(match)
        return out

    def _refresh_quotes(self) -> dict[str, Decimal]:
        """Fill the Quotes sheet. Returns instrument-key -> LTP for the P&L marks.

        Reusing this tick's quotes as the portfolio's marks means the two sheets
        cannot disagree about the price of the same contract, and costs no extra
        API call for a position the operator is already watching.
        """
        symbols = [
            sync.text_or_blank(cell)
            for (cell,) in self._io.read(
                layout.Quotes.SHEET, layout.FIRST_DATA_ROW, 1, layout.MAX_WATCH_ROWS, 1
            )
        ]

        resolved: list[tuple[int, MasterRow | None, str]] = []
        for offset, symbol in enumerate(symbols):
            if not symbol:
                resolved.append((offset, None, ""))
                continue
            try:
                resolved.append((offset, sync.resolve_row(self._master, symbol,
                                                          exchange=self._exchange), ""))
            except DataError as exc:
                resolved.append((offset, None, str(exc)))

        rows = [row for _, row, _ in resolved if row is not None]
        quotes = self._fetch_quotes(rows) if rows else {}
        now_ist = self._now_ist()

        width = len(layout.Quotes.HEADERS) - (layout.Quotes.FIRST_OUTPUT_COL - 1)
        table: list[list[Any]] = []
        marks: dict[str, Decimal] = {}
        for _offset, row, error in resolved:
            if row is None:
                # A blank input row writes blanks; a bad one writes the reason in
                # STATUS, which is the only place the operator will see it.
                cells: list[Any] = [None] * width
                if error:
                    cells[-2] = "ERROR"
                    cells[-1] = error
                table.append(cells)
                continue
            quote = quotes.get(row.symboltoken)
            table.append(sync.quote_cells(row, quote, now_ist=now_ist))
            if quote is not None and quote.ltp is not None:
                try:
                    key = orders.instrument_for(row, self._master, exchange=self._exchange).key
                except DataError:
                    continue
                marks[key] = quote.ltp

        self._io.write(
            layout.Quotes.SHEET,
            layout.FIRST_DATA_ROW,
            layout.Quotes.FIRST_OUTPUT_COL,
            sync.pad(table, to_rows=layout.MAX_WATCH_ROWS, width=width),
        )
        return marks

    # ----------------------------------------------------------------- chain
    def _refresh_chain(self) -> None:
        if self._chain_feed_for is None:
            return
        underlying = sync.text_or_blank(
            self._io.read(layout.Chain.SHEET, *layout.Chain.UNDERLYING_CELL, 1, 1)[0][0]
        ).upper()
        if not underlying:
            return
        expiry = sync.parse_expiry(
            self._io.read(layout.Chain.SHEET, *layout.Chain.EXPIRY_CELL, 1, 1)[0][0]
        )
        if expiry is None:
            listed = self._master.option_expiries(underlying, self._exchange)
            if not listed:
                raise DataError(
                    f"no {underlying} options listed in the instrument master; "
                    "nothing to build a chain from"
                )
            expiry = listed[0]

        snapshot = self._chain_feed_for(underlying).poll(expiry)
        table = sync.chain_cells(snapshot)
        self._io.write(
            layout.Chain.SHEET,
            *layout.Chain.FUTURES_PRICE_CELL,
            [[float(snapshot.futures_price)]],
        )
        self._io.write(
            layout.Chain.SHEET,
            layout.Chain.FIRST_DATA_ROW,
            1,
            sync.pad(table, to_rows=layout.MAX_CHAIN_ROWS, width=len(layout.Chain.HEADERS)),
        )

    # ------------------------------------------------------------- portfolio
    def _refresh_portfolio(self, marks: dict[str, Decimal]) -> None:
        if self._broker is None:
            return
        positions = self._broker.positions()
        funds = self._broker.funds()
        self._io.write(
            layout.Portfolio.SHEET,
            *layout.Portfolio.CASH_CELL,
            [[float(funds.cash)], [float(funds.margin_used)], [float(funds.margin_available)]],
        )
        self._io.write(
            layout.Portfolio.SHEET,
            layout.Portfolio.FIRST_DATA_ROW,
            1,
            sync.pad(
                sync.position_cells(positions, marks),
                to_rows=layout.MAX_POSITION_ROWS,
                width=len(layout.Portfolio.HEADERS),
            ),
        )

    # ---------------------------------------------------------------- orders
    def _drain_orders(self) -> None:
        table = self._io.read(
            layout.Orders.SHEET,
            layout.FIRST_DATA_ROW,
            1,
            layout.MAX_ORDER_ROWS,
            len(layout.Orders.HEADERS),
        )
        for offset, cells in enumerate(table):
            if not orders.is_armed(cells):
                continue
            self._send_one(cells, row=layout.FIRST_DATA_ROW + offset)

    def _send_one(self, cells: list[Any], *, row: int) -> None:
        """Parse, gate, stamp, send, report — for exactly one armed row."""
        try:
            armed = orders.parse_row(cells, row=row)
        except DataError as exc:
            self._write_order_status(row, "REJECTED", detail=str(exc))
            return
        if armed is None:  # re-checked by parse_row; not armed after all
            return

        reason = orders.gate_reason(self._mode)
        if reason is not None:
            # Validated as far as it can be without sending, so a dry run still
            # catches a bad symbol or a missing lot size.
            try:
                contract = sync.resolve_row(self._master, armed.symbol, exchange=self._exchange)
                orders.build_order(
                    armed,
                    contract,
                    self._master,
                    now=self._clock.now(),
                    order_id=orders.client_order_id(armed, at=self._clock.now()),
                    exchange=self._exchange,
                )
            except (DataError, ValueError) as exc:
                self._write_order_status(row, "REJECTED", detail=str(exc))
                return
            self._write_order_status(row, "DRY RUN", detail=reason)
            return

        if self._broker is None:
            self._write_order_status(
                row,
                "NO SESSION",
                detail="live mode is set but no Kotak trade session was established; "
                "the bridge was started without full credentials",
            )
            return

        try:
            contract = sync.resolve_row(self._master, armed.symbol, exchange=self._exchange)
            order_id = orders.client_order_id(armed, at=self._clock.now())
            order = orders.build_order(
                armed,
                contract,
                self._master,
                now=self._clock.now(),
                order_id=order_id,
                exchange=self._exchange,
            )
        except (DataError, ValueError) as exc:
            self._write_order_status(row, "REJECTED", detail=str(exc))
            return

        # Stamped BEFORE the call, so a crash in the window leaves a row that says
        # SENDING and is never retried automatically. See the orders module.
        self._write_order_status(row, orders.SENDING, order_id=order_id)
        try:
            ref = self._broker.place(order)
        except AlgoError as exc:
            self._write_order_status(row, "FAILED", order_id=order_id, detail=str(exc))
            return
        self._write_order_status(
            row, "SENT", order_id=order_id, broker_order_id=ref.broker_order_id
        )
        log.info(
            "order placed from workbook",
            row=row,
            client_order_id=order_id,
            broker_order_id=ref.broker_order_id,
        )

    def _write_order_status(
        self,
        row: int,
        status: str,
        *,
        order_id: str = "",
        broker_order_id: str = "",
        detail: str = "",
    ) -> None:
        """Write the four columns the bridge owns on an Orders row, in one call."""
        self._io.write(
            layout.Orders.SHEET,
            row,
            layout.Orders.FIRST_OUTPUT_COL,
            [
                [
                    f"{status}: {detail}" if detail else status,
                    order_id,
                    broker_order_id,
                    self._now_ist().strftime(sync.TIME_FORMAT),
                ]
            ],
        )

    # ---------------------------------------------------------------- status
    def _write_status(self) -> None:
        self._io.write(
            layout.Status.SHEET,
            layout.Status.FIRST_ROW,
            layout.Status.VALUE_COL,
            sync.status_cells(
                mode=str(self._mode),
                quotes_feed="connected" if self._quotes is not None else "none",
                trade_session=(
                    self._broker.health().detail if self._broker is not None else "not connected"
                ),
                last_refresh=self._now_ist(),
                last_error=self._last_error,
            ),
        )


def _payload_for(payloads: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """Match a quote payload to its token, tolerating the `SEGMENT|token` prefix the
    API sometimes writes into `exchange_token`."""
    for entry in payloads:
        exchange_token = str(entry.get("exchange_token") or "")
        if exchange_token == token or exchange_token.endswith(f"|{token}"):
            return entry
    return None
