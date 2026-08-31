"""``algo killswitch`` — inspect or manually trip the trading halt."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer()


@app.command("killswitch")
def kill_switch(
    state: Path = typer.Option(Path("state/dashboard.db"), help="Engine state file"),
    trip: bool = typer.Option(False, "--trip", help="Request a halt"),
    reset: bool = typer.Option(False, "--reset", help="Clear a halt (deliberate act)"),
    reason: str = typer.Option("", help="Why. Required with --trip"),
    flatten: bool = typer.Option(False, help="Also close open positions"),
) -> None:
    """Inspect, request, or clear the trading halt. Brief section 2.2.

    With no flags it reports. `--trip` records a halt request the engine acts on at
    its next bar. `--reset` clears one.

    Reset exists **only here**, not in the dashboard (D-066). Tripping the switch
    is one click because stopping should be easy; clearing it deserves a terminal,
    a look at why it tripped, and a person who has done both.
    """
    from algo.core.clock import SystemClock
    from algo.persistence.state import StateStore

    if trip and reset:
        raise typer.BadParameter("--trip and --reset are opposites; pick one")

    clock = SystemClock()
    with StateStore(state) as store:
        if trip:
            if not reason.strip():
                raise typer.BadParameter(
                    "--trip needs a --reason. Three weeks from now, 'why did trading "
                    "stop' has to have an answer, and this is the only moment anyone "
                    "knows it."
                )
            if flatten:
                typer.echo(
                    "  ! flatten will market-close every open position, including a "
                    "short strangle that may be mid-move. Halting alone stops new "
                    "orders and leaves positions untouched."
                )
                typer.confirm("Flatten anyway?", abort=True)
            request_id = store.request_kill_switch(
                requested_by="cli", reason=reason.strip(), flatten=flatten, at=clock.now()
            )
            typer.echo(f"halt requested (id {request_id}).")
            typer.echo("The engine acts on this at its next bar - it is not halted yet.")
            return

        if reset:
            pending = store.pending_kill_switch_requests()
            if not pending:
                typer.echo("nothing pending to clear.")
            for request in pending:
                store.mark_kill_switch_acted(request.id, at=clock.now())
                typer.echo(f"cleared request {request.id}: {request.reason}")
            typer.echo("")
            typer.echo(
                "Note: this clears the pending requests. The engine's own switch "
                "resets when it next starts a session and sees none outstanding."
            )
            return

        health = store.health()
        typer.echo(f"kill switch   {health.get('kill_switch', 'unknown')}")
        typer.echo(f"engine        {health.get('engine', 'unknown')}")
        history = store.kill_switch_requests(limit=5)
        if not history:
            typer.echo("no halt requests recorded.")
            return
        typer.echo("")
        typer.echo("recent requests:")
        for request in history:
            acted = request.acted_on_at.isoformat() if request.acted_on_at else "PENDING"
            typer.echo(
                f"  {request.id}  {request.requested_at:%Y-%m-%d %H:%M}  "
                f"by {request.requested_by:<10} {acted:<26} {request.reason}"
            )
