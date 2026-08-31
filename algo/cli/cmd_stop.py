"""``algo stop`` - ask a running loop to stop, from anywhere.

Ctrl-C works when you are at the loop's terminal. A loop started in the
background, by a service manager, or from another console has no terminal you
can reach - and on Windows a POSIX signal does not cross that boundary at all,
which turns "just Ctrl-C it" into "kill the process", the very thing the
graceful path exists to avoid.

This writes the sentinel `StopFile` the loop polls. Same shape as the kill
switch (D-012): it records a request, the loop reads it at its next bar
boundary, finishes the pass in flight, and exits through the identical path a
Ctrl-C would have taken.

It deliberately does not find, signal or kill anything. Nothing here can
interrupt a loop mid-order; the strongest thing it does is create a file.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer()


@app.command("stop")
def stop(
    stop_file: Path = typer.Option(
        Path("state/STOP"),
        "--stop-file",
        help="The sentinel the running loop polls",
    ),
    reason: str = typer.Option(
        "",
        "--reason",
        help="Recorded in the file and reported in the loop's exit note",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove a pending request instead of making one",
    ),
) -> None:
    """Ask a running loop to stop after its current bar.

    The loop clears the file on its way out, so this does not need cleaning up
    afterwards. `--clear` exists for the case where you change your mind before
    the loop has noticed.
    """
    from algo.live.shutdown import StopFile

    sentinel = StopFile(stop_file)

    if clear:
        if sentinel.clear():
            typer.echo(f"cleared {stop_file} - the loop will keep running")
        else:
            typer.echo(f"nothing to clear at {stop_file}")
        return

    if sentinel.requested:
        typer.echo(f"a stop is already pending at {stop_file}")
        typer.echo(f"  reason: {sentinel.reason}")
        typer.echo("The loop acts on it at its next bar boundary.")
        return

    sentinel.request(reason)
    typer.echo(f"stop requested at {stop_file}")
    if reason:
        typer.echo(f"  reason: {reason}")
    typer.echo("")
    typer.echo(
        "The loop finishes the bar it is on, records why it stopped, alerts, "
        "and exits.\nIt clears the file itself, so the next run starts clean."
    )
    typer.echo("If no loop is running, nothing happens and the file stays.")
