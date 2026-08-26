"""MCX bhavcopy loading. Daily, strike-wise, and it covers expired contracts.

Two things carry most of the weight here.

**A mismatched header must fail loudly.** MCX serves the file through a browser
flow behind bot protection, so the column mapping shipped in the code is a stated
assumption rather than a checked fact. A file silently read against the wrong
columns is the worst possible outcome — every downstream number would be
confidently wrong — so the parser refuses and prints what it actually found.

**A zero-volume strike must not become tradeable.** End-of-day data has no book,
so `assume_spread` invents one; the point of the tests below is that it only
invents a book where something genuinely changed hands. On a thin option ladder,
"listed but nobody traded it" is the most valuable thing this dataset says.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.enums import Right
from algo.core.errors import DataError
from algo.core.timeutil import to_ist
from algo.data.bhavcopy import (
    MCX_DEFAULT_COLUMNS,
    BhavcopyChainFeed,
    BhavcopyColumns,
    assume_spread,
    build_snapshots,
    coverage,
    futures_close,
    load_directory,
    parse_rows,
)

HEADER = (
    "Date,Instrument Name,Symbol,Expiry Date,Strike Price,Option Type,"
    "Open,High,Low,Close,Previous Close,Volume,Value,Open Interest"
)


def _row(
    *,
    day: str = "20-Aug-2026",
    kind: str = "OPTFUT",
    symbol: str = "GOLDM",
    expiry: str = "28-Aug-2026",
    strike: str = "160500",
    right: str = "CE",
    close: str = "756.00",
    volume: str = "40",
    oi: str = "120",
) -> str:
    return (
        f"{day},{kind},{symbol},{expiry},{strike},{right},"
        f"{close},{close},{close},{close},{close},{volume},1.5,{oi}"
    )


def _future(day: str = "20-Aug-2026", close: str = "156640", expiry: str = "04-Sep-2026") -> str:
    return f"{day},FUTCOM,GOLDM,{expiry},,,{close},{close},{close},{close},{close},900,140.2,4500"


def _write(tmp_path: Path, *rows: str, name: str = "bhav.csv") -> Path:
    path = tmp_path / name
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


class TestTheMappingFailsLoudly:
    """The mapping is an assumption; it must behave like one."""

    def test_a_wrong_header_names_what_it_found(self, tmp_path: Path) -> None:
        path = tmp_path / "other.csv"
        path.write_text("TradeDate,Ticker,Settle\n20-Aug-2026,GOLDM,156640\n", encoding="utf-8")

        with pytest.raises(DataError) as caught:
            parse_rows(path)

        message = str(caught.value)
        # Wording changed when auto-detection landed (D-105): the parser now
        # tries several layouts, so it can no longer speak of "the expected" one.
        assert "does not match any known bhavcopy layout" in message
        assert "TradeDate" in message, "it must show what the file actually has"
        assert "Instrument Name" in message, "and what it expected"
        assert "unverified assumption" in message

    def test_a_corrected_mapping_is_a_config_change_not_a_code_change(self, tmp_path: Path) -> None:
        """The whole reason the mapping is data."""
        path = tmp_path / "renamed.csv"
        path.write_text(
            "TRADE_DT,INSTRUMENT,SYM,EXPIRY,STRIKE,OPT,O,H,L,C,VOL,OI\n"
            "20-Aug-2026,OPTFUT,GOLDM,28-Aug-2026,160500,CE,756,756,756,756,40,120\n",
            encoding="utf-8",
        )
        remapped = BhavcopyColumns(
            trade_date="TRADE_DT",
            instrument_kind="INSTRUMENT",
            symbol="SYM",
            expiry="EXPIRY",
            strike="STRIKE",
            option_type="OPT",
            open="O",
            high="H",
            low="L",
            close="C",
            volume="VOL",
            open_interest="OI",
        )
        rows = parse_rows(path, columns=remapped)
        assert len(rows) == 1
        assert rows[0].strike == Decimal("160500")

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="bhavcopy not found"):
            parse_rows(tmp_path / "nope.csv")

    def test_a_bad_row_reports_its_line(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _row(), _row(close="not-a-price"))
        with pytest.raises(DataError, match=r"bhav\.csv:3"):
            parse_rows(path)


class TestParsing:
    def test_options_and_futures_are_distinguished(self, tmp_path: Path) -> None:
        rows = parse_rows(_write(tmp_path, _future(), _row()))
        assert len(rows) == 2
        assert [r.is_option for r in rows] == [False, True]

    def test_non_derivative_rows_are_skipped_quietly(self, tmp_path: Path) -> None:
        """Spot and index rows share the file; skipping them is expected."""
        spot = "20-Aug-2026,SPOT,GOLDM,,,,156600,156600,156600,156600,156600,0,0,0"
        rows = parse_rows(_write(tmp_path, spot, _row()))
        assert len(rows) == 1

    def test_prices_are_decimal_not_float(self, tmp_path: Path) -> None:
        """Brief §2.5, at the point data enters the system."""
        rows = parse_rows(_write(tmp_path, _row(close="156640.05")))
        assert rows[0].close == Decimal("156640.05")
        assert str(rows[0].close) == "156640.05"

    def test_thousands_separators_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "commas.csv"
        path.write_text(
            HEADER
            + "\n20-Aug-2026,OPTFUT,GOLDM,28-Aug-2026,160500,CE,"
            + '"1,756.50","1,756.50","1,756.50","1,756.50","1,756.50","1,240",1.5,"12,000"\n',
            encoding="utf-8",
        )
        rows = parse_rows(path)
        assert rows[0].close == Decimal("1756.50")
        assert rows[0].volume == 1240
        assert rows[0].open_interest == 12000

    @pytest.mark.parametrize(
        "written", ["28-Aug-2026", "28AUG2026", "2026-08-28", "28/08/2026", "28-08-2026"]
    )
    def test_date_formats(self, tmp_path: Path, written: str) -> None:
        rows = parse_rows(_write(tmp_path, _row(expiry=written)))
        assert rows[0].expiry == date(2026, 8, 28)

    def test_an_unknown_date_format_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="not a date in any known format"):
            parse_rows(_write(tmp_path, _row(expiry="August 28th")))

    def test_symbol_filtering(self, tmp_path: Path) -> None:
        silver = _row(symbol="SILVERM", strike="90000")
        rows = parse_rows(_write(tmp_path, _row(), silver), symbols=frozenset({"GOLDM"}))
        assert len(rows) == 1
        assert rows[0].symbol == "GOLDM"

    def test_traded_reflects_volume(self, tmp_path: Path) -> None:
        rows = parse_rows(_write(tmp_path, _row(volume="0"), _row(strike="161000", volume="5")))
        assert [r.traded for r in rows] == [False, True]


class TestSnapshots:
    def _rows(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        return parse_rows(
            _write(
                tmp_path,
                _future(),
                _row(strike="160500", right="CE", close="756.00"),
                _row(strike="160500", right="PE", close="12.00", volume="0"),
                _row(strike="153000", right="PE", close="769.00"),
                _row(strike="153000", right="CE", close="9.50", volume="0"),
            )
        )

    def test_one_snapshot_per_date_and_expiry(self, tmp_path: Path) -> None:
        snapshots = build_snapshots(self._rows(tmp_path), symbol="GOLDM")
        assert len(snapshots) == 1
        assert snapshots[0].option_expiry == date(2026, 8, 28)
        assert len(snapshots[0].rows) == 4

    def test_the_futures_close_becomes_the_forward(self, tmp_path: Path) -> None:
        """Options are priced off the future; without it every delta is invented."""
        assert build_snapshots(self._rows(tmp_path), symbol="GOLDM")[0].futures_price == Decimal(
            "156640"
        )

    def test_a_chain_with_no_futures_close_is_dropped(self, tmp_path: Path) -> None:
        rows = parse_rows(_write(tmp_path, _row()))
        assert build_snapshots(rows, symbol="GOLDM") == []

    def test_it_is_stamped_at_the_session_close_not_midnight(self, tmp_path: Path) -> None:
        """These are closing prices; dating them to 00:00 would put the whole
        chain before the market that produced it."""
        snapshot = build_snapshots(self._rows(tmp_path), symbol="GOLDM")[0]
        assert to_ist(snapshot.ts).strftime("%H:%M") == "23:30"
        assert to_ist(snapshot.ts).date() == date(2026, 8, 20)

    def test_the_underlying_future_can_be_resolved_explicitly(self, tmp_path: Path) -> None:
        """Q1c — the default is a heuristic and is meant to be overridden."""
        snapshot = build_snapshots(
            self._rows(tmp_path),
            symbol="GOLDM",
            resolve_underlying=lambda _: date(2026, 10, 5),
        )[0]
        assert snapshot.rows[0].option.underlying_future.expiry == date(2026, 10, 5)

    def test_the_default_pairs_with_the_next_futures_expiry(self, tmp_path: Path) -> None:
        snapshot = build_snapshots(self._rows(tmp_path), symbol="GOLDM")[0]
        assert snapshot.rows[0].option.underlying_future.expiry == date(2026, 9, 4)

    def test_rows_arrive_in_deterministic_order(self, tmp_path: Path) -> None:
        rows = build_snapshots(self._rows(tmp_path), symbol="GOLDM")[0].rows
        assert [(r.strike, r.right.value) for r in rows] == sorted(
            (r.strike, r.right.value) for r in rows
        )

    def test_volume_and_open_interest_survive(self, tmp_path: Path) -> None:
        snapshot = build_snapshots(self._rows(tmp_path), symbol="GOLDM")[0]
        call = snapshot.by_strike(Decimal("160500"), Right.CE)
        assert call is not None
        assert call.quote.volume == 40
        assert call.quote.open_interest == 120


class TestTheForward:
    """Every delta in a snapshot is computed against this number."""

    def _rows(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        return parse_rows(
            _write(
                tmp_path,
                _future(expiry="04-Sep-2026", close="156640"),
                _future(expiry="05-Oct-2026", close="157880"),
                _row(),
            )
        )

    def test_it_takes_the_nearest_expiry(self, tmp_path: Path) -> None:
        """The far month trades at a different price; picking it would shift
        every strike's moneyness."""
        close = futures_close(self._rows(tmp_path), symbol="GOLDM", on=date(2026, 8, 20))
        assert close == Decimal("156640")

    def test_a_day_with_no_futures_row_returns_none_rather_than_a_guess(
        self, tmp_path: Path
    ) -> None:
        assert futures_close(self._rows(tmp_path), symbol="GOLDM", on=date(2026, 8, 19)) is None

    def test_another_symbol_does_not_leak_in(self, tmp_path: Path) -> None:
        rows = parse_rows(
            _write(
                tmp_path,
                "20-Aug-2026,FUTCOM,SILVERM,05-Sep-2026,,,90000,90000,90000,90000,90000,10,1,2",
                _future(),
            )
        )
        assert futures_close(rows, symbol="GOLDM", on=date(2026, 8, 20)) == Decimal("156640")


