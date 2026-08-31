"""``algo bhavcopy`` and ``algo chain`` — inspect options chains."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:  # annotations only - `_chain_payload`'s body never runs
    # without its caller having already imported these lazily.
    from datetime import datetime

    from algo.core.chain import OptionChainSnapshot

app = typer.Typer()


@app.command("bhavcopy")
def inspect_bhavcopy(
    path: Path = typer.Argument(..., help="A bhavcopy CSV, or a directory of them."),
    symbol: str = typer.Option("GOLDM", help="Which underlying to report on."),
    expiry: str | None = typer.Option(
        None, help="Print one cycle's chain, as YYYY-MM-DD."
    ),
) -> None:
    """Check a downloaded MCX bhavcopy against the expected layout, and report coverage.

    Run this first on a real file. The column mapping in the loader is a stated
    assumption - MCX serves the file through a browser flow behind bot protection,
    so no sample could be fetched to confirm the headers. This command either
    parses cleanly or prints the columns the file actually has next to the ones
    the loader wanted, which makes correcting it a config change.

    Coverage matters as much as the schema. A hundred cycles of history is only
    worth having if the strikes the strategy wants were changing hands, and the
    percentage of the ladder that traded is the honest answer to that.
    """
    from algo.core.errors import DataError
    from algo.data.bhavcopy import (
        BhavcopyChainFeed,
        build_snapshots,
        coverage,
        load_directory,
        parse_rows,
    )

    try:
        if path.is_dir():
            rows = load_directory(path, symbol=symbol)
            typer.echo(f"read {len(sorted(path.glob('*.csv')))} files from {path}")
        else:
            rows = parse_rows(path, symbols=frozenset({symbol}))
            typer.echo(f"read {path}")
    except DataError as exc:
        typer.echo("")
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo("")
    typer.echo(coverage(rows, symbol=symbol))

    snapshots = build_snapshots(rows, symbol=symbol)
    typer.echo("")
    typer.echo(f"built {len(snapshots)} chain snapshots")
    if not snapshots:
        typer.echo("")
        typer.echo("  No snapshots. A chain is dropped when the file has no futures")
        typer.echo("  close for that session - options are priced off the future, and")
        typer.echo("  without it every delta would be invented. Check that the file")
        typer.echo("  contains the FUTCOM rows as well as the OPTFUT ones.")
        return

    feed = BhavcopyChainFeed(snapshots, underlying=symbol)
    typer.echo(f"cycles: {', '.join(str(e) for e in feed.expiries())}")

    if expiry is None:
        typer.echo("")
        typer.echo("Pass --expiry YYYY-MM-DD to print one cycle's ladder.")
        return

    wanted = date.fromisoformat(expiry)
    series = list(feed.snapshots(wanted))
    if not series:
        typer.echo(f"no snapshots for {wanted}")
        raise typer.Exit(code=1)

    latest = series[-1]
    typer.echo("")
    typer.echo(f"{wanted} as of {latest.ts:%Y-%m-%d}  future {latest.futures_price}")
    typer.echo(f"  {'strike':>10} {'right':>5} {'close':>10} {'volume':>9} {'OI':>9}")
    for row in latest.rows:
        typer.echo(
            f"  {row.strike:>10} {row.right.value:>5} {row.quote.ltp:>10}"
            f" {row.quote.volume:>9,} {row.quote.open_interest:>9,}"
        )
    typer.echo("")
    typer.echo("  No bid/ask column exists in this file, so none is shown. The spread")
    typer.echo("  stays an assumption until the recorder has live quotes.")


@app.command("chain")
def inspect_chain(
    path: Path = typer.Argument(..., help="A scraped live MCX option-chain .xlsx"),
    risk_free_rate: float = typer.Option(0.065, "--risk-free-rate"),
    futures_expiry: str | None = typer.Option(
        None,
        "--futures-expiry",
        help="The futures contract these options settle into, as YYYY-MM-DD. "
        "Defaults to the option expiry (see the loader's docstring, Q1c).",
    ),
    state: Path | None = typer.Option(
        None,
        "--state",
        help="Record this chain to a dashboard state file, so the chain panel "
        "shows a real book instead of the last backtest's modelled one",
    ),
) -> None:
    """Read a scraped live option chain, solve its greeks, and report what is tradeable.

    This is the only source in the project carrying a **real bid and ask**.
    Bhavcopy has no book at all, so tradeability there rests on `assume_spread`'s
    invention; this is measured. That makes it the honest answer to "which
    strikes could the strategy actually have sold", and the wrong tool for
    anything historical - it is one instant, not a series.

    Rows that will not solve stay unpriced and untradeable rather than borrowing
    a neighbour's volatility (D-005).
    """
    from datetime import time as _time

    from algo.core.enums import Right
    from algo.core.errors import DataError
    from algo.core.timeutil import ist_to_utc
    from algo.data.mcx_chain_excel import load_chain
    from algo.pricing.chain_greeks import atm_iv, enrich

    try:
        snapshot = load_chain(
            path,
            futures_expiry=date.fromisoformat(futures_expiry) if futures_expiry else None,
        )
    except DataError as exc:
        typer.echo("")
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    expires_at = ist_to_utc(snapshot.option_expiry, _time(23, 30))
    chain = enrich(snapshot, expires_at=expires_at, r=risk_free_rate)

    strikes = sorted({r.strike for r in chain.rows})
    tradeable = [r for r in chain.rows if r.is_tradeable]
    unpriced = [r for r in chain.rows if r.delta is None]
    reference_vol = atm_iv(chain)

    typer.echo(f"{chain.underlying} {chain.option_expiry}  as of {chain.ts:%Y-%m-%d %H:%M} UTC")
    typer.echo(f"  future    {chain.futures_price}")
    typer.echo(f"  strikes   {len(strikes)}  ({strikes[0]} to {strikes[-1]})")
    typer.echo(f"  tradeable {len(tradeable)} of {len(chain.rows)} rows")
    typer.echo(f"  unpriced  {len(unpriced)} (quoted too thin to solve, or not quoted)")
    if reference_vol is not None:
        typer.echo(f"  ATM vol   {reference_vol * 100:.2f}% at {chain.atm_strike()}")

    typer.echo("")
    typer.echo("  strikes the strategy's delta targets would land on:")
    for target in (0.15, 0.20, 0.25, 0.30):
        call = chain.nearest_delta(right=Right.CE, target=target, tolerance=0.05)
        put = chain.nearest_delta(right=Right.PE, target=target, tolerance=0.05)
        call_text = f"{call.strike} CE @ {call.delta:+.3f}" if call else "none in tolerance"
        put_text = f"{put.strike} PE @ {put.delta:+.3f}" if put else "none in tolerance"
        typer.echo(f"    delta {target:.2f}   {call_text:>26}   {put_text:>26}")

    if state is not None:
        from algo.persistence.state import StateStore

        store = StateStore(state)
        try:
            store.record_chain_snapshot(_chain_payload(chain, expires_at=expires_at))
        finally:
            store.close()
        typer.echo("")
        typer.echo(f"  recorded to {state}")


def _chain_payload(chain: OptionChainSnapshot, *, expires_at: datetime) -> dict[str, object]:
    """The dashboard's chain-panel payload, in the same shape the engine writes.

    Kept beside the command rather than shared with `BacktestEngine`: the engine
    knows a session count and a devolvement deadline because it is running a
    strategy through a calendar, and this is a single snapshot with neither. The
    honest values here are a real day count and `None`, not a borrowed one.
    """
    from algo.core.timeutil import iso

    days_left = (expires_at.date() - chain.ts.date()).days
    return {
        "ts": iso(chain.ts),
        "underlying": chain.underlying,
        "option_expiry": chain.option_expiry.isoformat(),
        "futures_price": str(chain.futures_price),
        "dte": days_left,
        "forced_exit_in_sessions": None,
        "rows": [
            {
                "strike": str(row.strike),
                "right": row.right.value,
                "bid": str(row.quote.bid) if row.quote.bid is not None else None,
                "ask": str(row.quote.ask) if row.quote.ask is not None else None,
                "ltp": str(row.quote.ltp) if row.quote.ltp is not None else None,
                "volume": row.quote.volume,
                "iv": row.iv,
                "delta": row.delta,
                "tradeable": row.is_tradeable,
                "flag": row.quote.status().value,
                "held": False,
            }
            for row in chain.rows
        ],
    }
