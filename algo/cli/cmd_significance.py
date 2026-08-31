"""``algo significance`` - is a strategy's result distinguishable from luck?

The counterpart to `algo mt5-replay`, which reports what a strategy earned.
This reports whether that number means anything: a bootstrap interval around
it, and a permutation test against a market with the same returns and no
structure. `algo/reporting/significance.py` sets out what each one can and
cannot answer.

Its own command rather than a flag on the replay, because it is a different
kind of run: it re-executes the whole backtest once per permutation, takes
minutes rather than a second, and answers a question about the result rather
than producing one.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer()


@app.command("significance")
def significance(
    strategy_name: str = typer.Option("macd", "--strategy", help="breakout | macd"),
    timeframe_minutes: int = typer.Option(60, "--timeframe", help="Bar interval, minutes"),
    symbol: str = typer.Option("XAUUSD", help="MT5 symbol"),
    lookback: int = typer.Option(20, help="Donchian channel length (breakout only)"),
    stop_loss_pct: str = typer.Option("0", help="Flat stop, % of entry. 0 disables"),
    trail_activation_pct: str = typer.Option("2", help="Profit % at which the trail arms"),
    trail_pct: str = typer.Option("0", help="Trail distance, % behind peak. 0 disables"),
    lots: int = typer.Option(100, help="Ounces per trade"),
    bars: int = typer.Option(20000, help="Bars of MT5 history to test over"),
    permutations: int = typer.Option(
        1000, help="Shuffled series to build the null distribution from"
    ),
    resamples: int = typer.Option(10000, help="Bootstrap resamples of the trade sequence"),
    seed: int = typer.Option(0, help="Seed. Same seed, same answer, on any machine"),
    json_out: Path = typer.Option(
        None, "--json", help="Also write the full result to this file"
    ),
) -> None:
    """Bootstrap the result and permutation-test the edge.

    The permutation test is the one that matters and the one that takes the
    time: each permutation rebuilds the price series from the same bar returns
    in a shuffled order and re-runs the entire backtest, costs included. What
    the strategy earns on those series is what it earns with no edge at all.

    A result inside that distribution is not evidence of a strategy. It is
    evidence of a strategy-shaped fit to one price path.
    """
    import json as _json

    import MetaTrader5 as mt5

    from algo.backtest.research import run_significance_study
    from algo.core.errors import AlgoError

    if not mt5.initialize():
        typer.echo(f"MT5 will not initialize: {mt5.last_error()}")
        typer.echo("Is the terminal running and logged in?")
        raise typer.Exit(code=1)

    try:
        typer.echo(
            f"testing       {strategy_name} on {timeframe_minutes}m {symbol}, "
            f"{bars} bars, {lots} oz"
        )
        typer.echo(
            f"null          {permutations} permutations, each a full backtest "
            "with costs charged\n"
        )

        with typer.progressbar(length=permutations, label="  permuting") as progress:
            done = 0

            def tick(current: int, _total: int) -> None:
                nonlocal done
                progress.update(current - done)
                done = current

            try:
                out = run_significance_study(
                    mt5,
                    strategy=strategy_name,
                    timeframe_minutes=timeframe_minutes,
                    symbol=symbol,
                    permutations=permutations,
                    resamples=resamples,
                    seed=seed,
                    progress=tick,
                    params={
                        "lookback": str(lookback),
                        "stop_loss_pct": stop_loss_pct,
                        "trail_activation_pct": trail_activation_pct,
                        "trail_pct": trail_pct,
                        "lots": str(lots),
                        "bars": str(bars),
                    },
                )
            except AlgoError as exc:
                raise typer.BadParameter(str(exc)) from exc

        observed = out["observed"]
        typer.echo(
            f"\nwindow        {out['window_start'][:10]} to {out['window_end'][:10]}"
            f"  ({out['bars']} bars)"
        )
        typer.echo(
            f"result        net {observed['net_pnl']} over {observed['trades']} trades"
            f"  (spread {observed['spread_paid']}, swap {observed['swap_paid']})\n"
        )

        typer.echo("BOOTSTRAP - how firm is the number?")
        typer.echo(out["bootstrap"]["summary"])
        typer.echo("\nPERMUTATION - is there an edge at all?")
        typer.echo(out["permutation"]["summary"])

        typer.echo("\nRead these before quoting either figure:")
        for caveat in out["caveats"]:
            typer.echo(f"  - {caveat}")

        if json_out is not None:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(_json.dumps(out, indent=2), encoding="utf-8")
            typer.echo(f"\nwritten       {json_out}")
    finally:
        mt5.shutdown()