class TestAssumingASpread:
    """It invents data, so the tests are about *where it refuses to*."""

    def _snapshot(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        rows = parse_rows(
            _write(
                tmp_path,
                _future(),
                _row(strike="160500", right="CE", close="756.00", volume="40"),
                _row(strike="163000", right="CE", close="120.00", volume="0"),
            )
        )
        return build_snapshots(rows, symbol="GOLDM")[0]

    def test_raw_bhavcopy_rows_are_not_tradeable(self, tmp_path: Path) -> None:
        """There is no book in the file, so nothing should look fillable."""
        snapshot = self._snapshot(tmp_path)
        assert all(r.quote.bid is None and r.quote.ask is None for r in snapshot.rows)
        assert not any(r.quote.is_tradeable for r in snapshot.rows)

    def test_a_spread_is_only_assumed_where_something_traded(self, tmp_path: Path) -> None:
        """A strike with zero volume was listed, not available. That distinction
        is the most valuable thing this dataset has to say about a thin ladder."""
        priced = assume_spread(self._snapshot(tmp_path), half_spread=Decimal("5"))

        traded = priced.by_strike(Decimal("160500"), Right.CE)
        untraded = priced.by_strike(Decimal("163000"), Right.CE)
        assert traded is not None and untraded is not None

        assert traded.quote.bid == Decimal("751.00")
        assert traded.quote.ask == Decimal("761.00")
        assert untraded.quote.bid is None, "a zero-volume strike must stay unquoted"

    def test_the_minimum_volume_is_configurable(self, tmp_path: Path) -> None:
        priced = assume_spread(self._snapshot(tmp_path), half_spread=Decimal("5"), min_volume=100)
        assert all(r.quote.bid is None for r in priced.rows), "40 lots is below the floor"

    def test_a_bid_never_goes_negative(self, tmp_path: Path) -> None:
        rows = parse_rows(_write(tmp_path, _future(), _row(close="2.00", volume="10")))
        priced = assume_spread(build_snapshots(rows, symbol="GOLDM")[0], half_spread=Decimal("50"))
        assert priced.rows[0].quote.bid == Decimal("0.05")

    def test_a_negative_spread_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="cannot be negative"):
            assume_spread(self._snapshot(tmp_path), half_spread=Decimal("-1"))


