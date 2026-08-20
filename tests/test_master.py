"""The instrument master: parsing, lookups, snapshots. D-009, D-023.

The master file is data to be read, never a formula to be re-derived — so the
tests pin the parsing behaviour exactly: which rows survive, which are skipped,
and what a lookup does when the answer is missing or ambiguous.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.enums import Exchange, Right
from algo.core.errors import DataError
from algo.core.instrument import FutureId, OptionId
from algo.core.timeutil import utc
from algo.exchange.master import (
    InstrumentMaster,
    parse_master,
    parse_master_csv,
)

FETCHED_AT = utc(2026, 8, 19, 4, 0)

FUTURE_ROW = {
    "token": "20001",
    "symbol": "GOLDM04SEP26FUT",
    "name": "GOLDM",
    "exch_seg": "MCX",
    "instrumenttype": "FUTCUR",
    "expiry": "04SEP2026",
    "strike": "",
    "lotsize": "100",
    "tick_size": "0.5",
}

CALL_ROW = {
    "token": "20011",
    "symbol": "GOLDM28AUG26160500CE",
    "name": "GOLDM",
    "exch_seg": "MCX",
    "instrumenttype": "OPTCUR",
    "expiry": "28AUG2026",
    "strike": "160500",
    "lotsize": "100",
    "tick_size": "0.5",
}

PUT_ROW = {
    "token": "20012",
    "symbol": "GOLDM28AUG26153000PE",
    "name": "GOLDM",
    "exch_seg": "MCX",
    "instrumenttype": "OPTCUR",
    "expiry": "28AUG2026",
    "strike": "153000",
    "lotsize": "100",
    "tick_size": "0.5",
}

JUNK_ROWS = [
    {"token": "", "symbol": "EMPTYTOKEN"},
    {"token": "x", "symbol": "NODATE", "expiry": "not-a-date", "name": "GOLDM"},
    {"token": "y", "symbol": "NOSTRIKE", "name": "GOLDM", "expiry": "28AUG2026"},
    {"token": "z", "symbol": "BADLOT", "name": "GOLDM", "expiry": "28AUG2026", "lotsize": "NaN"},
    "not a dict",
    42,
]


def _master(*rows: dict[str, str]) -> InstrumentMaster:
    return InstrumentMaster(parse_master([*rows, *JUNK_ROWS]), fetched_at=FETCHED_AT)


@pytest.fixture
def goldm_future() -> FutureId:
    return FutureId(underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX)


@pytest.fixture
def goldm_call(goldm_future: FutureId) -> OptionId:
    return OptionId(
        underlying_future=goldm_future,
        option_expiry=date(2026, 8, 28),
        strike=Decimal("160500"),
        right=Right.CE,
        exchange=Exchange.MCX,
    )


class TestParseMaster:
    def test_parses_a_contract_row(self) -> None:
        rows = parse_master([CALL_ROW])
        assert len(rows) == 1
        row = rows[0]
        assert row.symboltoken == "20011"
        assert row.tradingsymbol == "GOLDM28AUG26160500CE"
        assert row.expiry == date(2026, 8, 28)
        assert row.strike == Decimal("160500")
        assert row.lot_size == Decimal("100")
        assert row.tick_size == Decimal("0.5")

    def test_skips_unreadable_rows_silently(self) -> None:
        # Only the valid future row and the strike-less row survive; the rest
        # are unparseable or tokenless.
        rows = parse_master([FUTURE_ROW, *JUNK_ROWS])
        assert len(rows) == 2

    def test_expiry_and_strike_default_to_none_when_empty(self) -> None:
        row = parse_master([FUTURE_ROW])[0]
        assert row.strike is None
        assert row.expiry == date(2026, 9, 4)


class TestLookup:
    def test_future_lookup(self, master: InstrumentMaster, goldm_future: FutureId) -> None:
        row = master.row_for(goldm_future)
        assert row.symboltoken == "20001"
        assert row.tradingsymbol == "GOLDM04SEP26FUT"

    def test_option_lookup(self, master: InstrumentMaster, goldm_call: OptionId) -> None:
        row = master.row_for(goldm_call)
        assert row.symboltoken == "20011"

    def test_missing_contract_raises(self, master: InstrumentMaster) -> None:
        unknown = OptionId(
            underlying_future=FutureId(
                underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX
            ),
            option_expiry=date(2026, 8, 28),
            strike=Decimal("170000"),
            right=Right.CE,
            exchange=Exchange.MCX,
        )
        with pytest.raises(DataError, match="no broker contract"):
            master.row_for(unknown)

    def test_ambiguous_snapshot_refuses_to_guess(
        self, goldm_call: OptionId
    ) -> None:
        duplicate = dict(CALL_ROW, token="20999")
        master = _master(CALL_ROW, duplicate)
        with pytest.raises(DataError, match="ambiguous"):
            master.row_for(goldm_call)

    def test_row_by_token(self, master: InstrumentMaster) -> None:
        assert master.row_by_token("20011") is not None
        assert master.row_by_token("99999") is None


class TestExpiries:
    def test_option_expiries_sorted_and_unique(self, master: InstrumentMaster) -> None:
        assert master.option_expiries("GOLDM", Exchange.MCX) == (date(2026, 8, 28),)

    def test_option_rows_are_sorted_by_strike(self, master: InstrumentMaster) -> None:
        rows = master.option_rows("GOLDM", Exchange.MCX, date(2026, 8, 28))
        assert [r.strike for r in rows] == [Decimal("153000"), Decimal("160500")]

    def test_future_rows_nearest_first(self, master: InstrumentMaster) -> None:
        rows = master.future_rows("GOLDM", Exchange.MCX)
        assert [r.expiry for r in rows] == [date(2026, 9, 4)]


class TestSnapshots:
    def test_snapshot_roundtrip(self, tmp_path: Path, master: InstrumentMaster) -> None:
        path = tmp_path / "master.json"
        master.save_snapshot(path)
        restored = InstrumentMaster.from_snapshot(path)
        assert restored.fetched_at == FETCHED_AT
        assert restored.row_by_token("20011") == master.row_by_token("20011")
        assert restored.option_expiries("GOLDM", Exchange.MCX) == (
            date(2026, 8, 28),
        )

    def test_missing_snapshot_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="no instrument master snapshot"):
            InstrumentMaster.from_snapshot(tmp_path / "nope.json")


class TestParseMasterCsv:
    """The Kotak Neo mcx_fo.csv scrip master: p-prefixed columns, scientific
    strikes, epoch expiries, and a header with stray whitespace and a `;`."""

    #: Header verbatim from the 2026-07-14 file (trimmed to the used columns).
    KOTAK_HEADER = (
        "pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,"
        "pScripRefKey,dTickSize ,lLotSize,lExpiryDate ,lMultiplier ,lPrecision,"
        "dStrikePrice;,pExchange,pExpiryDate,iPermittedToTrade,"
        "dGenNum,dGenDen,dPriceNum,dPriceDen,lFreezeQty"
    )

    KOTAK_CSV = "\n".join(
        [
            KOTAK_HEADER,
            # A GOLDM call option, as the real file writes it (scientific strike).
            "578787,,mcx_fo,OPTFUT,GOLDM,GOLDM25SEP26148500CE,CE,,50,100,"
            "1790380799,-1,2,1.485e+07,MCX,1790380799,0,1,1,1,1,10000",
            # A GOLDM futures row.
            "578790,,mcx_fo,FUTCOM,GOLDM,GOLDM25SEP26FUT,,,50,100,"
            "1790380799,1,2,,MCX,1790380799,1,1,1,1,1,10000",
            # Junk rows: no symbol, no token.
            "578791,,mcx_fo,OPTFUT,GOLDM,,CE,,50,100,1790380799,-1,2,"
            "1.486e+07,MCX,1790380799,0,1,1,1,1,10000",
            ",,mcx_fo,OPTFUT,GOLDM,GOLDM25SEP26149000CE,CE,,50,100,"
            "1790380799,-1,2,1.49e+07,MCX,1790380799,0,1,1,1,1,10000",
        ]
    )

    def test_parses_an_option_row(self) -> None:
        rows = parse_master_csv(self.KOTAK_CSV)
        assert len(rows) == 2
        call = rows[0]
        assert call.symboltoken == "578787"
        assert call.tradingsymbol == "GOLDM25SEP26148500CE"
        assert call.exch_seg == "MCX"
        assert call.name == "GOLDM"
        assert call.instrumenttype == "OPTFUT"
        assert call.expiry == date(2026, 9, 25)
        assert call.strike == Decimal("148500")
        assert call.lot_size == Decimal("100")
        assert call.tick_size == Decimal("50")
        assert call.multiplier == Decimal("-1")
        assert call.precision == 2
        assert call.gen_num == Decimal("1")
        assert call.gen_den == Decimal("1")
        assert call.price_num == Decimal("1")
        assert call.price_den == Decimal("1")

    def test_parses_a_futures_row(self) -> None:
        rows = parse_master_csv(self.KOTAK_CSV)
        future = next(r for r in rows if r.instrumenttype == "FUTCOM")
        assert future.strike is None
        assert future.expiry == date(2026, 9, 25)

    def test_skips_tokenless_and_symboless_rows(self) -> None:
        assert len(parse_master_csv(self.KOTAK_CSV)) == 2

    def test_kotak_rows_serve_lookups(self) -> None:
        master = InstrumentMaster(parse_master_csv(self.KOTAK_CSV), fetched_at=FETCHED_AT)
        call = OptionId(
            underlying_future=FutureId(
                underlying="GOLDM", expiry=date(2026, 9, 25), exchange=Exchange.MCX
            ),
            option_expiry=date(2026, 9, 25),
            strike=Decimal("148500"),
            right=Right.CE,
            exchange=Exchange.MCX,
        )
        assert master.row_for(call).symboltoken == "578787"

    def test_row_by_symbol(self, master: InstrumentMaster) -> None:
        assert master.row_by_symbol("GOLDM04SEP26FUT") is not None
        assert master.row_by_symbol("NOTHING") is None


@pytest.fixture
def master() -> InstrumentMaster:
    return _master(FUTURE_ROW, CALL_ROW, PUT_ROW)
