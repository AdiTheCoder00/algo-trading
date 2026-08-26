"""The live-chain Excel reader maps columns by position, so the tests that
matter are the ones proving it notices when the layout moves under it."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from algo.core.enums import Right
from algo.core.errors import DataError
from algo.core.timeutil import ist_to_utc
from algo.data.mcx_chain_excel import EXPECTED_HEADER, load_chain
from algo.pricing.chain_greeks import enrich

TITLES = [
    ("GOLDM Option Chain - 2026-08-28",),
    ("As on 26 Aug 2026 - 20:20 IST",),
    ("Underlying Value: 160,837.00",),
    ("CALLS", None, None, None, None, None, None, None, None, None, "PUTS"),
]


#: A real 160000 row from a live GOLDM scrape, underlying 160,837. Real rather
#: than invented because the parity test below only means something if the call
#: and the put are a genuinely consistent pair.
_REAL_CE = (4725, 0, 38756, 1779.5, 0, 4000, 1781.5, 1788.0, 4000)
_REAL_PE = (4000, 951.5, 955.5, 4000, 0, 952.0, 139762, 0, 10581)


def _row(
    strike: float,
    *,
    ce: tuple[object, ...] = _REAL_CE,
    pe: tuple[object, ...] = _REAL_PE,
) -> tuple[object, ...]:
    return (*ce, strike, *pe)


def _write(path: Path, body: list[tuple[object, ...]], *, header: tuple[str, ...] | None = None):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "option_chain"
    for line in TITLES:
        sheet.append(list(line))
    sheet.append(list(header if header is not None else EXPECTED_HEADER))
    for line in body:
        sheet.append(list(line))
    workbook.save(path)
    workbook.close()
    return path


class TestLayout:
    def test_a_moved_column_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        shifted = ("Strike Price", *EXPECTED_HEADER[1:])
        path = _write(tmp_path / "c.xlsx", [_row(160000)], header=shifted)

        with pytest.raises(DataError) as excinfo:
            load_chain(path)

        # The message has to carry both sides, or fixing it is a debugging session.
        assert "wanted" in str(excinfo.value)
        assert "found" in str(excinfo.value)

    def test_a_file_with_nothing_below_the_header_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.xlsx", [])
        with pytest.raises(DataError, match="too few rows"):
            load_chain(path)

    def test_rows_that_carry_no_usable_strike_are_an_error_not_an_empty_chain(
        self, tmp_path: Path
    ) -> None:
        """A footer or a blank spacer row must not quietly produce a chain with
        no strikes in it - an empty ladder would read as 'nothing is listed'."""
        path = _write(tmp_path / "c.xlsx", [_row(0), _row(0)])
        with pytest.raises(DataError, match="no strike rows"):
            load_chain(path)

    def test_an_unreadable_title_line_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.xlsx", [_row(160000)])
        workbook = openpyxl.load_workbook(path)
        workbook["option_chain"]["A3"] = "Underlying Value: nonsense"
        workbook.save(path)
        workbook.close()

        with pytest.raises(DataError, match="underlying value"):
            load_chain(path)


class TestParsing:
    def test_the_header_is_read_into_symbol_expiry_time_and_underlying(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "c.xlsx", [_row(160000)])

        chain = load_chain(path)

        assert chain.underlying == "GOLDM"
        assert chain.option_expiry == date(2026, 8, 28)
        assert chain.futures_price == Decimal("160837.00")
        # 20:20 IST is 14:50 UTC - the offset must be applied, not ignored.
        assert chain.ts == ist_to_utc(date(2026, 8, 26), time(20, 20))

    def test_each_strike_yields_a_call_and_a_put_with_their_own_book(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "c.xlsx", [_row(160000)])

        rows = {r.right: r for r in load_chain(path).rows}

        assert rows[Right.CE].quote.bid == Decimal("1781.5")
        assert rows[Right.CE].quote.ask == Decimal("1788.0")
        assert rows[Right.CE].quote.volume == 38756
        assert rows[Right.PE].quote.bid == Decimal("951.5")
        assert rows[Right.PE].quote.ask == Decimal("955.5")
        assert rows[Right.PE].quote.volume == 139762

    def test_an_unquoted_strike_keeps_none_rather_than_becoming_zero(
        self, tmp_path: Path
    ) -> None:
        """'Nobody is quoting this' and 'the quote is zero' are different facts,
        and the whole tradeability gate depends on telling them apart."""
        blank = _row(
            170000,
            ce=(None, None, 0, 0.5, 0, 0, None, None, 0),
            pe=(0, None, None, 0, 0, 0.5, 0, None, None),
        )
        path = _write(tmp_path / "c.xlsx", [blank])

        row = next(r for r in load_chain(path).rows if r.right is Right.CE)

        assert row.quote.bid is None
        assert row.quote.ask is None
        assert not row.quote.is_tradeable

    def test_rows_come_back_sorted_so_a_run_is_reproducible(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.xlsx", [_row(161000), _row(160000), _row(160500)])

        keys = [(r.strike, r.right.value) for r in load_chain(path).rows]

        assert keys == sorted(keys)

    def test_the_futures_expiry_defaults_to_the_option_expiry_and_is_overridable(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "c.xlsx", [_row(160000)])

        assert load_chain(path).rows[0].option.underlying_future.expiry == date(2026, 8, 28)

        overridden = load_chain(path, futures_expiry=date(2026, 9, 4))
        assert overridden.rows[0].option.underlying_future.expiry == date(2026, 9, 4)


class TestGreeks:
    def test_a_real_book_solves_to_a_delta_that_respects_put_call_parity(
        self, tmp_path: Path
    ) -> None:
        """The strongest available check that the loader wired price, strike and
        right together correctly: |call delta| + |put delta| at one strike is 1,
        and it only comes out that way if all three are right."""
        path = _write(tmp_path / "c.xlsx", [_row(160000)])
        chain = load_chain(path)

        priced = enrich(
            chain, expires_at=ist_to_utc(chain.option_expiry, time(23, 30)), r=0.065
        )
        call = next(r for r in priced.rows if r.right is Right.CE)
        put = next(r for r in priced.rows if r.right is Right.PE)

        assert call.delta is not None and put.delta is not None
        assert call.delta - put.delta == pytest.approx(1.0, abs=0.02)
