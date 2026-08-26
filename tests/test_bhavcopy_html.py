"""Reading the MCX "commodity wise" export.

The fixture below is a byte-for-byte-shaped excerpt of a real file (D-105):
an HTML `<table>` served with an `.xls` extension, a trade date like
"29 Jul 2026", an expiry like "29JUL2026", unit-suffixed volume and open-interest
headers, and untraded rows carrying only a settlement close.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.enums import Right
from algo.core.errors import DataError
from algo.data.bhavcopy import (
    MCX_COMMODITY_WISE_COLUMNS,
    MCX_DEFAULT_COLUMNS,
    parse_rows,
)

HEADERS = [
    "Date",
    "Instrument Name",
    "Symbol",
    "Expiry Date",
    "Option Type",
    "Strike Price",
    "Open",
    "High",
    "Low",
    "Close",
    "Previous Close",
    "Volume(Lots)",
    "Volume(In 000's)",
    "Value(Lacs)",
    "Open Interest(Lots)",
]

# A traded put, an untraded call (empty O/H/L), and a futures row.
ROWS = [
    ["29 Jul 2026", "OPTFUT", "GOLDM", "29JUL2026", "PE", "120000",
     "2.00", "2.00", "0.50", "0.50", "0.50", "7980", "0.000 GRMS", "1.00", "5162"],
    ["29 Jul 2026", "OPTFUT", "GOLDM", "29JUL2026", "CE", "177500",
     "", "", "", "0.50", "0.50", "0", "0.000 GRMS", "0.00", "0"],
    ["29 Jul 2026", "FUTCOM", "GOLDM", "31AUG2026", "", "",
     "141500", "142100", "141200", "141850", "141600", "3412", "0.000 GRMS", "9.00", "8801"],
]


def _html(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f'<th style="padding: 5px;">{h}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td style="padding: 5px;">{c}</td>' for c in r) + "</tr>"
        for r in rows
    )
    return f'\n<table border="1">\n<thead>\n<tr>{head}</tr>\n</thead>\n{body}\n</table>'


@pytest.fixture
def export(tmp_path: Path) -> Path:
    # The real download is named .xls while being HTML - the point of the test.
    path = tmp_path / "BhavCopyCommodiyWise_01072026.xls"
    path.write_text(_html(HEADERS, ROWS), encoding="utf-8")
    return path


class TestTheExportIsReadAsHtml:
    def test_an_xls_that_is_really_html_still_parses(self, export: Path) -> None:
        rows = parse_rows(export)

        assert len(rows) == 3

    def test_the_verified_layout_is_selected_without_being_asked(
        self, export: Path
    ) -> None:
        """The blind mapping is missing `Volume(Lots)`; auto-detection has to
        pick the checked one instead of failing."""
        assert MCX_DEFAULT_COLUMNS.volume == "Volume"
        assert MCX_COMMODITY_WISE_COLUMNS.volume == "Volume(Lots)"

        rows = parse_rows(export)

        assert rows[0].volume == 7980

    def test_the_two_date_formats_in_one_file_both_parse(self, export: Path) -> None:
        row = parse_rows(export)[0]

        assert row.trade_date == date(2026, 7, 29)  # "29 Jul 2026"
        assert row.expiry == date(2026, 7, 29)  # "29JUL2026"

    def test_a_traded_option_keeps_every_real_number(self, export: Path) -> None:
        row = parse_rows(export)[0]

        assert row.is_option
        assert row.right is Right.PE
        assert row.strike == Decimal("120000")
        assert (row.open, row.high, row.low, row.close) == (
            Decimal("2.00"),
            Decimal("2.00"),
            Decimal("0.50"),
            Decimal("0.50"),
        )
        assert row.open_interest == 5162
        assert row.traded

    def test_the_futures_row_is_kept_and_marked(self, export: Path) -> None:
        """`build_snapshots` needs these for the forward - a file without them
        cannot produce a delta, so losing them silently would be serious."""
        future = [r for r in parse_rows(export) if not r.is_option]

        assert len(future) == 1
        assert future[0].expiry == date(2026, 8, 31)
        assert future[0].close == Decimal("141850")
        assert future[0].strike is None
        assert future[0].right is None

    def test_prices_are_decimals_never_floats(self, export: Path) -> None:
        for row in parse_rows(export):
            for value in (row.open, row.high, row.low, row.close):
                assert isinstance(value, Decimal)


class TestAnUntradedRow:
    """Empty open/high/low with a settlement close - about 80% of the ladder."""

    def test_it_does_not_abort_the_file(self, export: Path) -> None:
        assert len(parse_rows(export)) == 3

    def test_the_empty_prices_fall_back_to_the_settlement_close(
        self, export: Path
    ) -> None:
        row = parse_rows(export)[1]

        assert (row.open, row.high, row.low) == (Decimal("0.50"),) * 3
        assert row.close == Decimal("0.50")

    def test_it_is_still_untraded(self, export: Path) -> None:
        """The fallback must not make a dead strike look tradeable - that is the
        whole distinction `traded` exists to carry."""
        row = parse_rows(export)[1]

        assert row.volume == 0
        assert not row.traded


class TestItStillFailsLoudly:
    def test_an_unknown_layout_names_every_missing_column(self, tmp_path: Path) -> None:
        path = tmp_path / "wrong.xls"
        path.write_text(
            _html(["Trade Date", "Ticker", "Settle"], [["x", "y", "z"]]), encoding="utf-8"
        )

        with pytest.raises(DataError) as exc:
            parse_rows(path)

        message = str(exc.value)
        assert "does not match any known bhavcopy layout" in message
        assert "Volume(Lots)" in message  # what layout 1 wanted
        assert "Trade Date" in message  # what the file actually has

    def test_html_without_a_header_row_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "headerless.xls"
        path.write_text("<table><tr><td>1</td></tr></table>", encoding="utf-8")

        with pytest.raises(DataError, match="no table header row"):
            parse_rows(path)

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="bhavcopy not found"):
            parse_rows(tmp_path / "absent.xls")


class TestCsvStillWorks:
    """The HTML path must not have cost the original one."""

    def test_a_plain_csv_in_the_blind_layout_parses(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.csv"
        header = ",".join(MCX_DEFAULT_COLUMNS.required())
        path.write_text(
            f"{header}\n29-Jul-2026,OPTFUT,GOLDM,29JUL2026,120000,PE,2,2,0.5,0.5,7980,5162\n",
            encoding="utf-8",
        )

        rows = parse_rows(path)

        assert len(rows) == 1
        assert rows[0].strike == Decimal("120000")
        assert rows[0].volume == 7980