class TestFeedAndCoverage:
    def test_the_feed_serves_snapshots_by_expiry_in_time_order(self, tmp_path: Path) -> None:
        rows = parse_rows(
            _write(
                tmp_path,
                _future(day="19-Aug-2026"),
                _row(day="19-Aug-2026"),
                _future(day="20-Aug-2026"),
                _row(day="20-Aug-2026"),
            )
        )
        feed = BhavcopyChainFeed(build_snapshots(rows, symbol="GOLDM"), underlying="GOLDM")
        series = list(feed.snapshots(date(2026, 8, 28)))
        assert len(series) == 2
        assert series[0].ts < series[1].ts
        assert feed.expiries() == (date(2026, 8, 28),)

    def test_an_unknown_expiry_yields_nothing(self, tmp_path: Path) -> None:
        feed = BhavcopyChainFeed([], underlying="GOLDM")
        assert list(feed.snapshots(date(2026, 8, 28))) == []

    def test_coverage_reports_what_actually_traded(self, tmp_path: Path) -> None:
        """The first thing to look at: a hundred cycles of history is only worth
        having if the strikes the strategy wants were changing hands."""
        rows = parse_rows(
            _write(
                tmp_path,
                _future(),
                _row(strike="160500", volume="40"),
                _row(strike="163000", volume="0"),
                _row(strike="165000", volume="0"),
                _row(strike="167000", volume="0"),
            )
        )
        report = coverage(rows, symbol="GOLDM")
        assert "4 option rows" in report
        assert "1 expiries" in report
        assert "25.0% of the ladder" in report

    def test_coverage_says_when_there_is_nothing(self, tmp_path: Path) -> None:
        assert "no GOLDM option rows found" in coverage([], symbol="GOLDM")

    def test_a_directory_loads_in_filename_order(self, tmp_path: Path) -> None:
        _write(tmp_path, _future(day="19-Aug-2026"), _row(day="19-Aug-2026"), name="a.csv")
        _write(tmp_path, _future(day="20-Aug-2026"), _row(day="20-Aug-2026"), name="b.csv")
        rows = load_directory(tmp_path, symbol="GOLDM")
        assert [r.trade_date for r in rows if r.is_option] == [
            date(2026, 8, 19),
            date(2026, 8, 20),
        ]

    def test_an_empty_directory_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(DataError, match="no files matching"):
            load_directory(tmp_path / "empty", symbol="GOLDM")


class TestTheDefaultMappingIsMarkedUnverified:
    def test_the_module_says_so(self) -> None:
        """If this ever becomes verified, the docstring and this test change
        together — deliberately."""
        import algo.data.bhavcopy as module

        assert "unverified" in (module.__doc__ or "").lower()
        assert MCX_DEFAULT_COLUMNS.trade_date == "Date"
