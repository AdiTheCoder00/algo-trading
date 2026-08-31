"""``algo config`` — resolve and display run configuration."""

from __future__ import annotations

from pathlib import Path

import typer

from algo.config.loader import config_hash, load_config
from algo.config.modes import resolve_mode

app = typer.Typer()


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
    basis = config.strategy.exit.take_profit_kind.removeprefix("PCT_OF_").lower()
    stop = (
        "NO STOP LOSS"
        if config.strategy.exit.no_stop_loss
        else f"SL {config.strategy.exit.stop_loss_value}%"
    )
    typer.echo(
        f"exits         TP {config.strategy.exit.take_profit_value}% / {stop} of {basis}"
    )
    entry = f"{config.strategy.entry_bars_ist[0]} IST, {config.strategy.cadence}"
    if config.strategy.roll_at_front_dte is not None:
        entry += f", at front DTE <= {config.strategy.roll_at_front_dte}"
    if config.strategy.cycle_offset:
        entry += f", selling cycle +{config.strategy.cycle_offset}"
    typer.echo(f"entry         {entry}")
    if config.strategy.strike_multiple is not None:
        typer.echo(f"strikes       multiples of {config.strategy.strike_multiple} only")
    typer.echo(f"equity        {config.risk.starting_equity}")
