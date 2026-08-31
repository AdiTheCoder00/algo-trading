"""The MT5 paper loop, driven end to end against a scripted terminal.

`test_mt5_feed.py` proves the feed reads bars correctly and `test_execution.py`
proves the router places at most once. What neither covers is the thing this
module exists for: that a breakout on real-shaped MT5 bars actually reaches the
broker as an order, and that a second poll of the same bar does not place a
second one.

Nothing here touches a terminal, a socket, or the wall clock. The "terminal" is
a list of rows chosen to break a Donchian channel on a known bar, so the
assertion is about the wiring, not about whether the strategy is any good.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from algo.core.bar import Timeframe
from algo.core.clock import BacktestClock
from algo.core.enums import Exchange
from algo.core.errors import DataError
from algo.core.instrument import CfdId
from algo.costs.cfd import CfdChargeModel
from algo.costs.slippage import NoSlippage
from algo.costs.spread import FixedTickSpread
from algo.data.mt5_feed import Mt5BarFeed
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.execution.paper import PaperBroker
from algo.live.mt5_runner import build_mt5_paper_loop, strategy_for
from algo.persistence.journal import OrderJournal

TF = Timeframe(minutes=30)
NOW = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
XAUUSD = CfdId(symbol="XAUUSD")
LOOKBACK = 5

_DTYPE = np.dtype(
    [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "i8"),
    ]
)


class ScriptedTerminal:
    """A terminal whose bars are given, not generated. Server clock = UTC."""

    TIMEFRAME_M30 = 30

    def __init__(self, closes: list[float]) -> None:
        self.closes = closes

    def last_error(self) -> tuple[int, str]:
        return (-1, "scripted")

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> NDArray[np.void]:
        # Newest last; the feed drops the final row as the forming bar, so one
        # extra is appended here exactly as the real API would return it.
        series = [*self.closes, self.closes[-1]]
        n = min(count, len(series))
        window = series[-n:]
        rows = [
            (
                int((NOW - timedelta(minutes=30 * (n - 1 - i))).timestamp()),
                close,
                close + 0.5,
                close - 0.5,
                close,
                1000,
            )
            for i, close in enumerate(window)
        ]
        return np.array(rows, dtype=_DTYPE)


def _breakout_series() -> list[float]:
    """Flat, then a decisive break above the channel high."""
    return [*([4400.0] * (LOOKBACK + 2)), 4460.0]


def _run(tmp_path: Path, closes: list[float]) -> tuple[object, PaperBroker]:
    terminal = ScriptedTerminal(closes)
    feed = Mt5BarFeed(
        terminal=terminal,  # type: ignore[arg-type]
        symbol="XAUUSD",
        timeframe=TF,
        server_offset=timedelta(0),
    )
    clock = BacktestClock(NOW)
    seed = list(feed.closed_bars(count=len(closes)))
    broker = PaperBroker(
        simulator=FillSimulator(
            spread=FixedTickSpread(29),
            slippage=NoSlippage(),
            charges=CfdChargeModel.vantage_standard(),
        ),
        specs=ContractSpecStore.default(),
        quote=lambda key: seed[-1].close if key == XAUUSD.key else None,
        clock=clock,
        exchange=Exchange.OTC,
    )
    broker.connect()
    journal = OrderJournal(tmp_path / "journal.db")
    journal.__enter__()
    run = build_mt5_paper_loop(
        bars=feed,
        broker=broker,
        clock=clock,
        strategy=strategy_for(
            "breakout",
            instrument=XAUUSD,
            stop_loss_pct=Decimal("0"),
            trail_activation_pct=Decimal("2"),
            trail_pct=Decimal("0"),
            lookback=LOOKBACK,
        ),
        instrument=XAUUSD,
        timeframe=TF,
        journal=journal,
        seed_bars=seed,
        lots=10,
        max_lots=10,
    )
    return run, broker


class TestTheLoopActuallyTrades:
    def test_a_breakout_reaches_the_broker_as_a_fill(self, tmp_path: Path) -> None:
        run, broker = _run(tmp_path, _breakout_series())

        result = run.loop.pass_once()  # type: ignore[attr-defined]

        assert result.routed, f"no order routed: {result.summary()}"
        positions = broker.positions()
        assert len(positions) == 1
        assert positions[0].instrument_key == XAUUSD.key
        assert positions[0].lots > 0  # long, on an upside break
        run.journal.close()  # type: ignore[attr-defined]

    def test_the_same_bar_twice_does_not_double_the_position(
        self, tmp_path: Path
    ) -> None:
        """The property `LiveLoop` exists to guarantee, asserted through this
        wiring rather than assumed from its own tests: a duplicated poll is a
        doubled position if the bar watermark is not honoured."""
        run, broker = _run(tmp_path, _breakout_series())

        first = run.loop.pass_once()  # type: ignore[attr-defined]
        second = run.loop.pass_once()  # type: ignore[attr-defined]

        assert first.routed
        assert not second.routed
        assert "already acted on" in second.summary()
        assert len(broker.positions()) == 1
        run.journal.close()  # type: ignore[attr-defined]

    def test_a_flat_market_places_nothing(self, tmp_path: Path) -> None:
        run, broker = _run(tmp_path, [4400.0] * (LOOKBACK + 3))

        result = run.loop.pass_once()  # type: ignore[attr-defined]

        assert not result.routed
        assert broker.positions() == []
        run.journal.close()  # type: ignore[attr-defined]


class TestNoOrderCanReachTheRealBroker:
    def test_the_broker_is_the_paper_one(self, tmp_path: Path) -> None:
        """The safety property of this whole path: fills are simulated, and
        `Mt5Broker.place` - never exercised against a live endpoint - is not in
        the loop at all."""
        run, _ = _run(tmp_path, _breakout_series())

        assert isinstance(run.broker, PaperBroker)  # type: ignore[attr-defined]
        run.journal.close()  # type: ignore[attr-defined]


class TestStrategyLookup:
    def test_it_resolves_both_cfd_strategies(self) -> None:
        for name in ("breakout", "macd"):
            built = strategy_for(
                name,
                instrument=XAUUSD,
                stop_loss_pct=Decimal("0.5"),
                trail_activation_pct=Decimal("2"),
                trail_pct=Decimal("1"),
            )
            assert built.params()["stop_loss_pct"] == "0.5"
            assert built.params()["trail_pct"] == "1"

    def test_an_unknown_name_is_refused(self) -> None:
        with pytest.raises(DataError, match="unknown strategy"):
            strategy_for(
                "strangle",
                instrument=XAUUSD,
                stop_loss_pct=Decimal("0"),
                trail_activation_pct=Decimal("2"),
                trail_pct=Decimal("0"),
            )


class TestItRefusesToStartBlind:
    def test_no_seed_bars_is_refused(self, tmp_path: Path) -> None:
        """A loop started with no history would treat the first bar it ever saw
        as the whole channel - the market being shut is a reason to wait, not to
        trade off one bar."""
        clock = BacktestClock(NOW)
        broker = PaperBroker(
            simulator=FillSimulator(
                spread=FixedTickSpread(29),
                slippage=NoSlippage(),
                charges=CfdChargeModel.vantage_standard(),
            ),
            specs=ContractSpecStore.default(),
            quote=lambda key: Decimal("4400"),
            clock=clock,
            exchange=Exchange.OTC,
        )
        broker.connect()
        with OrderJournal(tmp_path / "j.db") as journal, pytest.raises(
            DataError, match="at least one closed bar"
        ):
            build_mt5_paper_loop(
                bars=object(),
                broker=broker,
                clock=clock,
                strategy=strategy_for(
                    "breakout",
                    instrument=XAUUSD,
                    stop_loss_pct=Decimal("0"),
                    trail_activation_pct=Decimal("2"),
                    trail_pct=Decimal("0"),
                ),
                instrument=XAUUSD,
                timeframe=TF,
                journal=journal,
                seed_bars=[],
            )
