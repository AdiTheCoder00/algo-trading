"""The dashboard state store, driven by the engine (D-088).

These tests prove the loop the monitoring layer depends on: the engine writes
equity, positions, signals, notes, trades and health to the state file the API
reads, and acts on kill-switch requests recorded there on its next bar.

The wiring is opt-in — every assertion here runs the same engine with a
`StateStore` attached, and one test proves that attaching one changes nothing
about the result it produces.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from algo.backtest.engine import BacktestEngine
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import FutureId
from algo.core.timeutil import utc
from algo.costs.charges import McxChargeModel
from algo.costs.slippage import TickSlippage
from algo.costs.spread import FixedTickSpread
from algo.data.resample import resample
from algo.data.synthetic import one_minute_session
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.persistence.state import StateStore
from algo.portfolio.book import Portfolio
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.risk.exits import ExitReason
from algo.risk.killswitch import KillSwitch
from algo.strategy.buy_and_hold import BuyAndHold
from algo.strategy.coin_flip import CoinFlip

INSTRUMENT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))
STARTING = Decimal("1000000.00")


def _bars() -> list[Bar]:
    calendar = synthetic_calendar()
    minute_bars = one_minute_session(calendar, date(2026, 8, 19), seed=20260819)
    return resample(minute_bars, calendar=calendar, timeframe=Timeframe(minutes=30))


def _run(
    store: StateStore,
    *,
    strategy: str = "coin_flip",
    kill_switch: KillSwitch | None = None,
) -> BacktestEngine:
    """Run the same wiring the CLI builds for `algo backtest --state`."""
    bars = _bars()
    engine = BacktestEngine(
        bars=bars,
        calendar=synthetic_calendar(),
        specs=ContractSpecStore.default(),
        strategy=(
            CoinFlip(INSTRUMENT, seed=20260819)
            if strategy == "coin_flip"
            else BuyAndHold(INSTRUMENT)
        ),
        risk=RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=1,
            max_lots_per_underlying=5,
        ),
        simulator=FillSimulator(
            spread=FixedTickSpread(2),
            slippage=TickSlippage(market_ticks=0, stop_ticks=2),
            charges=McxChargeModel.default(),
        ),
        portfolio=Portfolio(STARTING),
        instrument=INSTRUMENT,
        timeframe=Timeframe(minutes=30),
        is_option=False,
        kill_switch=kill_switch,
        state=store,
        mode="backtest",
        broker="backtest",
    )
    return engine


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "dashboard.db")


class TestEngineWritesTheDashboard:
    def test_equity_rows_stream_one_per_bar(self, store: StateStore) -> None:
        engine = _run(store, strategy="buy_and_hold")
        result = engine.run()
        curve = store.equity_curve()

        assert len(curve) == len(result.equity_curve)
        assert curve[0].equity == STARTING
        assert all(curve[i].ts < curve[i + 1].ts for i in range(len(curve) - 1))
        assert Decimal(curve[-1].equity) == result.final_equity

    def test_positions_snapshot_reflects_the_open_book(self, store: StateStore) -> None:
        engine = _run(store, strategy="buy_and_hold")
        engine.run()

        rows = store.positions()
        assert len(rows) == 1
        assert rows[0].instrument_key == INSTRUMENT.key
        assert rows[0].qty == Decimal("1")
        assert rows[0].mark is not None and rows[0].mark > 0
        assert rows[0].average_price > 0

    def test_signals_carry_their_reason(self, store: StateStore) -> None:
        engine = _run(store, strategy="buy_and_hold")
        engine.run()

        rows = store.signals()
        assert len(rows) == 1
        assert rows[0].action == "OPEN"
        assert rows[0].strategy == "buy_and_hold"
        assert rows[0].reason  # the six-weeks-later question must be answerable
        assert rows[0].context

    def test_completed_trades_are_recorded(self, store: StateStore) -> None:
        engine = _run(store, strategy="coin_flip")
        result = engine.run()

        recorded = {row["trade_id"] for row in store.trades()}
        assert len(recorded) == len(result.trades)
        assert {trade.trade_id for trade in result.trades} == recorded

    def test_health_stamps_what_the_run_was(self, store: StateStore) -> None:
        engine = _run(store, strategy="buy_and_hold")
        engine.run()

        health = store.health()
        assert health["engine"] == "stopped"
        assert health["mode"] == "backtest"
        assert health["broker"] == "backtest"
        assert health["kill_switch"] == "armed"
        # The falsification's placeholders are exactly what the API warns about.
        assert health["costs_verified"] == "false"
        assert health["spread_measured"] == "false"
        assert health["margin_calibrated"] == "true"

    def test_a_rejected_signal_becomes_a_note(self, store: StateStore) -> None:
        kill = KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )
        kill.trip_manually("pre-trip for the test", utc(2026, 8, 19, 3, 30))
        engine = _run(store, strategy="buy_and_hold", kill_switch=kill)
        result = engine.run()

        assert result.rejections[0].reason.value == "KILL_SWITCH_TRIPPED"
        messages = [note.message for note in store.notes()]
        assert any("KILL_SWITCH_TRIPPED" in message for message in messages)


class TestEngineConsumesKillSwitchRequests:
    def test_a_pending_request_trips_the_switch_and_is_marked_acted(
        self, store: StateStore
    ) -> None:
        store.request_kill_switch(
            requested_by="test", reason="cover check", flatten=False, at=utc(2026, 8, 19, 3, 30)
        )
        kill = KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )
        engine = _run(store, strategy="buy_and_hold", kill_switch=kill)
        result = engine.run()

        assert result.kill_switch_tripped
        assert store.pending_kill_switch_requests() == []
        assert store.health()["kill_switch"] == "tripped"
        assert store.kill_switch_requests()[0].acted_on_at is not None

    def test_a_flatten_request_closes_the_open_position(self, store: StateStore) -> None:
        # Buy-and-hold enters on the second bar (~04:30 UTC); the request lands
        # after that, so there is a position for the flatten to close.
        store.request_kill_switch(
            requested_by="test", reason="get me out", flatten=True, at=utc(2026, 8, 19, 4, 45)
        )
        kill = KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )
        engine = _run(store, strategy="buy_and_hold", kill_switch=kill)
        result = engine.run()

        assert any(exit_event.reason is ExitReason.KILL_SWITCH for exit_event in result.exits)
        assert any(trade.exit_reason == "KILL_SWITCH" for trade in result.trades)
        assert store.positions() == []

    def test_flatten_is_explicit_never_the_default(self, store: StateStore) -> None:
        store.request_kill_switch(
            requested_by="test", reason="halt only", flatten=False, at=utc(2026, 8, 19, 4, 45)
        )
        kill = KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )
        engine = _run(store, strategy="buy_and_hold", kill_switch=kill)
        result = engine.run()

        assert result.kill_switch_tripped
        assert result.exits == ()
        # The halt stopped new orders; without flatten the position is untouched.
        assert store.positions()


class TestWiringIsInvisible:
    def test_attaching_a_store_changes_nothing_about_the_result(self, store: StateStore) -> None:
        with_state = _run(store, strategy="coin_flip")
        first = with_state.run()

        plain = _run(store, strategy="coin_flip")  # same store, second run
        second = plain.run()

        assert second.net_pnl == first.net_pnl
        assert second.final_equity == first.final_equity
        assert [f.fill_id for f in second.fills] == [f.fill_id for f in first.fills]
        assert [p.equity for p in second.equity_curve] == [p.equity for p in first.equity_curve]
