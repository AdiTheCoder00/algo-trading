"""Turning domain objects into cell values, and nothing else.

Every function here is pure: domain objects in, lists of cells out. That is what
makes the interesting behaviour — which strike lands on which row, what a missing
book looks like, how a short position's P&L is signed — testable without Excel,
without a broker, and without a network.

**Decimals become floats at this boundary and nowhere earlier.** The engine prices
in `Decimal` deliberately; Excel has no such type and COM would coerce it anyway.
Doing the conversion in one place keeps the lossy step visible instead of scattered
through the writers.

**Missing stays missing.** An absent bid is written as an empty cell, never as
zero — the same distinction `algo/data/mcx_chain_excel.py` preserves when reading a
chain. "Nobody is quoting this" and "the quote is zero" are different facts, and a
sheet that blurs them invites an order against a phantom price.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Exchange, Right
from algo.core.errors import DataError
from algo.core.quote import Quote
from algo.excel import layout
from algo.exchange.master import InstrumentMaster, MasterRow
from algo.execution.broker import BrokerPositionSnapshot

#: How the bridge stamps every "as of" cell. Seconds matter — a refresh that has
#: stopped advancing is the failure this column exists to expose.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _num(value: Decimal | int | float | None) -> float | None:
    """Decimal to float for Excel, preserving None as an empty cell."""
    return None if value is None else float(value)


def text_or_blank(value: Any) -> str:
    """A cell as trimmed text, with an untouched cell reading as the empty string.

    Excel hands back None for an empty cell and a float for anything that looks
    numeric, so a symbol column cannot simply be treated as `str`.
    """
    return "" if value is None else str(value).strip()


def resolve_row(
    master: InstrumentMaster,
    symbol: str,
    *,
    exchange: Exchange = Exchange.MCX,
) -> MasterRow:
    """Find the contract an operator meant by `symbol`.

    Two spellings are accepted, because both are natural to type: an exact
    tradingsymbol (`GOLDM25SEPFUT`) pins one contract, and a bare underlying
    (`GOLDM`) means "the front month" — resolved to the nearest expiry, since
    `InstrumentMaster.future_rows` sorts ascending by expiry.

    The exact match is tried first. If it were the other way round an underlying
    that happened to also be a listed tradingsymbol would silently win, and the
    operator would be watching a contract they did not name.
    """
    wanted = symbol.strip().upper()
    if not wanted:
        raise DataError("blank symbol")

    exact = master.row_by_symbol(wanted)
    if exact is not None:
        return exact

    futures = master.future_rows(wanted, exchange)
    if futures:
        return futures[0]  # ascending by expiry, so [0] is the front month

    raise DataError(
        f"{wanted!r} is neither a tradingsymbol nor an underlying with listed "
        f"{exchange} futures in the instrument master. Refresh the master with "
        "--refresh-master if the contract is newly listed."
    )


def quote_cells(
    row: MasterRow,
    quote: Quote | None,
    *,
    now_ist: datetime,
) -> list[Any]:
    """The output half of one Quotes row: TRADINGSYMBOL through UPDATED_IST.

    Only the columns the bridge owns are returned. SYMBOL stays as the operator
    typed it — a refresh that rewrote it would fight whoever is editing the sheet.
    """
    stamp = now_ist.strftime(TIME_FORMAT)
    if quote is None:
        return [
            row.tradingsymbol,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            row.expiry.isoformat() if row.expiry else None,
            "NO QUOTE",
            stamp,
        ]
    return [
        row.tradingsymbol,
        _num(quote.ltp),
        _num(quote.bid),
        _num(quote.ask),
        quote.bid_qty,
        quote.ask_qty,
        quote.volume,
        quote.open_interest,
        row.expiry.isoformat() if row.expiry else None,
        str(quote.status()),
        stamp,
    ]


def chain_cells(snapshot: OptionChainSnapshot) -> list[list[Any]]:
    """The ladder, one row per strike, calls left and puts right.

    Strikes come from whichever side lists them, so a strike quoted only as a call
    still gets a row with an empty put half rather than being dropped. A ladder
    that silently omits a one-sided strike would misrepresent where the chain ends.
    """
    by_strike: dict[Decimal, dict[Right, ChainRow]] = {}
    for chain_row in snapshot.rows:
        by_strike.setdefault(chain_row.strike, {})[chain_row.right] = chain_row

    def side(row: ChainRow | None, *, call: bool) -> list[Any]:
        if row is None:
            return [None, None, None, None, None]
        quote = row.quote
        if call:
            # CE_OI, CE_VOLUME, CE_BID, CE_ASK, CE_LTP
            return [
                quote.open_interest,
                quote.volume,
                _num(quote.bid),
                _num(quote.ask),
                _num(quote.ltp),
            ]
        # PE_LTP, PE_BID, PE_ASK, PE_VOLUME, PE_OI.
        #
        # Written out rather than reversing the call list. The put columns mirror
        # the calls so the two books meet at the strike, but only their *order*
        # mirrors — a bid is still a bid. Reversing produced [LTP, ASK, BID, ...]
        # against headers [PE_LTP, PE_BID, PE_ASK, ...], quietly swapping the put
        # book so every put appeared to be bid above its offer.
        return [
            _num(quote.ltp),
            _num(quote.bid),
            _num(quote.ask),
            quote.volume,
            quote.open_interest,
        ]

    out: list[list[Any]] = []
    for strike in sorted(by_strike):
        pair = by_strike[strike]
        out.append(
            [
                *side(pair.get(Right.CE), call=True),
                _num(strike),
                *side(pair.get(Right.PE), call=False),
            ]
        )
    return out


def position_cells(
    positions: Sequence[BrokerPositionSnapshot],
    ltp_by_key: Mapping[str, Decimal],
) -> list[list[Any]]:
    """Positions with mark-to-market P&L, or blank P&L where there is no mark.

    `qty` is signed, so `(ltp - average) * qty` is already correct for a short:
    two lots sold at 100 and now worth 90 gives `(90 - 100) * -2 = +20`. Writing
    the multiplication once, rather than branching on side, is what keeps the sign
    right — a short leg mis-signed here reads as a loss while it is making money.

    A position with no live mark gets an empty LTP and an empty P&L. Falling back
    to the average price would render every unmarked position as flat, which is
    the most reassuring possible way to be wrong.
    """
    out: list[list[Any]] = []
    for position in positions:
        ltp = ltp_by_key.get(position.instrument_key)
        pnl = (ltp - position.average_price) * position.qty if ltp is not None else None
        out.append(
            [
                position.instrument_key,
                position.lots,
                _num(position.qty),
                _num(position.average_price),
                _num(ltp),
                _num(pnl),
            ]
        )
    return out


def pad(rows: Sequence[Sequence[Any]], *, to_rows: int, width: int) -> list[list[Any]]:
    """Pad a table out to a fixed height with blank cells.

    The bridge writes a fixed window every refresh rather than only the rows it
    filled, so that deleting a symbol clears its old numbers instead of leaving
    them frozen on screen. A stale row that still looks live is worse than a blank
    one. Rows longer than `width` are truncated so a caller can never write past
    the columns it owns.
    """
    padded = [list(row[:width]) + [None] * (width - len(row[:width])) for row in rows[:to_rows]]
    padded.extend([None] * width for _ in range(to_rows - len(padded)))
    return padded


def status_cells(
    *,
    mode: str,
    quotes_feed: str,
    trade_session: str,
    last_refresh: datetime | None,
    last_error: str,
) -> list[list[Any]]:
    """The Status sheet, in `layout.Status.LABELS` order."""
    return [
        [mode],
        [quotes_feed],
        [trade_session],
        [last_refresh.strftime(TIME_FORMAT) if last_refresh else "never"],
        [last_error],
    ]


def parse_expiry(value: Any) -> date | None:
    """Read the Chain sheet's EXPIRY cell, which Excel may hand back either way.

    A date-formatted cell arrives as a `datetime`; a text cell arrives as a string.
    Blank means "the nearest listed expiry", which is why this returns None rather
    than raising — an operator who has not chosen an expiry has not made an error.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise DataError(
            f"cannot read {text!r} as the chain expiry in "
            f"{layout.Chain.SHEET}!B{layout.Chain.EXPIRY_CELL[0]}. "
            "Use YYYY-MM-DD, or leave it blank for the nearest listed expiry."
        ) from exc
