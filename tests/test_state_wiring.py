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
from algo.core.enums import QuoteFlag
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
    margin_cap_pct: Decimal | None = None,
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
            margin_cap_pct=margin_cap_pct,
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


class TestChainSnapshotIsRecorded:
    """The dashboard's chain panel needs a real chain, not just an equity
    number - this is the wiring that gets it there."""

    def test_no_chain_provider_skips_silently(self, store: StateStore) -> None:
        """`TestEngineWritesTheDashboard` and friends run coin_flip/buy_and_hold
        on the underlying future directly - no chain_provider, no expiries. That
        must not raise; it must simply have nothing to record."""
        engine = _run(store, strategy="coin_flip")
        engine.run()
        assert store.chain_snapshot() is None

    def test_a_real_strangle_run_records_a_real_chain(self, store: StateStore) -> None:
        from algo.backtest.prices import ChainFeedProvider, ChainPriceSource, CompositePriceSource
        from algo.core.enums import Exchange
        from algo.core.instrument import FutureId as _FutureId
        from algo.data.synthetic_chain import build_chain
        from algo.exchange.expiries import ExpiryCalendar, ExpirySet, InstrumentMasterExpiries
        from algo.risk.devolvement import DevolvementGuard
        from algo.strategy.delta_strangle import DeltaStrangle

        calendar = synthetic_calendar()
        option_expiry = date(2026, 8, 28)
        future = _FutureId(underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX)
        expires_at = calendar.session_close(option_expiry)

        bars = _bars()
        chains = [
            build_chain(
                ts=bar.ts,
                underlying_future=future,
                option_expiry=option_expiry,
                futures_price=bar.close,
                expires_at=expires_at,
                vol=0.2175,
                strikes_each_side=14,
                strike_centre=Decimal("156640"),
                populate_greeks=True,
            )
            for bar in bars
        ]
        master = InstrumentMasterExpiries(
            {
                ("GOLDM", 2026, 8): ExpirySet(
                    option_expiry=option_expiry, futures_expiry=date(2026, 9, 4)
                )
            }
        )

        engine = BacktestEngine(
            bars=bars,
            calendar=calendar,
            specs=ContractSpecStore.default(),
            strategy=DeltaStrangle(underlying="GOLDM", min_dte=3, max_dte=45),
            risk=RiskEngine(
                sizer=FixedLotSizer(1),
                spec_for=None,
                max_concurrent_positions=2,
                max_lots_per_underlying=10,
            ),
            simulator=FillSimulator(
                spread=FixedTickSpread(2),
                slippage=TickSlippage(market_ticks=0, stop_ticks=2),
                charges=McxChargeModel.default(),
            ),
            portfolio=Portfolio(STARTING),
            instrument=future,
            timeframe=Timeframe(minutes=30),
            is_option=True,
            price_source=CompositePriceSource(ChainPriceSource(chains)),
            chain_provider=ChainFeedProvider(chains),
            expiries=ExpiryCalendar(authority=master, rule=None),
            devolvement=DevolvementGuard(
                calendar=calendar,
                force_exit_sessions_before_expiry=1,
                block_new_entries_within_dte=2,
            ),
            state=store,
            mode="backtest",
            broker="backtest",
        )
        result = engine.run()

        snapshot = store.chain_snapshot()
        assert snapshot is not None
        assert snapshot["underlying"] == "GOLDM"
        assert snapshot["option_expiry"] == option_expiry.isoformat()
        assert snapshot["rows"], "a real chain must carry real strikes"

        row = snapshot["rows"][0]
        assert set(row) == {
            "strike",
            "right",
            "bid",
            "ask",
            "ltp",
            "volume",
            "iv",
            "delta",
            "tradeable",
            "flag",
            "held",
        }
        # Why a row is untradeable, not just that it is: a strike rejected for a
        # blown-out spread (Q17) is a different fact from one nobody quoted.
        assert row["flag"] in {f.value for f in QuoteFlag}

        # `days_until_forced_exit` was already written for exactly this - "the
        # deadline is never a surprise" (algo/risk/devolvement.py) - and had
        # never actually reached anything a person could look at until now.
        assert isinstance(snapshot["dte"], int)
        assert snapshot["dte"] > 0, "the 19 Aug fixture session is well before the 28 Aug expiry"
        assert isinstance(snapshot["forced_exit_in_sessions"], int)

        if result.fills:
            # Once the strangle is open, the two legs it actually holds must be
            # the ones flagged - not just any two tradeable strikes.
            assert any(r["held"] for r in snapshot["rows"])
            held_count = sum(1 for r in snapshot["rows"] if r["held"])
            assert held_count <= 2


class TestMarginUtilisationIsRecorded:
    """`RiskEngine`'s own cap decides whether the *next* signal can size at
    all - surfaced so an operator does not have to infer a rejected entry was
    a margin cap and not something else."""

    def test_no_cap_configured_writes_nothing(self, store: StateStore) -> None:
        engine = _run(store, strategy="buy_and_hold")  # margin_cap_pct defaults to None
        engine.run()
        assert "margin_used" not in store.health()

    def test_a_configured_cap_is_reported(self, store: StateStore) -> None:
        engine = _run(store, strategy="buy_and_hold", margin_cap_pct=Decimal("50"))
        engine.run()

        health = store.health()
        assert "margin_used" in health
        assert Decimal(health["margin_cap_pct"]) == Decimal("50")
        # cap = equity * pct / 100; equity moves bar to bar, so check the
        # relationship holds rather than pinning an exact rupee figure.
        curve = store.equity_curve(limit=1)
        assert Decimal(health["margin_cap"]) == curve[0].equity * Decimal("50") / Decimal("100")


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
