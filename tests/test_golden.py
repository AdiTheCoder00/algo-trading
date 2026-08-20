"""The golden-file integration test. Brief §11:

    "Integration test: full backtest on a fixed dataset with a golden-file trade
     log."

A full strangle backtest over a fixed synthetic dataset, with its trade log
committed to the repository. Any change in engine behaviour — a fill priced
differently, an exit firing a bar earlier, a charge computed on the wrong base —
shows up as a diff in a file a human can read, rather than as a number that moved
somewhere in a summary.

The digest check fails fast; the file comparison shows *where*. Both, because
"the hash changed" is not a useful bug report.

**Regenerating the golden file is a deliberate act.** Run:

    ALGO_UPDATE_GOLDEN=1 pytest tests/test_golden.py

and then read the diff before committing it. A test that silently rewrites its own
expectation whenever the code changes is not a test.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from algo.backtest.engine import BacktestEngine, BacktestResult
from algo.backtest.prices import ChainFeedProvider, ChainPriceSource
from algo.core.bar import M1, M30, Bar
from algo.core.enums import Exchange
from algo.core.instrument import FutureId, InstrumentSpec
from algo.costs.charges import FlatChargeModel
from algo.costs.margin import FixedMarginPerLot
from algo.costs.slippage import NoSlippage
from algo.costs.spread import FixedTickSpread
from algo.data.resample import resample
from algo.data.synthetic_chain import build_chain
from algo.exchange.calendar import MarketCalendar, synthetic_calendar
from algo.exchange.expiries import (
    ExpiryCalendar,
    ExpirySet,
    InstrumentMasterExpiries,
    LastFridayRule,
)
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.portfolio.book import Portfolio
from algo.reporting import metrics as metrics_mod
from algo.reporting.export import trade_log_digest, write_trade_log
from algo.reporting.tearsheet import render
from algo.risk.devolvement import DevolvementGuard
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.strategy.delta_strangle import DeltaStrangle

GOLDEN = Path(__file__).parent / "golden" / "strangle_trade_log.csv"
GOLDEN_DIGEST = Path(__file__).parent / "golden" / "strangle_trade_log.digest"

START_PRICE = Decimal("156640")
EXPIRY = date(2026, 8, 28)
GOLDM_FUT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))

CYCLE = ExpirySet(
    option_expiry=EXPIRY,
    futures_expiry=date(2026, 9, 4),
    tender_period_start=date(2026, 9, 1),
)

SPEC = InstrumentSpec(
    underlying="GOLDM",
    exchange=Exchange.MCX,
    lot_size=Decimal("100"),
    multiplier=Decimal("10"),
    tick_size=Decimal("0.50"),
    strike_interval=Decimal("500"),
    min_lots=1,
    effective_from=date(2026, 1, 1),
    source="golden fixture",
)

#: Fixed, deliberately. A golden file over a random walk would change whenever the
#: generator did, which is exactly the coupling this test exists to avoid.
SESSION_DAYS = [
    date(2026, 8, 19),
    date(2026, 8, 20),
    date(2026, 8, 21),
    date(2026, 8, 24),
    date(2026, 8, 25),
    date(2026, 8, 26),
    date(2026, 8, 27),
]
DRIFT = Decimal("1.5")


def _run_fixed_backtest(calendar: MarketCalendar) -> BacktestResult:
    minute_bars: list[Bar] = []
    level = START_PRICE
    for day in SESSION_DAYS:
        opened = calendar.session_open(day)
        for minute in range(1, calendar.session_minutes(day) + 1):
            nxt = level + DRIFT
            minute_bars.append(
                Bar(
                    ts=opened + timedelta(minutes=minute),
                    timeframe=M1,
                    open=level,
                    high=nxt,
                    low=level,
                    close=nxt,
                    volume=1,
                )
            )
            level = nxt
    bars = resample(minute_bars, calendar=calendar, timeframe=M30)

    expires_at = calendar.session_close(EXPIRY)
    chains = [
        build_chain(
            ts=bar.ts,
            underlying_future=GOLDM_FUT,
            option_expiry=EXPIRY,
            futures_price=bar.close,
            expires_at=expires_at,
            vol=0.2175,
            strikes_each_side=14,
            strike_centre=START_PRICE,
            populate_greeks=True,
        )
        for bar in bars
    ]

    master = InstrumentMasterExpiries({("GOLDM", 2026, 8): CYCLE})
    engine = BacktestEngine(
        bars=bars,
        calendar=calendar,
        specs=ContractSpecStore([SPEC]),
        strategy=DeltaStrangle(underlying="GOLDM", min_dte=3, max_dte=45),
        risk=RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=2,
            max_lots_per_underlying=10,
        ),
        simulator=FillSimulator(
            spread=FixedTickSpread(2),
            slippage=NoSlippage(),
            charges=FlatChargeModel(Decimal("20")),
        ),
        portfolio=Portfolio(Decimal("1000000.00")),
        instrument=GOLDM_FUT,
        timeframe=M30,
        is_option=True,
        config_hash="golden",
        price_source=ChainPriceSource(chains),
        chain_provider=ChainFeedProvider(chains),
        expiries=ExpiryCalendar(authority=master, rule=LastFridayRule(calendar)),
        devolvement=DevolvementGuard(calendar=calendar),
        margin=FixedMarginPerLot(Decimal("100000")),
    )
    return engine.run()


@pytest.fixture(scope="module")
def result() -> BacktestResult:
    return _run_fixed_backtest(synthetic_calendar())


class TestGoldenTradeLog:
    def test_the_run_produced_trades_at_all(self, result: BacktestResult) -> None:
        """Guards the guard: a golden file of zero trades would pass forever."""
        assert result.trades, "the fixture must actually trade or the golden file is vacuous"
        assert result.round_trips == len(result.trades)

    def test_the_trade_log_matches_the_golden_file(
        self, result: BacktestResult, tmp_path: Path
    ) -> None:
        produced = write_trade_log(result.trades, tmp_path / "trade_log.csv")
        text = produced.read_text(encoding="utf-8")

        if os.environ.get("ALGO_UPDATE_GOLDEN"):
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(text, encoding="utf-8")
            GOLDEN_DIGEST.write_text(trade_log_digest(result.trades), encoding="utf-8")
            pytest.skip("golden file regenerated — read the diff before committing it")

        assert GOLDEN.exists(), (
            "no golden file. Generate one with ALGO_UPDATE_GOLDEN=1 and commit it."
        )
        assert text == GOLDEN.read_text(encoding="utf-8")

    def test_the_digest_matches(self, result: BacktestResult) -> None:
        if os.environ.get("ALGO_UPDATE_GOLDEN"):
            pytest.skip("regenerating")
        assert GOLDEN_DIGEST.exists()
        assert trade_log_digest(result.trades) == GOLDEN_DIGEST.read_text(encoding="utf-8").strip()

    def test_two_runs_of_the_same_fixture_agree(self) -> None:
        """Brief §7.4 — byte-identical across runs, not merely across a rerun of
        the same object."""
        first = _run_fixed_backtest(synthetic_calendar())
        second = _run_fixed_backtest(synthetic_calendar())
        assert trade_log_digest(first.trades) == trade_log_digest(second.trades)
        assert first.dataset_hash == second.dataset_hash
        assert [f.model_dump_json() for f in first.fills] == [
            f.model_dump_json() for f in second.fills
        ]


class TestTearsheetRenders:
    def test_it_produces_a_self_contained_document(self, result: BacktestResult) -> None:
        summary = metrics_mod.compute(
            result.equity_curve,
            trade_count=result.round_trips,
            total_cost=result.realised_cost,
            trades=result.trades,
        )
        markup = render(
            title="GOLDM strangle — golden fixture",
            metrics=summary,
            curve=result.equity_curve,
            trades=result.trades,
            warnings=result.warnings,
            dataset_hash=result.dataset_hash,
            config_hash=result.config_hash,
        )
        assert markup.startswith("<!doctype html>")
        assert "<svg" in markup
        # Self-contained: nothing fetched when it opens.
        assert "http://" not in markup
        assert "https://" not in markup
        assert "<script" not in markup

    def test_the_caveats_appear_above_the_numbers(self, result: BacktestResult) -> None:
        """Placeholder rates change what every figure below them means, and a
        footnote is where that goes to be ignored."""
        summary = metrics_mod.compute(
            result.equity_curve,
            trade_count=result.round_trips,
            total_cost=result.realised_cost,
            trades=result.trades,
        )
        markup = render(
            title="t",
            metrics=summary,
            curve=result.equity_curve,
            trades=result.trades,
            warnings=("spread is modelled, not measured",),
        )
        assert markup.index("Read these before reading the numbers") < markup.index("net P&amp;L")

    def test_it_never_claims_the_strategy_works(self, result: BacktestResult) -> None:
        """Brief §12."""
        summary = metrics_mod.compute(
            result.equity_curve,
            trade_count=result.round_trips,
            total_cost=result.realised_cost,
            trades=result.trades,
        )
        markup = render(
            title="t", metrics=summary, curve=result.equity_curve, trades=result.trades
        )
        assert "makes no claim that the strategy is profitable" in markup
