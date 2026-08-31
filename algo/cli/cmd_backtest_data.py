"""``algo backtest-bhavcopy`` and ``algo backtest-smartapi`` commands."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import typer

from algo.cli._helpers import calendar_for, strangle_from_config
from algo.config.loader import load_config
from algo.config.schema import RunSettings

app = typer.Typer()


@app.command("backtest-bhavcopy")
def backtest_bhavcopy(
    path: Path = typer.Argument(..., help="A bhavcopy CSV, or a directory of them."),
    symbol: str = typer.Option("GOLDM", help="Which underlying to trade."),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="A config file whose sizing, caps, kill switch, devolvement window, "
        "strategy parameters and equity this run uses",
    ),
    min_volume: int = typer.Option(
        1,
        "--min-volume",
        help="Minimum day volume before a strike is considered quotable "
        "(bhavcopy has no bid/ask of its own, so this is the only tradeability gate)",
    ),
    spread_ticks: int = typer.Option(
        2,
        "--spread-ticks",
        help="Modelled spread, in ticks - this is what actually moves the fill "
        "price away from the mid; see the command's --help",
    ),
    risk_free_rate: float = typer.Option(0.065, "--risk-free-rate"),
    tearsheet: Path | None = typer.Option(None, help="Write an HTML tearsheet here"),
    trade_log: Path | None = typer.Option(None, help="Write the trade log CSV here"),
    state: Path | None = typer.Option(
        None,
        "--state",
        help="Stream equity, positions, signals, notes, trades and the option "
        "chain to this dashboard state file as the run progresses",
    ),
) -> None:
    """Run the real GOLDM short-strangle strategy over recorded MCX bhavcopy history.

    Where `backtest` proves the engine's cost arithmetic on generated data and
    trades nothing resembling the actual strategy, this runs `DeltaStrangle`
    itself against every real monthly cycle the bhavcopy archive covers.

    It is a **shape test**, not a fill-accurate one, and says so in its own
    output. Bhavcopy is end-of-day: there is no 09:30 print and no intraday grid,
    so each session becomes exactly two bars - entry (09:30 IST, priced from the
    day's open) and close (the real session close, priced from the day's close,
    high and low). Every exit check in between - a stop that would have fired and
    reversed by the close - is invisible to this run. See
    algo/backtest/bhavcopy_runner.py and D-081 through D-085 for the rest of what
    that trades away, and algo/data/bhavcopy.py for why the column mapping itself
    is an unverified assumption until checked against a real file.
    """
    from algo.backtest.bhavcopy_runner import build_dataset
    from algo.backtest.engine import BacktestEngine
    from algo.backtest.prices import ChainFeedProvider, ChainPriceSource, CompositePriceSource
    from algo.core.bar import Timeframe
    from algo.core.errors import DataError
    from algo.costs.charges import McxChargeModel
    from algo.costs.margin import SpanApproxMargin
    from algo.costs.slippage import TickSlippage
    from algo.costs.spread import FixedTickSpread
    from algo.data.bhavcopy import load_directory, parse_rows
    from algo.exchange.specs import ContractSpecStore
    from algo.execution.fills import FillSimulator
    from algo.persistence.state import StateStore
    from algo.portfolio.book import Portfolio
    from algo.reporting import metrics as metrics_mod
    from algo.risk.devolvement import DevolvementGuard
    from algo.risk.engine import FixedLotSizer, RiskEngine
    from algo.risk.killswitch import KillSwitch
    from algo.strategy.delta_strangle import DeltaStrangle

    calendar = calendar_for(config)

    try:
        rows = (
            load_directory(path, symbol=symbol)
            if path.is_dir()
            else parse_rows(path, symbols=frozenset({symbol}))
        )
        typer.echo(f"read {path}")
        dataset = build_dataset(
            rows,
            symbol=symbol,
            calendar=calendar,
            min_volume=min_volume,
            risk_free_rate=risk_free_rate,
        )
    except DataError as exc:
        typer.echo("")
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"sessions       {dataset.sessions_used} used, {len(dataset.skipped_sessions)} skipped"
    )
    for reason in dataset.skipped_sessions:
        typer.echo(f"  ! {reason}")
    if not dataset.bars:
        typer.echo("no usable sessions - nothing to run")
        raise typer.Exit(code=1)

    if config is not None:
        cfg = load_config(config)
        settings = RunSettings.from_config(cfg)
        strategy = strangle_from_config(cfg, symbol)
        typer.echo(
            f"config         {config} ({settings.lots} lot(s), equity "
            f"{settings.starting_equity})"
        )
    else:
        settings = RunSettings.defaults()
        strategy = DeltaStrangle(underlying=symbol)
    kill_switch = KillSwitch(
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_consecutive_losses=settings.max_consecutive_losses,
        max_drawdown_pct=settings.max_drawdown_pct,
    )

    store: StateStore | None = None
    if state is not None:
        store = StateStore(state)

    try:
        engine = BacktestEngine(
            bars=dataset.bars,
            calendar=calendar,
            specs=ContractSpecStore.default(),
            strategy=strategy,
            risk=RiskEngine(
                sizer=FixedLotSizer(settings.lots),
                spec_for=None,
                max_concurrent_positions=2,  # a strangle is two positions
                max_lots_per_underlying=settings.max_lots_per_underlying,
                margin_cap_pct=settings.margin_cap_pct,
            ),
            simulator=FillSimulator(
                spread=FixedTickSpread(spread_ticks),
                slippage=TickSlippage(market_ticks=0, stop_ticks=2),
                charges=McxChargeModel.default(),
            ),
            portfolio=Portfolio(settings.starting_equity),
            instrument=dataset.instrument,
            timeframe=Timeframe(minutes=30),
            is_option=True,
            price_source=CompositePriceSource(ChainPriceSource(dataset.chain_snapshots)),
            chain_provider=ChainFeedProvider(dataset.chain_snapshots),
            expiries=dataset.expiries,
            devolvement=DevolvementGuard(
                calendar=calendar,
                force_exit_sessions_before_expiry=settings.force_exit_sessions_before_expiry,
                block_new_entries_within_dte=settings.block_new_entries_within_dte,
            ),
            kill_switch=kill_switch,
            flatten_on_trip=settings.flatten_on_trip,
            margin=SpanApproxMargin(),
            state=store,
            mode="backtest",
            broker="backtest",
            stop_viability_threshold=settings.stop_viability_threshold,
            on_stop_viability_breach=settings.on_stop_viability_breach,
        )
        result = engine.run()
    finally:
        if store is not None:
            store.close()

    typer.echo("")
    typer.echo(f"cycles         {len(result.trades)} round trip(s)")
    summary = metrics_mod.compute(
        result.equity_curve,
        trade_count=result.round_trips,
        total_cost=result.realised_cost,
        trades=result.trades,
    )
    typer.echo(summary.summary())
    typer.echo("")
    typer.echo(f"  spread paid     {result.spread_cost:,}")
    typer.echo(f"  charges paid    {result.total_charges:,}")
    for warning in result.warnings:
        typer.echo(f"  ! {warning}")
    typer.echo("  ! SHAPE TEST ONLY - two ticks a day (open, close), not a real")
    typer.echo("    intraday grid. See the command's --help for what that trades away.")

    if trade_log is not None:
        from algo.reporting.export import write_trade_log

        typer.echo(f"  trade log  -> {write_trade_log(result.trades, trade_log)}")
    if tearsheet is not None:
        from algo.reporting.tearsheet import render, write

        markup = render(
            title=f"GOLDM strangle - {symbol} bhavcopy",
            metrics=summary,
            curve=result.equity_curve,
            trades=result.trades,
            warnings=(*result.warnings, "SHAPE TEST: two ticks a day, not a real intraday grid"),
            dataset_hash=result.dataset_hash,
            config_hash=result.config_hash,
        )
        typer.echo(f"  tearsheet  -> {write(tearsheet, markup)}")


@app.command("backtest-smartapi")
def backtest_smartapi(
    expiry: str = typer.Argument(
        ..., help="Option expiry to trade, YYYY-MM-DD - must be currently listed "
        "and not yet expired (SmartAPI cannot serve an expired contract)"
    ),
    symbol: str = typer.Option("GOLDM", help="Which underlying to trade."),
    since: str | None = typer.Option(
        None, help="Start date YYYY-MM-DD (default: 25 calendar days before expiry)"
    ),
    until: str | None = typer.Option(None, help="End date YYYY-MM-DD (default: today)"),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="A config file whose sizing, caps, kill switch, devolvement window, "
        "strategy parameters and equity this run uses",
    ),
    strike_band_pct: float = typer.Option(
        0.10,
        "--strike-band-pct",
        help="Fetch strikes within this fraction beyond the underlying's observed "
        "high/low - wider covers more of a 0.25-delta wing's possible range, at "
        "the cost of more real API calls",
    ),
    rate_limit_s: float = typer.Option(0.35, "--rate-limit-s", help="Pause between candle calls"),
    max_contracts: int = typer.Option(
        120, "--max-contracts", help="Refuse to fetch more than this many contracts"
    ),
    risk_free_rate: float = typer.Option(0.065, "--risk-free-rate"),
    refresh_master: bool = typer.Option(
        False, "--refresh-master", help="Re-download the instrument master first"
    ),
    tearsheet: Path | None = typer.Option(None, help="Write an HTML tearsheet here"),
    trade_log: Path | None = typer.Option(None, help="Write the trade log CSV here"),
    state: Path | None = typer.Option(
        None,
        "--state",
        help="Stream equity, positions, signals, notes, trades and the option "
        "chain to this dashboard state file as the run progresses",
    ),
) -> None:
    """Run the real GOLDM short-strangle strategy over real SmartAPI history.

    Where `backtest-bhavcopy` covers ~100 past cycles at two ticks a day, this
    covers exactly **one** - whichever `expiry` is currently listed - at real
    30-minute resolution. Angel One's own candle API cannot serve a contract
    that has already expired, so this is structurally the only cycle it can
    ever answer for. The two commands are complements: bhavcopy answers "has
    this ever worked, across many cycles"; this answers "what would this
    month's trade actually have looked like, at real intraday resolution".

    Connects to your real SmartAPI account (ALGO_SMARTAPI_* in .env) and makes
    one real, rate-limited network call per strike fetched - see
    algo/backtest/smartapi_runner.py for exactly how the strike band and the
    rate limit are chosen, and why. No orders are placed; this only reads
    historical candles.
    """
    from datetime import time as _time
    from datetime import timedelta

    from algo.backtest.engine import BacktestEngine
    from algo.backtest.prices import ChainFeedProvider, ChainPriceSource, CompositePriceSource
    from algo.backtest.smartapi_runner import build_dataset
    from algo.core.bar import Timeframe
    from algo.core.clock import SystemClock
    from algo.core.errors import DataError
    from algo.core.timeutil import ist_to_utc
    from algo.costs.charges import McxChargeModel
    from algo.costs.margin import SpanApproxMargin
    from algo.costs.slippage import TickSlippage
    from algo.costs.spread import FixedTickSpread
    from algo.data.smartapi_feed import (
        SmartConnectTransport,
    )
    from algo.data.smartapi_feed import (
        credentials_from_env as smart_credentials_from_env,
    )
    from algo.exchange.master import HttpMasterSource, InstrumentMaster, fetch_master
    from algo.exchange.specs import ContractSpecStore
    from algo.execution.fills import FillSimulator
    from algo.execution.kotak import default_totp
    from algo.persistence.state import StateStore
    from algo.portfolio.book import Portfolio
    from algo.reporting import metrics as metrics_mod
    from algo.risk.devolvement import DevolvementGuard
    from algo.risk.engine import FixedLotSizer, RiskEngine
    from algo.risk.killswitch import KillSwitch
    from algo.strategy.delta_strangle import DeltaStrangle

    option_expiry = date.fromisoformat(expiry)
    calendar = calendar_for(config)
    clock = SystemClock()

    credentials = smart_credentials_from_env()
    if not credentials.has_all():
        typer.echo("  ! ALGO_SMARTAPI_* credentials are missing (see .env.example)")
        raise typer.Exit(code=1)

    snapshot_path = Path("state/master_mcx.json")
    if refresh_master or not snapshot_path.exists():
        typer.echo(f"fetching instrument master -> {snapshot_path}")
        fetch_master(HttpMasterSource(), snapshot_path, now=clock.now())
    master = InstrumentMaster.from_snapshot(snapshot_path)
    typer.echo(f"master snapshot fetched {master.fetched_at:%Y-%m-%d %H:%M}Z")

    since_dt = (
        ist_to_utc(date.fromisoformat(since), _time(9, 0))
        if since is not None
        else ist_to_utc(option_expiry - timedelta(days=25), _time(9, 0))
    )
    until_dt = (
        ist_to_utc(date.fromisoformat(until), _time(23, 59)) if until is not None else clock.now()
    )

    transport = SmartConnectTransport(credentials.api_key)
    typer.echo(f"logging in to SmartAPI as {credentials.client_id}")
    transport.connect(
        credentials.client_id, credentials.password, default_totp(credentials.totp_seed)()
    )
    typer.echo("login OK")

    def progress(message: str) -> None:
        typer.echo(f"  {message}")

    try:
        dataset = build_dataset(
            transport,
            master,
            symbol=symbol,
            option_expiry=option_expiry,
            calendar=calendar,
            since=since_dt,
            until=until_dt,
            strike_band_pct=Decimal(str(strike_band_pct)),
            rate_limit_s=rate_limit_s,
            max_contracts=max_contracts,
            risk_free_rate=risk_free_rate,
            on_progress=progress,
        )
    except DataError as exc:
        typer.echo("")
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"contracts      {dataset.contracts_fetched} fetched, "
        f"{len(dataset.contracts_skipped_empty)} had no bars in this window"
    )
    typer.echo(f"bars           {len(dataset.bars)}   snapshots {len(dataset.chain_snapshots)}")
    if not dataset.chain_snapshots:
        typer.echo("no usable chain snapshots - nothing to run")
        raise typer.Exit(code=1)

    if config is not None:
        cfg = load_config(config)
        settings = RunSettings.from_config(cfg)
        strategy = strangle_from_config(cfg, symbol)
        typer.echo(
            f"config         {config} ({settings.lots} lot(s), equity "
            f"{settings.starting_equity})"
        )
    else:
        settings = RunSettings.defaults()
        strategy = DeltaStrangle(underlying=symbol)
    kill_switch = KillSwitch(
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_consecutive_losses=settings.max_consecutive_losses,
        max_drawdown_pct=settings.max_drawdown_pct,
    )

    store: StateStore | None = None
    if state is not None:
        store = StateStore(state)

    try:
        engine = BacktestEngine(
            bars=dataset.bars,
            calendar=calendar,
            specs=ContractSpecStore.default(),
            strategy=strategy,
            risk=RiskEngine(
                sizer=FixedLotSizer(settings.lots),
                spec_for=None,
                max_concurrent_positions=2,  # a strangle is two positions
                max_lots_per_underlying=settings.max_lots_per_underlying,
                margin_cap_pct=settings.margin_cap_pct,
            ),
            simulator=FillSimulator(
                spread=FixedTickSpread(2),
                slippage=TickSlippage(market_ticks=0, stop_ticks=2),
                charges=McxChargeModel.default(),
            ),
            portfolio=Portfolio(settings.starting_equity),
            instrument=dataset.instrument,
            timeframe=Timeframe(minutes=30),
            is_option=True,
            price_source=CompositePriceSource(ChainPriceSource(dataset.chain_snapshots)),
            chain_provider=ChainFeedProvider(dataset.chain_snapshots),
            expiries=dataset.expiries,
            devolvement=DevolvementGuard(
                calendar=calendar,
                force_exit_sessions_before_expiry=settings.force_exit_sessions_before_expiry,
                block_new_entries_within_dte=settings.block_new_entries_within_dte,
            ),
            kill_switch=kill_switch,
            flatten_on_trip=settings.flatten_on_trip,
            margin=SpanApproxMargin(),
            state=store,
            mode="backtest",
            broker="backtest",
            stop_viability_threshold=settings.stop_viability_threshold,
            on_stop_viability_breach=settings.on_stop_viability_breach,
        )
        result = engine.run()
    finally:
        if store is not None:
            store.close()

    typer.echo("")
    typer.echo(f"cycles         {len(result.trades)} round trip(s)")
    summary = metrics_mod.compute(
        result.equity_curve,
        trade_count=result.round_trips,
        total_cost=result.realised_cost,
        trades=result.trades,
    )
    typer.echo(summary.summary())
    typer.echo("")
    typer.echo(f"  spread paid     {result.spread_cost:,}")
    typer.echo(f"  charges paid    {result.total_charges:,}")
    for warning in result.warnings:
        typer.echo(f"  ! {warning}")
    typer.echo("  ! REAL 30-MINUTE DATA, ONE CYCLE ONLY - the option chain has no real")
    typer.echo("    order book (candles, not quotes); the spread is still modelled.")

    if trade_log is not None:
        from algo.reporting.export import write_trade_log

        typer.echo(f"  trade log  -> {write_trade_log(result.trades, trade_log)}")
    if tearsheet is not None:
        from algo.reporting.tearsheet import render, write

        markup = render(
            title=f"GOLDM strangle - {symbol} {expiry} (SmartAPI)",
            metrics=summary,
            curve=result.equity_curve,
            trades=result.trades,
            warnings=(*result.warnings, "REAL 30-MIN DATA, ONE CYCLE ONLY - spread still modelled"),
            dataset_hash=result.dataset_hash,
            config_hash=result.config_hash,
        )
        typer.echo(f"  tearsheet  -> {write(tearsheet, markup)}")
