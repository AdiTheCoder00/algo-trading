"""Command line entry points.

Three commands: `verify` proves the data pipeline end to end on synthetic bars,
`config` shows exactly what settings a run would use, and `backtest` runs the
Milestone 3 falsification.

`backtest` deliberately does not accept a real dataset yet. There is no recorded
data to point it at, and a command that quietly ran on generated bars while
looking like a strategy result would be worse than one that says what it is.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from algo.config.loader import config_hash, load_config
from algo.config.modes import resolve_mode
from algo.core.bar import Timeframe
from algo.data.resample import expected_bar_count, resample
from algo.data.synthetic import one_minute_session
from algo.data.validate import validate_bars
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.specs import ContractSpecStore

app = typer.Typer(add_completion=False, help="MCX GOLDM short-strangle engine")

#: One day inside US daylight saving and one outside, so both session-length
#: regimes are exercised every time `verify` runs.
US_DST_DAY = date(2026, 8, 19)
STANDARD_DAY = date(2026, 11, 10)


@app.command()
def verify(timeframe_minutes: int = 30) -> None:
    """Run the data pipeline on synthetic bars and report what came out.

    This is the Milestone 1 proof: session boundaries, resampling, the partial-bar
    stub and the quality gates, exercised in both DST regimes.
    """
    calendar = synthetic_calendar()
    tf = Timeframe(minutes=timeframe_minutes)

    typer.echo(f"Synthetic pipeline check, {tf.label} bars\n")
    for label, day in (("US DST     ", US_DST_DAY), ("standard   ", STANDARD_DAY)):
        minute_bars = one_minute_session(calendar, day, seed=20260819)
        bars = resample(minute_bars, calendar=calendar, timeframe=tf)
        report = validate_bars(bars, calendar=calendar, timeframe=tf)
        partial = sum(1 for b in bars if b.is_partial)
        typer.echo(
            f"  {label} {day}  session {calendar.session_minutes(day)} min"
            f"  ->  {len(bars)} bars"
            f" (expected {expected_bar_count(calendar, day, tf)}, {partial} partial)"
        )
        typer.echo(f"               {report.summary()}")

    typer.echo("\nContract specifications in force:")
    store = ContractSpecStore.default()
    for underlying, exchange in store.underlyings():
        spec = store.spec_for(underlying, exchange, US_DST_DAY)
        typer.echo(
            f"  {underlying} on {exchange}: lot {spec.lot_size}, tick {spec.tick_size}, "
            f"multiplier {spec.multiplier}, strikes every {spec.strike_interval}"
        )
        typer.echo(f"    source: {spec.source.strip()}")


@app.command("config")
def show_config(
    path: Path = typer.Argument(Path("config/goldm.yaml"), help="Config file to resolve"),
) -> None:
    """Resolve a configuration and show what a run would actually use."""
    config = load_config(path)
    mode = resolve_mode(config.mode)

    typer.echo(f"mode          {mode}")
    typer.echo(f"config hash   {config_hash(config)}")
    typer.echo(f"instruments   {', '.join(i.underlying for i in config.instruments)}")
    typer.echo(f"bar           {config.market.bar.timeframe_minutes}m")
    typer.echo(f"sizing        {config.risk.sizing.mode} = {config.risk.sizing.fixed_lots} lot(s)")
    typer.echo(
        f"exits         TP {config.strategy.exit.take_profit_value}% / "
        f"SL {config.strategy.exit.stop_loss_value}% of "
        f"{config.strategy.exit.stop_loss_kind.removeprefix('PCT_OF_').lower()}"
    )
    typer.echo(f"entry         {config.strategy.entry_bars_ist[0]} IST, {config.strategy.cadence}")
    typer.echo(f"equity        {config.risk.starting_equity}")


@app.command()
def backtest(
    strategy: str = typer.Option("coin_flip", help="coin_flip | buy_and_hold"),
    spread_ticks: int = typer.Option(2, help="Modelled spread, in ticks"),
    seed: int = typer.Option(20260819),
) -> None:
    """Run the Milestone 3 falsification on synthetic bars.

    This is not a strategy result and does not pretend to be one. It runs the two
    reference strategies over generated data to show that the engine's cost
    arithmetic behaves — buy-and-hold tracking the instrument, and a coin flip on
    a flat market losing exactly its costs and nothing else.
    """
    from decimal import Decimal

    from algo.backtest.engine import BacktestEngine
    from algo.core.instrument import FutureId
    from algo.costs.charges import McxChargeModel
    from algo.costs.slippage import TickSlippage
    from algo.costs.spread import FixedTickSpread
    from algo.data.resample import resample
    from algo.data.synthetic import flat_session, one_minute_session
    from algo.execution.fills import FillSimulator
    from algo.portfolio.book import Portfolio
    from algo.reporting import metrics as metrics_mod
    from algo.risk.engine import FixedLotSizer, RiskEngine
    from algo.strategy.buy_and_hold import BuyAndHold
    from algo.strategy.coin_flip import CoinFlip

    calendar = synthetic_calendar()
    tf = Timeframe(minutes=30)
    instrument = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))

    flat = strategy == "coin_flip"
    source = (
        flat_session(calendar, US_DST_DAY, price=Decimal("156640.00"))
        if flat
        else one_minute_session(calendar, US_DST_DAY, seed=seed)
    )
    bars = resample(source, calendar=calendar, timeframe=tf)

    engine = BacktestEngine(
        bars=bars,
        calendar=calendar,
        specs=ContractSpecStore.default(),
        strategy=(
            CoinFlip(instrument, seed=seed)
            if strategy == "coin_flip"
            else BuyAndHold(instrument)
        ),
        risk=RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=1,
            max_lots_per_underlying=5,
        ),
        simulator=FillSimulator(
            spread=FixedTickSpread(spread_ticks),
            slippage=TickSlippage(market_ticks=0, stop_ticks=2),
            charges=McxChargeModel.default(),
        ),
        portfolio=Portfolio(Decimal("1000000.00")),
        instrument=instrument,
        timeframe=tf,
        is_option=False,
    )
    result = engine.run()

    typer.echo(f"strategy      {strategy}")
    typer.echo(f"market        {'flat (cost isolation)' if flat else 'seeded random walk'}")
    typer.echo(f"bars          {len(bars)}   dataset {result.dataset_hash}")
    typer.echo("")
    summary = metrics_mod.compute(
        result.equity_curve,
        trade_count=result.round_trips,
        total_cost=result.realised_cost,
    )
    typer.echo(summary.summary())
    typer.echo("")
    typer.echo(f"  spread paid     {result.spread_cost:,}")
    typer.echo(f"  charges paid    {result.total_charges:,}")
    if flat:
        typer.echo("")
        typer.echo(
            f"  FALSIFICATION: gross P&L on a flat market = {result.gross_pnl} "
            f"(must be exactly 0)"
        )
    for warning in result.warnings:
        typer.echo(f"  ! {warning}")


if __name__ == "__main__":
    app()
