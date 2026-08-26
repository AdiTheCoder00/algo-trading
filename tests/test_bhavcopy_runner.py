"""Turning bhavcopy rows into a runnable dataset (D-115).

This module produced the only real-data backtest result the project has - six
GOLDM cycles, Jan-Jun 2026 - and had no tests of its own. Its sibling
`smartapi_runner` has ten.

The properties pinned here are the ones that would silently change that result:
which sessions are dropped and why, that a session becomes exactly two bars, and
that the two bars mean what the strategy assumes they mean.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from algo.backtest.bhavcopy_runner import ENTRY_TIME_IST, build_dataset
from algo.core.enums import Right
from algo.core.errors import DataError
from algo.core.timeutil import to_ist
from algo.data.bhavcopy import BhavcopyRow
from algo.exchange.calendar import synthetic_calendar

CAL = synthetic_calendar()
DAY = date(2026, 8, 19)
OPT_EXPIRY = date(2026, 8, 28)
FUT_EXPIRY = date(2026, 9, 4)


def _future(day: date = DAY, close: str = "156640") -> BhavcopyRow:
    return BhavcopyRow(
        trade_date=day,
        symbol="GOLDM",
        expiry=FUT_EXPIRY,
        is_option=False,
        strike=None,
        right=None,
        open=Decimal("156000"),
        high=Decimal("157000"),
        low=Decimal("155500"),
        close=Decimal(close),
        volume=5000,
        open_interest=1000,
    )


def _option(strike: str, right: Right, *, day: date = DAY, volume: int = 500) -> BhavcopyRow:
    return BhavcopyRow(
        trade_date=day,
        symbol="GOLDM",
        expiry=OPT_EXPIRY,
        is_option=True,
        strike=Decimal(strike),
        right=right,
        open=Decimal("1200"),
        high=Decimal("1300"),
        low=Decimal("1100"),
        close=Decimal("1250"),
        volume=volume,
        open_interest=200,
    )


def _ladder(day: date = DAY) -> list[BhavcopyRow]:
    rows = [_future(day)]
    for strike in ("154000", "156000", "158000"):
        rows.append(_option(strike, Right.CE, day=day))
        rows.append(_option(strike, Right.PE, day=day))
    return rows


def _build(rows: list[BhavcopyRow], **kwargs: object):
    return build_dataset(rows, symbol="GOLDM", calendar=CAL, **kwargs)  # type: ignore[arg-type]


class TestASessionBecomesTwoBars:
    """D-081..D-085: bhavcopy is end-of-day, so a session is an entry tick and a
    close tick. Any other count means the engine is being told something the data
    does not contain."""

    def test_exactly_two_bars_per_session(self) -> None:
        dataset = _build(_ladder())

        assert len(dataset.bars) == 2
        assert dataset.sessions_used == 1

    def test_the_first_bar_is_the_entry_time_priced_from_the_open(self) -> None:
        """The strategy enters at 09:30; bhavcopy has no 09:30 print, so the
        day's open stands in. If this drifts, every entry price is wrong."""
        entry = _build(_ladder()).bars[0]

        assert to_ist(entry.ts).time() == ENTRY_TIME_IST == time(9, 30)
        assert entry.open == entry.high == entry.low == Decimal("156000")

    def test_the_second_bar_is_the_real_session_close(self) -> None:
        close_bar = _build(_ladder()).bars[1]

        assert close_bar.ts == CAL.session_close(DAY)
        assert close_bar.close == Decimal("156640")

    def test_the_bars_are_ordered(self) -> None:
        bars = _build(_ladder()).bars

        assert bars[0].ts < bars[1].ts


class TestSessionsAreDroppedLoudly:
    """Silently dropping a session shortens the backtest without saying so."""

    def test_a_session_with_no_futures_row_is_dropped_and_named(self) -> None:
        """The exact case the real July data hit: options present, no futures,
        so there is no forward and every delta would be invented. One good
        session alongside it, or the whole build refuses before reporting."""
        bad_day = date(2026, 8, 20)
        rows = _ladder(DAY) + [r for r in _ladder(bad_day) if r.is_option]

        dataset = _build(rows)

        assert dataset.sessions_used == 1
        assert len(dataset.skipped_sessions) == 1
        assert str(bad_day) in dataset.skipped_sessions[0]
        assert "no futures row" in dataset.skipped_sessions[0]

    def test_a_dataset_with_nothing_usable_refuses(self) -> None:
        with pytest.raises(DataError):
            _build([r for r in _ladder() if r.is_option])

    def test_a_good_session_is_not_dropped(self) -> None:
        assert _build(_ladder()).skipped_sessions == []


class TestTheChainThatComesOut:
    def test_two_snapshots_per_session_one_for_each_bar(self) -> None:
        """One per bar, so a position can be marked at both. A single snapshot
        would mark the close against the open's prices."""
        dataset = _build(_ladder())

        assert len(dataset.chain_snapshots) == 2
        assert [s.ts for s in dataset.chain_snapshots] == [b.ts for b in dataset.bars]

    def test_each_snapshot_is_priced_from_its_own_bar(self) -> None:
        """Every delta depends on the forward; taking it from the wrong row
        would move every strike the strategy picks. The entry snapshot uses the
        day's open, the close snapshot its close."""
        entry, close = _build(_ladder()).chain_snapshots

        assert entry.futures_price == Decimal("156000")  # the open
        assert close.futures_price == Decimal("156640")  # the close
        assert entry.option_expiry == close.option_expiry == OPT_EXPIRY

    def test_greeks_are_populated(self) -> None:
        """`DeltaStrangle` selects by delta - an unenriched chain would make it
        silently never trade."""
        snapshot = _build(_ladder()).chain_snapshots[0]

        assert any(row.delta is not None for row in snapshot.rows)

    def test_min_volume_decides_what_is_quotable(self) -> None:
        """Bhavcopy has no book, so day volume is the only tradeability gate
        there is - raising it must actually remove strikes."""
        rows = [_future(), _option("156000", Right.CE, volume=1)]
        rows.append(_option("156000", Right.PE, volume=1))

        permissive = _build(rows, min_volume=1).chain_snapshots[0]
        strict = _build(rows, min_volume=1000).chain_snapshots[0]

        assert sum(r.is_tradeable for r in permissive.rows) > sum(
            r.is_tradeable for r in strict.rows
        )


class TestMultipleSessions:
    def test_each_session_contributes_its_own_pair_of_bars(self) -> None:
        rows = _ladder(DAY) + _ladder(date(2026, 8, 20))

        dataset = _build(rows)

        assert dataset.sessions_used == 2
        assert len(dataset.bars) == 4
        assert len(dataset.chain_snapshots) == 4  # two per session

    def test_the_instrument_is_the_futures_contract(self) -> None:
        dataset = _build(_ladder())

        assert dataset.instrument.underlying == "GOLDM"
        assert dataset.instrument.expiry == FUT_EXPIRY
