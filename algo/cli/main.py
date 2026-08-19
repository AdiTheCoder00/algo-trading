"""Command line entry points.

Milestone 1 ships two commands: one that proves the data pipeline end to end on
synthetic bars, and one that shows exactly what configuration a run would use.

`backtest` deliberately does not exist yet. It arrives with the engine at
Milestone 3, and a stub that silently produced no trades would be worse than an
honest absence.
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
def backtest() -> None:
    """Not implemented until Milestone 3."""
    raise typer.BadParameter(
        "the backtest engine arrives at Milestone 3. Milestone 1 covers the domain "
        "models, the MCX calendar, the feeds and the look-ahead guarantees — run "
        "`algo verify` to exercise those."
    )


if __name__ == "__main__":
    app()
