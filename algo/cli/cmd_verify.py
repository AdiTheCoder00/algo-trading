"""``algo verify`` — the Milestone 1 data-pipeline proof."""

from __future__ import annotations

import typer

from algo.cli._helpers import STANDARD_DAY, US_DST_DAY
from algo.core.bar import Timeframe
from algo.data.resample import expected_bar_count, resample
from algo.data.synthetic import one_minute_session
from algo.data.validate import validate_bars
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.specs import ContractSpecStore

app = typer.Typer()


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
