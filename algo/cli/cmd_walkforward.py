"""``algo walkforward`` — assess data feasibility before backtesting."""

from __future__ import annotations

from datetime import date

import typer

app = typer.Typer()


@app.command("walkforward")
def walk_forward_feasibility(
    years: float = typer.Option(2.0, help="Years of recorded data available"),
    trades_per_year: int = typer.Option(12, help="Trades the strategy makes per year"),
    in_sample_days: int = typer.Option(180, help="Length of each optimise window"),
    out_of_sample_days: int = typer.Option(90, help="Length of each validate window"),
) -> None:
    """Can a walk-forward on this much data tell you anything?

    Not a backtest. This answers the question Milestone 5 raises before the data
    exists: given a strategy that trades roughly `trades_per_year` times, how many
    out-of-sample trades would a rolling walk-forward actually validate against,
    and is that enough for any ratio to mean something?

    For a monthly-cycle strangle the answer is usually no, and knowing that before
    spending months recording is worth more than the analysis itself.
    """
    from datetime import timedelta

    from algo.backtest.walkforward import MIN_OOS_TRADES, assess_feasibility, rolling_windows

    start = date(2026, 1, 1)
    end = start + timedelta(days=int(years * 365))
    windows = rolling_windows(
        start=start,
        end=end,
        in_sample_days=in_sample_days,
        out_of_sample_days=out_of_sample_days,
    )

    typer.echo(f"data available      {years:g} years ({start} .. {end})")
    typer.echo(f"strategy cadence    ~{trades_per_year} trades per year")
    typer.echo(f"windows             {in_sample_days}d optimise / {out_of_sample_days}d validate")
    typer.echo("")

    if not windows:
        typer.echo(
            f"  no complete window fits in {years:g} years at "
            f"{in_sample_days}+{out_of_sample_days} days. Walk-forward is not possible."
        )
        return

    per_window = max(round(trades_per_year * out_of_sample_days / 365), 0)
    verdict = assess_feasibility(
        len(windows), per_window * len(windows), [per_window] * len(windows)
    )

    typer.echo(f"  windows              {len(windows)}")
    typer.echo(f"  OOS trades / window  ~{per_window}")
    typer.echo(f"  OOS trades total     ~{verdict.oos_trades}")
    typer.echo("")
    typer.echo(f"  {verdict.confidence}: {verdict.message}")

    if not verdict.supports_a_conclusion:
        needed_years = MIN_OOS_TRADES / max(trades_per_year, 1)
        typer.echo("")
        typer.echo(
            f"  To reach {MIN_OOS_TRADES} out-of-sample trades at {trades_per_year} a year "
            f"would take roughly {needed_years:.1f} years of validated data,"
        )
        typer.echo(
            "  and rather more than that of recording, since the first in-sample "
            "window is consumed before any validation begins."
        )
