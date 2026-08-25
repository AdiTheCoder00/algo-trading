"""The SmartAPI-to-engine bridge: real 30-minute bars for one live cycle.

Nothing here touches a socket - `FakeTransport` scripts every response by
symboltoken, exactly like `tests/test_smartapi_feed.py`'s fake. The behaviours
that matter are the ones a real account and a real rate limit make expensive to
get wrong by trial and error: the strike band actually bounds what gets fetched
(never a call for a strike outside it), a contract with no bars is skipped and
reported rather than silently dropped, and a transient failure is retried
before the run gives up.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from algo.backtest.smartapi_runner import DEFAULT_MAX_CONTRACTS, build_dataset
from algo.core.enums import Right
from algo.core.errors import DataError, RetryableBrokerError
from algo.core.timeutil import utc
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.master import InstrumentMaster, MasterRow

SYMBOL = "GOLDM"
FUT_TOKEN = "50001"
OPTION_EXPIRY = date(2026, 8, 28)
FUT_EXPIRY = date(2026, 9, 4)

SINCE = utc(2026, 8, 19, 3, 30)  # 09:00 IST
UNTIL = utc(2026, 8, 19, 8, 0)  # 13:30 IST, same session


def _future_row() -> MasterRow:
    return MasterRow(
        symboltoken=FUT_TOKEN,
        tradingsymbol="GOLDM04SEP26FUT",
        exch_seg="MCX",
        name=SYMBOL,
        instrumenttype="FUTCOM",
        expiry=FUT_EXPIRY,
        lot_size=Decimal("100"),
        tick_size=Decimal("0.5"),
    )


def _option_row(token: str, strike: str, right: str) -> MasterRow:
    return MasterRow(
        symboltoken=token,
        tradingsymbol=f"GOLDM28AUG26{strike}{right}",
        exch_seg="MCX",
        name=SYMBOL,
        instrumenttype="OPTFUT",
        expiry=OPTION_EXPIRY,
        strike=Decimal(strike),
        lot_size=Decimal("100"),
        tick_size=Decimal("0.5"),
    )


#: A small ladder: two strikes inside any reasonable band around 156500-157200,
#: and one strike (150000) far enough away that a sane band must exclude it.
CE_NEAR = _option_row("60001", "157000", "CE")
PE_NEAR = _option_row("60002", "157000", "PE")
CE_MID = _option_row("60003", "156500", "CE")
PE_MID = _option_row("60004", "156500", "PE")
CE_FAR = _option_row("60005", "150000", "CE")
PE_FAR = _option_row("60006", "150000", "PE")


def _bar(ts: str, o: float, h: float, lo: float, c: float, v: int) -> list[Any]:
    return [ts, o, h, lo, c, v]


class FakeTransport:
    """Scripts one candle response per symboltoken. Raising a token that was
    never scripted is the point - it is how the strike-band test proves an
    out-of-band strike was never even asked for."""

    def __init__(self) -> None:
        self.by_token: dict[str, list[list[Any]]] = {}
        self.calls: list[str] = []
        self.fail_n_times: dict[str, int] = {}

    def script(self, token: str, rows: list[list[Any]]) -> None:
        self.by_token[token] = rows

    def candles(self, params: dict[str, Any]) -> dict[str, Any]:
        token = params["symboltoken"]
        self.calls.append(token)
        remaining = self.fail_n_times.get(token, 0)
        if remaining > 0:
            self.fail_n_times[token] = remaining - 1
            raise RetryableBrokerError("simulated throttle")
        if token not in self.by_token:
            raise AssertionError(f"unscripted symboltoken {token} was requested")
        return {"status": True, "data": list(self.by_token[token])}


@pytest.fixture
def calendar():  # type: ignore[no-untyped-def]
    return synthetic_calendar()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff is seconds long by design (a real rate limit needs real
    patience). Tests should not spend real wall-clock time on it."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _master(*rows: MasterRow) -> InstrumentMaster:
    return InstrumentMaster(list(rows), fetched_at=SINCE)


class TestTheStrikeBandActuallyBounds:
    def test_a_far_strike_is_never_requested(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        transport.script(
            CE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 700, 750, 690, 720, 40)]
        )
        transport.script(
            PE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 650, 700, 640, 680, 35)]
        )
        master = _master(_future_row(), CE_NEAR, PE_NEAR, CE_FAR, PE_FAR)

        dataset = build_dataset(
            transport,
            master,
            symbol=SYMBOL,
            option_expiry=OPTION_EXPIRY,
            calendar=calendar,
            since=SINCE,
            until=UNTIL,
            strike_band_pct=Decimal("0.02"),  # tight: excludes the 150000 strike
        )

        assert CE_FAR.symboltoken not in transport.calls
        assert PE_FAR.symboltoken not in transport.calls
        assert dataset.contracts_fetched == 2
        assert len(dataset.chain_snapshots) == 1
        assert {r.strike for r in dataset.chain_snapshots[0].rows} == {Decimal("157000")}

    def test_widening_the_band_includes_it(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        for row in (CE_NEAR, PE_NEAR, CE_FAR, PE_FAR):
            transport.script(
                row.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 500, 550, 490, 520, 10)]
            )
        master = _master(_future_row(), CE_NEAR, PE_NEAR, CE_FAR, PE_FAR)

        dataset = build_dataset(
            transport,
            master,
            symbol=SYMBOL,
            option_expiry=OPTION_EXPIRY,
            calendar=calendar,
            since=SINCE,
            until=UNTIL,
            strike_band_pct=Decimal("0.10"),  # wide enough for 150000 too
        )

        assert CE_FAR.symboltoken in transport.calls
        assert dataset.contracts_fetched == 4


class TestEmptyContractsAreSkippedNotSilent:
    def test_a_contract_with_no_bars_is_reported(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        transport.script(CE_NEAR.symboltoken, [])  # never traded in this window
        transport.script(
            PE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 650, 700, 640, 680, 35)]
        )
        master = _master(_future_row(), CE_NEAR, PE_NEAR)

        dataset = build_dataset(
            transport,
            master,
            symbol=SYMBOL,
            option_expiry=OPTION_EXPIRY,
            calendar=calendar,
            since=SINCE,
            until=UNTIL,
            strike_band_pct=Decimal("0.02"),
        )

        assert dataset.contracts_skipped_empty == [CE_NEAR.tradingsymbol]
        assert dataset.contracts_fetched == 1
        # The one leg that did trade still forms a snapshot on its own.
        assert len(dataset.chain_snapshots) == 1
        assert len(dataset.chain_snapshots[0].rows) == 1


class TestNoFuturesBarsRefusesRatherThanInventingAChain:
    def test_raises_a_clear_error(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(FUT_TOKEN, [])
        master = _master(_future_row(), CE_NEAR, PE_NEAR)

        with pytest.raises(DataError, match="nothing to build a"):
            build_dataset(
                transport,
                master,
                symbol=SYMBOL,
                option_expiry=OPTION_EXPIRY,
                calendar=calendar,
                since=SINCE,
                until=UNTIL,
            )


class TestTheContractCountGuard:
    def test_refuses_past_max_contracts_without_being_asked(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        master = _master(_future_row(), CE_NEAR, PE_NEAR, CE_MID, PE_MID)

        with pytest.raises(DataError, match="max_contracts"):
            build_dataset(
                transport,
                master,
                symbol=SYMBOL,
                option_expiry=OPTION_EXPIRY,
                calendar=calendar,
                since=SINCE,
                until=UNTIL,
                strike_band_pct=Decimal("0.02"),
                max_contracts=1,
            )

    def test_the_default_is_generous_enough_for_a_realistic_ladder(self) -> None:
        assert DEFAULT_MAX_CONTRACTS >= 100


class TestRetryOnThrottling:
    def test_a_transient_failure_is_retried_and_succeeds(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        transport.script(
            CE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 700, 750, 690, 720, 40)]
        )
        transport.script(
            PE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 650, 700, 640, 680, 35)]
        )
        transport.fail_n_times[CE_NEAR.symboltoken] = 2  # fails twice, then serves the script
        master = _master(_future_row(), CE_NEAR, PE_NEAR)

        dataset = build_dataset(
            transport,
            master,
            symbol=SYMBOL,
            option_expiry=OPTION_EXPIRY,
            calendar=calendar,
            since=SINCE,
            until=UNTIL,
            strike_band_pct=Decimal("0.02"),
        )

        assert dataset.contracts_fetched == 2
        assert transport.calls.count(CE_NEAR.symboltoken) == 3  # 2 failures + 1 success

    def test_exhausting_every_retry_raises_clearly(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        transport.fail_n_times[FUT_TOKEN] = 99  # never recovers

        master = _master(_future_row())
        with pytest.raises(DataError, match="kept failing"):
            build_dataset(
                transport,
                master,
                symbol=SYMBOL,
                option_expiry=OPTION_EXPIRY,
                calendar=calendar,
                since=SINCE,
                until=UNTIL,
            )


class TestTheChainIsPriced:
    def test_deltas_are_solved_not_left_none(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        transport.script(
            CE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 700, 750, 690, 720, 40)]
        )
        transport.script(
            PE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 650, 700, 640, 680, 35)]
        )
        master = _master(_future_row(), CE_NEAR, PE_NEAR)

        dataset = build_dataset(
            transport,
            master,
            symbol=SYMBOL,
            option_expiry=OPTION_EXPIRY,
            calendar=calendar,
            since=SINCE,
            until=UNTIL,
            strike_band_pct=Decimal("0.02"),
        )

        snapshot = dataset.chain_snapshots[0]
        call = next(r for r in snapshot.rows if r.right is Right.CE)
        assert call.delta is not None
        assert call.iv is not None
        assert call.is_tradeable

    def test_the_expiry_table_resolves_the_right_futures_contract(self, calendar) -> None:  # type: ignore[no-untyped-def]
        transport = FakeTransport()
        transport.script(
            FUT_TOKEN, [_bar("2026-08-19T09:30:00+05:30", 156800, 157200, 156600, 157000, 900)]
        )
        transport.script(
            CE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 700, 750, 690, 720, 40)]
        )
        transport.script(
            PE_NEAR.symboltoken, [_bar("2026-08-19T09:30:00+05:30", 650, 700, 640, 680, 35)]
        )
        master = _master(_future_row(), CE_NEAR, PE_NEAR)

        dataset = build_dataset(
            transport,
            master,
            symbol=SYMBOL,
            option_expiry=OPTION_EXPIRY,
            calendar=calendar,
            since=SINCE,
            until=UNTIL,
            strike_band_pct=Decimal("0.02"),
        )

        cycle = dataset.expiries.expiry_set(SYMBOL, OPTION_EXPIRY.year, OPTION_EXPIRY.month)
        assert cycle.option_expiry == OPTION_EXPIRY
        assert cycle.futures_expiry == FUT_EXPIRY
        assert dataset.instrument.expiry == FUT_EXPIRY
