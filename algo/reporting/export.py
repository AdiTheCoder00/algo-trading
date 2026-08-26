"""Trade log and equity export. Brief §13, and the golden file from §11.

Everything written here is **byte-stable**: sorted columns, `Decimal` rendered by
`str()` rather than by a format string, timestamps in one fixed UTC form. That is
what makes the golden-file test meaningful — a diff shows a behaviour change, not
a locale, a float repr or a dictionary that happened to iterate differently.

The temptation is to format numbers nicely on the way out. Two decimal places, a
thousands separator, a rounded R-multiple. Every one of those makes the file
prettier and the diff useless, so presentation lives in the tearsheet and this
module writes exactly what the engine computed.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from algo.core.ids import stable_hash
from algo.core.trade import Trade

TRADE_COLUMNS = (
    "trade_id",
    "strategy_id",
    "signal_id",
    "opened_at",
    "closed_at",
    "legs",
    "gross_pnl",
    "charges_total",
    "net_pnl",
    "r_multiple",
    "exit_reason",
    "reason",
)

EQUITY_COLUMNS = (
    "ts",
    "equity",
    "cash",
    "market_value",
    "realised_pnl",
    "unrealised_pnl",
    "charges",
    "open_positions",
)


def trade_rows(trades: Sequence[Trade]) -> list[dict[str, str]]:
    return [trade.to_log_row() for trade in trades]


def write_trade_log(trades: Sequence[Trade], path: Path) -> Path:
    """Write the trade log as CSV.

    `newline=""` and an explicit `lineterminator` because the default on Windows
    produces `\\r\\r\\n`, which makes the same run hash differently on different
    machines — and a golden file that depends on the operating system is not a
    golden file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(TRADE_COLUMNS), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(trade_rows(trades))
    return path



def trade_log_digest(trades: Sequence[Trade]) -> str:
    """A stable hash of the trade log.

    Brief §7.4 asks for a byte-identical trade log across runs. Comparing digests
    says *whether* two runs agree; comparing the CSVs says *where* they differ.
    The golden test does both — the digest fails fast, the file shows why.
    """
    return stable_hash({"rows": trade_rows(trades)})
