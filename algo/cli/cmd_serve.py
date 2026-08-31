"""``algo serve`` — run the read-only monitoring API."""

from __future__ import annotations

import os
from pathlib import Path

import typer

app = typer.Typer()


@app.command()
def serve(
    state: Path = typer.Option(Path("state/dashboard.db"), help="State file the engine writes"),
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000),
    mode: str = typer.Option("paper"),
) -> None:
    """Serve the read-only monitoring API.

    Binds to localhost by default. The kill-switch endpoint changes trading
    behaviour, so the default is a socket nothing outside this machine can reach;
    exposing it is a decision, not an accident.
    """
    import uvicorn

    from algo.api.app import TOKEN_ENV, create_app

    if not os.environ.get(TOKEN_ENV):
        raise typer.BadParameter(
            f"{TOKEN_ENV} is not set. The API will not start without it - the "
            "kill-switch endpoint is not something to serve unauthenticated."
        )
    if host not in ("127.0.0.1", "localhost") :
        typer.echo(
            f"  ! binding to {host}, not localhost. Anything that can reach this "
            "port can request a trading halt."
        )

    typer.echo(f"monitoring API on http://{host}:{port}  (state: {state})")
    uvicorn.run(create_app(state_path=state, mode=mode), host=host, port=port, log_level="warning")
