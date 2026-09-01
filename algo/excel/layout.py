"""The contract between the workbook and the bridge, in one place.

Every sheet name, header and anchor cell the bridge reads or writes lives here.
The reason is the same one `algo/data/mcx_chain_excel.py` gives for reading the
scraped chain by position: a sheet is an interface with no compiler behind it.
If the header text and the code that fills the column drift apart, the result is
not a crash but a workbook that looks right and is wrong.

So the bridge verifies headers on every attach (`io.verify_layout`) and refuses
to write into a sheet whose columns have moved. A user is free to reorder or
rename columns — they will simply be told the workbook no longer matches, and
`--recreate` will rebuild it.

**Input columns versus output columns.** Some columns are typed by the operator
(the symbol to watch, the side to trade) and the rest are written by the bridge.
The split matters: a refresh must never overwrite what the user is in the middle
of typing, so writes are addressed to explicit output ranges rather than to whole
rows. `first_output_col` is what makes that possible.
"""

from __future__ import annotations

from typing import Final

#: Row 1 of every table is its header; data begins on row 2.
HEADER_ROW: Final = 1
FIRST_DATA_ROW: Final = 2

#: How many rows of each table the bridge reads and refreshes. A fixed window
#: keeps every COM round-trip one call regardless of how much is filled in —
#: reading "used range" per tick is what makes naive Excel automation crawl.
MAX_WATCH_ROWS: Final = 200
MAX_CHAIN_ROWS: Final = 400
MAX_ORDER_ROWS: Final = 200
MAX_POSITION_ROWS: Final = 200


class Quotes:
    """Live quotes. The operator types SYMBOL; the bridge fills the rest."""

    SHEET: Final = "Quotes"
    HEADERS: Final = (
        "SYMBOL",
        "TRADINGSYMBOL",
        "LTP",
        "BID",
        "ASK",
        "BID_QTY",
        "ASK_QTY",
        "VOLUME",
        "OI",
        "EXPIRY",
        "STATUS",
        "UPDATED_IST",
    )
    #: SYMBOL is the operator's. Everything from TRADINGSYMBOL on is ours.
    FIRST_OUTPUT_COL: Final = 2


class Chain:
    """The option-chain ladder: calls on the left, strike in the middle, puts on
    the right — the order the exchange itself renders, so a reader who knows the
    exchange screen already knows this sheet."""

    SHEET: Final = "Chain"
    #: The two inputs live above the table, not in it: they describe the whole
    #: ladder rather than any one row.
    UNDERLYING_CELL: Final = (1, 2)  # B1
    EXPIRY_CELL: Final = (2, 2)  # B2
    FUTURES_PRICE_CELL: Final = (3, 2)  # B3, written by the bridge
    HEADER_ROW: Final = 5
    FIRST_DATA_ROW: Final = 6
    HEADERS: Final = (
        "CE_OI",
        "CE_VOLUME",
        "CE_BID",
        "CE_ASK",
        "CE_LTP",
        "STRIKE",
        "PE_LTP",
        "PE_BID",
        "PE_ASK",
        "PE_VOLUME",
        "PE_OI",
    )


class Orders:
    """Order entry.

    SEND is the trigger and STATUS is the interlock. The bridge only ever acts on
    a row whose SEND says yes and whose STATUS is still empty, and it stamps
    STATUS *before* it calls the broker. That ordering is what makes a crash
    mid-send safe: a row that was already picked up is never picked up twice,
    which is the one property an order sheet cannot be allowed to lose.
    """

    SHEET: Final = "Orders"
    HEADERS: Final = (
        "SYMBOL",
        "SIDE",
        "LOTS",
        "ORDER_TYPE",
        "PRODUCT",
        "LIMIT_PRICE",
        "TRIGGER_PRICE",
        "SEND",
        "STATUS",
        "CLIENT_ORDER_ID",
        "BROKER_ORDER_ID",
        "UPDATED_IST",
    )
    #: Columns the operator owns, 1-based: SYMBOL..SEND.
    FIRST_OUTPUT_COL: Final = 9  # STATUS
    SEND_COL: Final = 8
    STATUS_COL: Final = 9

    #: What the operator types into SEND to arm a row. Compared case-folded but
    #: otherwise exactly: an unrecognised value leaves the row alone rather than
    #: being guessed at.
    SEND_VALUES: Final = frozenset({"yes", "y", "send", "true", "1"})


class Portfolio:
    """Positions, and the account totals above them."""

    SHEET: Final = "Portfolio"
    CASH_CELL: Final = (1, 2)  # B1
    MARGIN_USED_CELL: Final = (2, 2)  # B2
    MARGIN_AVAILABLE_CELL: Final = (3, 2)  # B3
    HEADER_ROW: Final = 5
    FIRST_DATA_ROW: Final = 6
    HEADERS: Final = (
        "INSTRUMENT",
        "LOTS",
        "QTY",
        "AVG_PRICE",
        "LTP",
        "PNL",
    )


class Status:
    """What the bridge is doing, in words, so a stale sheet is never mistaken for
    a quiet market. `LAST_REFRESH_IST` is the one cell worth watching: if it stops
    advancing, the numbers above it are history."""

    SHEET: Final = "Status"
    LABELS: Final = (
        "MODE",
        "QUOTES_FEED",
        "TRADE_SESSION",
        "LAST_REFRESH_IST",
        "LAST_ERROR",
    )
    FIRST_ROW: Final = 1
    LABEL_COL: Final = 1
    VALUE_COL: Final = 2


#: Every sheet the bridge expects, in the order `create_workbook` lays them out.
SHEET_ORDER: Final = (
    Status.SHEET,
    Quotes.SHEET,
    Chain.SHEET,
    Orders.SHEET,
    Portfolio.SHEET,
)
