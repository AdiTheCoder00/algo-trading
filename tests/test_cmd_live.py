"""The decision logic inside `algo live`, exercised without a broker.

`tests/test_cli.py` runs the command itself, but stops at the refusal paths on
purpose: `live` opens real sessions, and a suite that logs into a broker is a
suite nobody can run. That leaves the helpers underneath it - which is where the
decisions actually are - unexercised, and D-117 records what that costs. Wiring
`flatten_on_trip` shipped an `UnboundLocalError` on the no-config path that a
790-test suite passed straight over, because no test imported the module.

Both helpers here take their transport as an argument, so they can be driven with
a fake. What they decide is which *contract* the run reads: `future_rows` sorts
ascending by expiry, and picking the wrong end of that list points a live session
at a barely-traded back month. That bug was real - three call sites used
`futures[-1]` while their comments said "nearest" - and nothing could see it,
because both fixtures in the suite listed a single futures contract, making
`[0]` and `[-1]` the same row.

So the fixture here lists two, and the assertion is on the token actually
requested rather than on the printed output. The request is the decision; the
message is only a report of it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from algo.cli.cmd_live import _bars_from_candles, _run_paper_loop
from algo.config.loader import load_config
from algo.core.clock import BacktestClock
from algo.core.enums import Mode
from algo.core.errors import DomainError
from algo.core.timeutil import utc
from algo.exchange.master import InstrumentMaster, MasterRow

REFERENCE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "goldm.yaml"
#: 13:30 IST on a Wednesday - mid-session, so the feed actually requests
#: candles. A real clock would make these tests pass or fail by time of day.
NOW = utc(2026, 8, 19, 8, 0)
FETCHED_AT = utc(2026, 8, 19, 4, 0)

FRONT_TOKEN = "front-month-token"
BACK_TOKEN = "back-month-token"


def _future(token: str, symbol: str, expiry: date) -> MasterRow:
    return MasterRow(
        symboltoken=token,
        tradingsymbol=symbol,
        exch_seg="MCX",
        name="GOLDM",
        instrumenttype="FUTCOM",
        expiry=expiry,
        lot_size=Decimal("10"),
        tick_size=Decimal("1"),
    )


#: Two months, so the front and the back are different rows. A single-contract
#: master cannot tell a correct choice from the opposite one.
TWO_MONTHS = [
    _future(FRONT_TOKEN, "GOLDM25SEPFUT", date(2026, 9, 30)),
    _future(BACK_TOKEN, "GOLDM25OCTFUT", date(2026, 10, 31)),
]


class FakeDataTransport:
    """The candle surface, scripted. Records what was asked for."""

    def __init__(self, *, rows: list[list[Any]] | None = None, boom: bool = False) -> None:
        self.rows = rows or []
        self.boom = boom
        self.last_params: dict[str, Any] | None = None
        self.calls = 0

    def candles(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.last_params = params
        if self.boom:
            raise ConnectionError("smartapi is down")
        return {"status": True, "data": list(self.rows)}


@pytest.fixture
def config() -> Any:
    return load_config(REFERENCE_CONFIG, env={})


@pytest.fixture
def master() -> InstrumentMaster:
    return InstrumentMaster(list(TWO_MONTHS), fetched_at=FETCHED_AT)


@pytest.fixture
def empty_master() -> InstrumentMaster:
    """Options only - a snapshot with nothing to trade the underlying with."""
    return InstrumentMaster(
        [
            MasterRow(
                symboltoken="opt",
                tradingsymbol="GOLDM25SEP150000CE",
                exch_seg="MCX",
                name="GOLDM",
                instrumenttype="OPTFUT",
                expiry=date(2026, 9, 25),
                strike=Decimal("150000"),
                lot_size=Decimal("10"),
            )
        ],
        fetched_at=FETCHED_AT,
    )


class TestTheCandleProof:
    def test_it_reads_the_front_month_not_the_back(
        self, config: Any, master: InstrumentMaster, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The decision this exists to pin. `future_rows` is sorted ascending, so
        the front month is [0]; the back month is what the bug used to pick, and
        it is the contract that barely trades."""
        transport = FakeDataTransport()

        _bars_from_candles(transport, master, config, BacktestClock(NOW))

        assert transport.last_params is not None
        assert transport.last_params["symboltoken"] == FRONT_TOKEN
        assert transport.last_params["symboltoken"] != BACK_TOKEN

    def test_it_names_the_contract_it_read(
        self, config: Any, master: InstrumentMaster, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The operator has to be able to see which month the drill used, or the
        proof proves nothing about the contract they care about."""
        _bars_from_candles(FakeDataTransport(), master, config, BacktestClock(NOW))

        assert "GOLDM25SEPFUT" in capsys.readouterr().out

    def test_a_master_with_no_futures_is_reported_not_crashed(
        self,
        config: Any,
        empty_master: InstrumentMaster,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A drill that cannot run says so. Raising here would abort the whole
        `live` command over a diagnostic that is not the point of the run."""
        transport = FakeDataTransport()

        _bars_from_candles(transport, empty_master, config, BacktestClock(NOW))

        assert "no futures contract in the master snapshot" in capsys.readouterr().out
        assert transport.calls == 0, "nothing should be requested with no contract"

    def test_a_feed_failure_degrades_rather_than_crashing(
        self, config: Any, master: InstrumentMaster, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The broad `except` here is deliberate and commented as such: a drill
        must degrade, not crash. A transport failure during a diagnostic must not
        take down a command whose real job is starting a session."""
        _bars_from_candles(
            FakeDataTransport(boom=True), master, config, BacktestClock(NOW)
        )

        out = capsys.readouterr().out
        assert "no bars yet" in out
        assert "smartapi is down" in out

    def test_it_reports_how_many_closed_bars_it_saw(
        self, config: Any, master: InstrumentMaster, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Zero is a real answer outside session hours, and has to read as zero
        rather than as a failure."""
        _bars_from_candles(FakeDataTransport(), master, config, BacktestClock(NOW))

        assert "closed bar(s) today" in capsys.readouterr().out


class TestThePaperLoopGuards:
    """`_run_paper_loop` returns before touching its transport in both cases
    below, so they need no broker at all."""

    def _run(self, config: Any, master: InstrumentMaster) -> None:
        _run_paper_loop(
            config=config,
            master=master,
            live_master=master,
            market_data_key="",
            transport=FakeDataTransport(),
            clock=BacktestClock(NOW),
            passes=1,
            poll_interval_s=0.0,
            wait_for_bar_min=0.0,
            state=None,
        )

    def test_it_refuses_to_run_in_live_mode(
        self, config: Any, master: InstrumentMaster
    ) -> None:
        """Belt to the caller's braces. The paper loop simulates fills, so
        reaching it with a live config would mean a run that believes it is
        trading and is not - and the check is inside the function so it cannot be
        bypassed by a future caller that forgets."""
        live = config.model_copy(update={"mode": Mode.LIVE})

        with pytest.raises(DomainError, match="must never be reached in live mode"):
            self._run(live, master)

    def test_a_master_with_no_futures_is_reported_not_crashed(
        self,
        config: Any,
        empty_master: InstrumentMaster,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._run(config, empty_master)

        assert "no futures contract in the master snapshot" in capsys.readouterr().out
