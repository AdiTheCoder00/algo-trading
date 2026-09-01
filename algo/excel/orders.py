"""Reading order rows out of a spreadsheet, safely.

A spreadsheet is the least disciplined order source imaginable: every cell is free
text, a stray keystroke changes a number, and an accidental paste can arm two
hundred rows at once. This module exists to make that source safe enough to point
at a real account, and it does so with three rules.

**A row must be armed deliberately.** Nothing is sent because it was typed. The
operator writes an explicit value into SEND, and only a recognised one counts — an
unrecognised entry leaves the row alone rather than being guessed at.

**Status is written before the broker is called, never after.** The sequence is:
stamp CLIENT_ORDER_ID and STATUS=SENDING, then place, then overwrite STATUS with
the outcome. A crash anywhere in that window leaves a row that says SENDING, and a
row that says SENDING is never retried automatically — it is surfaced for a human
to reconcile against the order book. Writing status afterwards would invert the
failure: a crash mid-send would leave a blank row that looks unsent, and the next
tick would send it again.

**Live is off by default.** The gate is `algo.config.modes.resolve_mode`, the same
three-condition check the engine uses. In any other mode a parsed, validated order
is written back as DRY RUN and never reaches the transport, so the whole path can
be exercised against a real session without an order leaving the building.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from algo.core.enums import Exchange, Mode, OrderType, ProductType, Side
from algo.core.errors import DataError
from algo.core.ids import stable_hash
from algo.core.instrument import FutureId, InstrumentId, OptionId
from algo.core.order import Order
from algo.excel import layout
from algo.exchange.master import InstrumentMaster, MasterRow, right_of

#: Written into STATUS before the broker is called. A row still saying this after a
#: restart means the outcome is genuinely unknown — see the module docstring.
SENDING = "SENDING"

#: Order types that need a limit price, and those that need a trigger. Kept here
#: rather than inferred so the error messages can name what is missing; `Order`
#: itself enforces the same rules, but it raises after the fact and in its own
#: vocabulary rather than the sheet's.
_NEEDS_LIMIT = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT})
_NEEDS_TRIGGER = frozenset({OrderType.STOP_MARKET, OrderType.STOP_LIMIT})


@dataclass(frozen=True)
class ArmedRow:
    """One order the operator has armed, parsed but not yet resolved to a contract."""

    row: int
    symbol: str
    side: Side
    lots: int
    order_type: OrderType
    product: ProductType
    limit_price: Decimal | None
    trigger_price: Decimal | None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any, *, field: str, row: int) -> Decimal | None:
    text = _text(value)
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise DataError(f"row {row}: {field} is {text!r}, which is not a number") from exc


def _enum(value: Any, options: type[Any], *, field: str, row: int) -> Any:
    text = _text(value).upper()
    try:
        return options(text)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in options)
        raise DataError(
            f"row {row}: {field} is {text or '(blank)'!r}; expected one of {allowed}"
        ) from exc


def is_armed(cells: list[Any]) -> bool:
    """Is this row asking to be sent, and has it not been picked up already?

    Both halves matter. SEND alone would re-send every completed row on every tick;
    an empty STATUS alone would send rows nobody armed.
    """
    send = _text(_at(cells, layout.Orders.SEND_COL)).lower()
    status = _text(_at(cells, layout.Orders.STATUS_COL))
    return send in layout.Orders.SEND_VALUES and not status


def _at(cells: list[Any], col: int) -> Any:
    """1-based column access that tolerates a short row.

    Excel hands back a ragged tail when the right-hand columns have never been
    touched, and a trailing-blank row is normal rather than an error.
    """
    index = col - 1
    return cells[index] if 0 <= index < len(cells) else None


def parse_row(cells: list[Any], *, row: int) -> ArmedRow | None:
    """Parse one armed row, or return None if the row is not armed.

    Raises `DataError` naming the row and the offending column when an armed row
    cannot be read — the operator gets that text back in STATUS, which is the only
    place they will look.
    """
    if not is_armed(cells):
        return None

    symbol = _text(_at(cells, 1)).upper()
    if not symbol:
        raise DataError(f"row {row}: SYMBOL is blank")

    lots_text = _text(_at(cells, 3))
    try:
        lots = int(Decimal(lots_text))
    except (InvalidOperation, ValueError) as exc:
        raise DataError(
            f"row {row}: LOTS is {lots_text or '(blank)'!r}, which is not a whole number"
        ) from exc
    if lots < 1:
        raise DataError(f"row {row}: LOTS is {lots}; an order must be for at least one lot")

    order_type = _enum(_at(cells, 4), OrderType, field="ORDER_TYPE", row=row)
    limit_price = _decimal(_at(cells, 6), field="LIMIT_PRICE", row=row)
    trigger_price = _decimal(_at(cells, 7), field="TRIGGER_PRICE", row=row)

    # Checked here as well as in `Order` so the message names the sheet column the
    # operator has to fix, rather than the model field they have never heard of.
    if order_type in _NEEDS_LIMIT and limit_price is None:
        raise DataError(f"row {row}: {order_type} needs a LIMIT_PRICE")
    if order_type not in _NEEDS_LIMIT and limit_price is not None:
        raise DataError(f"row {row}: {order_type} must not carry a LIMIT_PRICE")
    if order_type in _NEEDS_TRIGGER and trigger_price is None:
        raise DataError(f"row {row}: {order_type} needs a TRIGGER_PRICE")
    if order_type not in _NEEDS_TRIGGER and trigger_price is not None:
        raise DataError(f"row {row}: {order_type} must not carry a TRIGGER_PRICE")

    product_cell = _at(cells, 5)
    return ArmedRow(
        row=row,
        symbol=symbol,
        side=_enum(_at(cells, 2), Side, field="SIDE", row=row),
        lots=lots,
        order_type=order_type,
        # Blank PRODUCT means NRML, which is what an MCX overnight strategy wants
        # and what the rest of the engine defaults to.
        product=(
            ProductType.NRML
            if not _text(product_cell)
            else _enum(product_cell, ProductType, field="PRODUCT", row=row)
        ),
        limit_price=limit_price,
        trigger_price=trigger_price,
    )


def client_order_id(armed: ArmedRow, *, at: datetime) -> str:
    """A short, deterministic id for one armed row at one instant.

    Derived from the row's contents and the second it was picked up, so re-arming
    the same row later is a genuinely new order rather than a duplicate the broker
    refuses — while two ticks racing on the same second cannot invent two ids for
    the same intent. The `XL` prefix makes orders from the workbook identifiable in
    the broker's own order book, where it travels as the tag.
    """
    return "XL" + stable_hash(
        {
            "row": armed.row,
            "symbol": armed.symbol,
            "side": armed.side.value,
            "lots": armed.lots,
            "order_type": armed.order_type.value,
            "product": armed.product.value,
            "limit": str(armed.limit_price),
            "trigger": str(armed.trigger_price),
            "at": at.replace(microsecond=0).isoformat(),
        }
    )


def instrument_for(
    row: MasterRow,
    master: InstrumentMaster,
    *,
    exchange: Exchange = Exchange.MCX,
) -> InstrumentId:
    """The `InstrumentId` a master row describes.

    For an option this has to name the *underlying future* as well as the option
    itself, and the two expiries are different dates — the option expires first.
    `algo/core/instrument.py` is emphatic that conflating them is what walks a short
    leg into devolvement, so the futures expiry is looked up with the project's one
    pairing rule (`InstrumentMaster.future_for_option_expiry`): the contract this
    cycle settles into, not the front month. Where the master lists no future at
    all, the option expiry is the last resort.

    Neither `row_for` nor `OptionId.key` reads the futures expiry, so this choice
    cannot misroute an order — it only decides whether the object describes reality.
    """
    if row.expiry is None:
        raise DataError(
            f"{row.tradingsymbol} has no expiry in the instrument master, so it "
            "cannot be resolved to a tradeable instrument"
        )
    if not row.instrumenttype.startswith("OPT"):
        return FutureId(underlying=row.name, expiry=row.expiry, exchange=exchange)

    right = right_of(row.tradingsymbol)
    if right is None or row.strike is None:
        raise DataError(
            f"{row.tradingsymbol} is listed as an option but carries no readable "
            "strike or right; refusing to guess which contract it is"
        )
    future = master.future_for_option_expiry(row.name, exchange, row.expiry)
    futures_expiry = future.expiry if future is not None and future.expiry else row.expiry
    return OptionId(
        underlying_future=FutureId(
            underlying=row.name, expiry=futures_expiry, exchange=exchange
        ),
        option_expiry=row.expiry,
        strike=row.strike,
        right=right,
        exchange=exchange,
    )


def build_order(
    armed: ArmedRow,
    row: MasterRow,
    master: InstrumentMaster,
    *,
    now: datetime,
    order_id: str,
    exchange: Exchange = Exchange.MCX,
) -> Order:
    """Turn an armed row plus its contract into an `Order` the broker can take.

    `qty` comes from the master's lot size rather than from the sheet: the operator
    trades in lots, and a quantity typed by hand is a decimal point away from being
    a hundred times too large.
    """
    if row.lot_size is None or row.lot_size <= 0:
        raise DataError(
            f"row {armed.row}: the instrument master has no lot size for "
            f"{row.tradingsymbol}, so {armed.lots} lots cannot be converted to a "
            "quantity. Refresh the master with --refresh-master."
        )
    return Order(
        client_order_id=order_id,
        # Nothing upstream produced a signal — a human did. Recording that
        # honestly beats minting a signal id for an intent no strategy formed.
        signal_id=f"excel:{armed.row}",
        instrument=instrument_for(row, master, exchange=exchange),
        side=armed.side,
        lots=armed.lots,
        qty=Decimal(armed.lots) * row.lot_size,
        order_type=armed.order_type,
        product=armed.product,
        limit_price=armed.limit_price,
        trigger_price=armed.trigger_price,
        created_at=now,
    )


def gate_reason(mode: Mode) -> str | None:
    """Why an order will not be sent, or None if it will.

    Phrased as a reason rather than a boolean because the reason is what gets
    written into STATUS, and an operator staring at a row that did nothing needs to
    know which of the three live conditions was missing.
    """
    if mode is Mode.LIVE:
        return None
    # No "DRY RUN" prefix here: the caller writes the status word and this is the
    # explanation after it, so repeating it reads as "DRY RUN: DRY RUN".
    return (
        f"mode={mode}, validated and not sent. To send for real, all three of: "
        "--mode live, TRADING_MODE=live in the environment, and "
        "--i-understand-this-is-real-money."
    )
