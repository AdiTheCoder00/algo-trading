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
    MasterRow,
    parse_master,
    parse_master_csv,
)

FETCHED_AT = utc(2026, 8, 19, 4, 0)

#: Strike and tick size below are written the way Angel One's real master
#: actually serves them for MCX - scaled by 100 - not the real rupee value.
#: See `_ANGEL_ONE_PRICE_SCALE` in algo/exchange/master.py for the live
#: evidence this scale rests on. `parse_master` divides it back out; every
#: assertion below checks the real, descaled value.
FUTURE_ROW = {
    "token": "20001",
    "symbol": "GOLDM04SEP26FUT",
    "name": "GOLDM",
    "exch_seg": "MCX",
    "instrumenttype": "FUTCUR",
    "expiry": "04SEP2026",
    "strike": "",
    "lotsize": "100",
    "tick_size": "50.000000",
}

CALL_ROW = {
    "token": "20011",
    "symbol": "GOLDM28AUG26160500CE",
    "name": "GOLDM",
    "exch_seg": "MCX",
    "instrumenttype": "OPTCUR",
    "expiry": "28AUG2026",
    "strike": "16050000.000000",
    "lotsize": "100",
    "tick_size": "50.000000",
}

PUT_ROW = {
    "token": "20012",
    "symbol": "GOLDM28AUG26153000PE",
    "name": "GOLDM",
    "exch_seg": "MCX",
    "instrumenttype": "OPTCUR",
    "expiry": "28AUG2026",
    "strike": "15300000.000000",
    "lotsize": "100",
    "tick_size": "50.000000",
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

    def test_a_strike_matching_the_tradingsymbol_survives_x100_and_back(self) -> None:
        """The regression this file exists to catch: `parse_master` must return
        the strike the tradingsymbol itself states (GOLDM...157000CE means
        157000), not the x100 figure Angel One's real API serves it as. Confirmed
        live on 2026-08-25 against the actual master file - `state/master_mcx.json`
        held `"strike": "15700000.000000"` for that exact contract before this
        fix, silently 100x too large, unnoticed because nothing had constructed
        an `OptionId` from a live Angel One row until now."""
        row = parse_master(
            [
                {
                    "token": "575067",
                    "symbol": "GOLDM28AUG26157000CE",
                    "name": "GOLDM",
                    "exch_seg": "MCX",
                    "instrumenttype": "OPTFUT",
                    "expiry": "28AUG2026",
                    "strike": "15700000.000000",
                    "lotsize": "100",
                    "tick_size": "50.000000",
                }
            ]
        )[0]
        assert row.strike == Decimal("157000")
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


class TestFutureForOptionExpiry:
    """The one pairing rule: the contract an option cycle settles into.

    Every master here lists three futures so that the front month, the farthest
    contract and the correct answer are three *different* dates. A fixture with a
    single futures row — which is what `master` above has — cannot tell them apart,
    which is exactly how `KotakChainFeed` shipped anchoring its chain on the
    farthest listed contract while its comment claimed the nearest.
    """

    OPTION_EXPIRY = date(2026, 9, 25)

    def _master(self, *expiries: date) -> InstrumentMaster:
        return InstrumentMaster(
            [
                MasterRow(
                    symboltoken=str(index),
                    tradingsymbol=f"GOLDM{expiry:%d%b%y}FUT".upper(),
                    exch_seg="MCX",
                    name="GOLDM",
                    instrumenttype="FUTCOM",
                    expiry=expiry,
                    lot_size=Decimal("100"),
                )
                for index, expiry in enumerate(expiries)
            ],
            fetched_at=FETCHED_AT,
        )

    def test_it_is_neither_the_front_month_nor_the_farthest(self) -> None:
        master = self._master(date(2026, 8, 28), date(2026, 9, 30), date(2026, 10, 30))
        rows = master.future_rows("GOLDM", Exchange.MCX)

        row = master.future_for_option_expiry("GOLDM", Exchange.MCX, self.OPTION_EXPIRY)

        assert row is not None
        assert row.expiry == date(2026, 9, 30)
        assert row.expiry != rows[0].expiry  # not the front month
        assert row.expiry != rows[-1].expiry  # not the farthest — the old behaviour

    def test_a_contract_expiring_on_the_option_date_still_counts(self) -> None:
        """"On or after" is inclusive: MCX lists cycles whose option and future
        share a date, and excluding it would skip to the next month."""
        master = self._master(date(2026, 9, 25), date(2026, 10, 30))
        row = master.future_for_option_expiry("GOLDM", Exchange.MCX, self.OPTION_EXPIRY)
        assert row is not None and row.expiry == date(2026, 9, 25)

    def test_an_incomplete_master_falls_back_to_the_farthest_listed(self) -> None:
        """Every listed contract expires before the option, so none of them is one
        the option can settle into. The farthest is the least wrong, and picking an
        earlier one would be actively misleading."""
        master = self._master(date(2026, 7, 31), date(2026, 8, 28))
        row = master.future_for_option_expiry("GOLDM", Exchange.MCX, self.OPTION_EXPIRY)
        assert row is not None and row.expiry == date(2026, 8, 28)

    def test_no_futures_at_all_is_none_rather_than_a_guess(self) -> None:
        master = self._master()
        assert master.future_for_option_expiry("GOLDM", Exchange.MCX, self.OPTION_EXPIRY) is None


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
