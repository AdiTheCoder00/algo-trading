"""``algo live-mt5`` and ``algo mt5-replay`` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from algo.exchange.specs import ContractSpecStore

app = typer.Typer()


@app.command("live-mt5")
def live_mt5(
    strategy_name: str = typer.Option("breakout", "--strategy", help="breakout | macd"),
    timeframe_minutes: int = typer.Option(30, "--timeframe", help="Bar interval, minutes"),
    symbol: str = typer.Option("XAUUSD", help="MT5 symbol"),
    lookback: int = typer.Option(20, help="Donchian channel length (breakout only)"),
    stop_loss_pct: str = typer.Option("0", help="Flat stop, % of entry. 0 disables"),
    trail_activation_pct: str = typer.Option("2", help="Profit % at which the trail arms"),
    trail_pct: str = typer.Option("0", help="Trail distance, % behind peak. 0 disables"),
    lots: int = typer.Option(100, help="Ounces per trade (1 lot = 1 oz = 0.01 MT5 lots)"),
    equity: str = typer.Option("100000", help="Starting equity for the paper book"),
    passes: int = typer.Option(1, help="Poll this many times, then stop"),
    poll_interval_s: float = typer.Option(30.0, help="Seconds between polls"),
    state: Path = typer.Option(
        Path("state/dashboard.db"), help="State file the dashboard reads"
    ),
    journal_path: Path = typer.Option(
        Path("state/mt5_journal.db"), "--journal", help="Order journal"
    ),
) -> None:
    """Run a CFD strategy on live MT5 bars against the **paper** broker.

    Real bars from a running terminal; fills simulated by the same
    `FillSimulator` the backtest uses. No order reaches the broker - see
    `algo/live/mt5_runner.py` for why the first live order is not something to
    place from inside an unattended loop.

    `--passes` is required to terminate: a trading loop with no stopping
    condition keeps trading after whoever was watching it has gone home.
    """
    import time as _time
    from decimal import Decimal as _D

    import MetaTrader5 as mt5

    from algo.core.bar import Timeframe
    from algo.core.clock import SystemClock
    from algo.core.enums import Exchange
    from algo.core.instrument import CfdId
    from algo.core.timeutil import iso
    from algo.costs.cfd import CfdChargeModel
    from algo.costs.slippage import NoSlippage
    from algo.costs.spread import FixedTickSpread
    from algo.data.mt5_feed import Mt5BarFeed, measure_server_offset
    from algo.exchange.forex_calendar import ForexCalendar
    from algo.execution.fills import FillSimulator
    from algo.execution.paper import PaperBroker
    from algo.live.mt5_runner import XAUUSD_SPREAD_TICKS, build_mt5_paper_loop, strategy_for
    from algo.persistence.journal import OrderJournal
    from algo.persistence.state import StateStore

    if not mt5.initialize():
        typer.echo(f"MT5 will not initialize: {mt5.last_error()}")
        typer.echo("Is the terminal running and logged in?")
        raise typer.Exit(code=1)

    try:
        account = mt5.account_info()
        if account is None:
            typer.echo("MT5 reports no account - log the terminal in first.")
            raise typer.Exit(code=1)
        typer.echo(f"account       {account.login} on {account.server}")
        typer.echo(f"balance       {account.balance} {account.currency}")
        typer.echo("broker        PAPER - no order reaches this account")

        clock = SystemClock()
        calendar = ForexCalendar()
        if not calendar.is_open(clock.now()):
            typer.echo(
                "market        CLOSED right now - this venue runs Sunday 22:00 to "
                "Friday 21:00 UTC.\n              Nothing to poll; exiting rather "
                "than looping over a stale tick."
            )
            raise typer.Exit(code=0)

        offset = measure_server_offset(mt5, symbol)
        typer.echo(f"server clock  {offset} from UTC (measured, not assumed)")

        timeframe = Timeframe(minutes=timeframe_minutes)
        instrument = CfdId(symbol=symbol)
        feed = Mt5BarFeed(
            terminal=mt5,
            symbol=symbol,
            timeframe=timeframe,
            server_offset=offset,
        )
        seed = list(feed.closed_bars(count=max(lookback + 40, 60)))
        typer.echo(f"bars          {len(seed)} closed, latest {iso(seed[-1].ts)}")

        store = StateStore(state)
        broker = PaperBroker(
            simulator=FillSimulator(
                spread=FixedTickSpread(XAUUSD_SPREAD_TICKS),
                slippage=NoSlippage(),
                charges=CfdChargeModel.vantage_standard(),
            ),
            specs=ContractSpecStore.default(),
            quote=lambda key: seed[-1].close if key == instrument.key else None,
            clock=clock,
            starting_cash=_D(equity),
            exchange=Exchange.OTC,
        )
        broker.connect()

        with OrderJournal(journal_path) as journal:
            run = build_mt5_paper_loop(
                bars=feed,
                broker=broker,
                clock=clock,
                strategy=strategy_for(
                    strategy_name,
                    instrument=instrument,
                    stop_loss_pct=_D(stop_loss_pct),
                    trail_activation_pct=_D(trail_activation_pct),
                    trail_pct=_D(trail_pct),
                    lookback=lookback,
                ),
                instrument=instrument,
                timeframe=timeframe,
                journal=journal,
                seed_bars=seed,
                starting_equity=_D(equity),
                lots=lots,
                max_lots=lots,
                state=store,
            )
            typer.echo(
                f"strategy      {strategy_name} on {timeframe.label}, {lots} oz, "
                f"stop {stop_loss_pct}% / trail {trail_pct}% from {trail_activation_pct}%"
            )
            typer.echo(f"polling       {passes} pass(es), {poll_interval_s}s apart\n")

            for result in run.loop.run(
                max_passes=passes,
                sleep=_time.sleep,
                poll_interval_s=poll_interval_s,
            ):
                typer.echo(f"  {iso(result.ts)}  {result.summary()}")
            store.close()
    finally:
        mt5.shutdown()


@app.command("mt5-replay")
def mt5_replay(
    strategy_name: str = typer.Option("breakout", "--strategy", help="breakout | macd"),
    timeframe_minutes: int = typer.Option(30, "--timeframe", help="Bar interval, minutes"),
    symbol: str = typer.Option("XAUUSD", help="MT5 symbol"),
    lookback: int = typer.Option(20, help="Donchian channel length (breakout only)"),
    stop_loss_pct: str = typer.Option("0", help="Flat stop, % of entry. 0 disables"),
    trail_activation_pct: str = typer.Option("2", help="Profit % at which the trail arms"),
    trail_pct: str = typer.Option("0", help="Trail distance, % behind peak. 0 disables"),
    lots: int = typer.Option(100, help="Ounces per trade (1 lot = 1 oz = 0.01 MT5 lots)"),
    equity: str = typer.Option("100000", help="Starting equity"),
    bars: int = typer.Option(5000, help="Bars of MT5 history to replay"),
    measured_spread: bool = typer.Option(
        True,
        "--measured-spread/--modelled-spread",
        help="Charge the spread measured from tick history, when a profile exists",
    ),
    state: Path = typer.Option(
        Path("state/mt5.db"), help="State file the dashboard reads"
    ),
) -> None:
    """Replay real MT5 history through the paper pipeline into a state file.

    The live loop (`algo live-mt5`) only produces a trade when the market is
    open and a bar closes - one every `--timeframe` minutes, and nothing at all
    over a weekend. This runs the *same* strategy, over the *same* real bars,
    with the *same* costs, and writes the whole history to the dashboard's state
    file so there is something real to read immediately.

    It is a **replay of history, not a live run**, and says so: `mode` is
    recorded as `backtest` and the dashboard shows it. Nothing here contacts a
    broker or places an order.

    Written to its own state file by default. The MCX and CFD venues settle in
    different currencies, and mixing them in one file would put the right number
    behind the wrong symbol.
    """
    from decimal import Decimal as _D

    import MetaTrader5 as mt5

    from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
    from algo.core.bar import Timeframe
    from algo.core.instrument import CfdId
    from algo.core.timeutil import iso
    from algo.data.mt5_history import fetch_history, resolve_server_offset
    from algo.data.mt5_spread import load_profile
    from algo.live.mt5_runner import strategy_for
    from algo.persistence.state import EquityRow, PositionRow, StateStore

    if not mt5.initialize():
        typer.echo(f"MT5 will not initialize: {mt5.last_error()}")
        raise typer.Exit(code=1)
    try:
        offset = resolve_server_offset(mt5, symbol)
        typer.echo(f"server clock  {offset.describe()}")
        timeframe = Timeframe(minutes=timeframe_minutes)
        instrument = CfdId(symbol=symbol)
        history = fetch_history(
            mt5, symbol=symbol, timeframe=timeframe, count=bars, offset=offset.offset
        )
    finally:
        mt5.shutdown()

    typer.echo(
        f"bars          {len(history)} closed, {iso(history[0].ts)} .. {iso(history[-1].ts)}"
    )

    # A measured profile replaces the flat constant when one has been sampled.
    # Absent, the constant stands and the dashboard keeps saying so - the run
    # never silently upgrades its own claim about what it charged.
    profile = load_profile(symbol) if measured_spread else None
    costs = CfdCosts(half_spread_at=profile.half_spread_at) if profile else CfdCosts()
    if profile:
        typer.echo(f"spread        {profile.describe()}")
    else:
        typer.echo(f"spread        modelled at {costs.half_spread * 2} flat (no tick profile)")

    result = run_cfd_backtest(
        history,
        instrument=instrument,
        timeframe=timeframe,
        strategy_factory=lambda: strategy_for(
            strategy_name,
            instrument=instrument,
            stop_loss_pct=_D(stop_loss_pct),
            trail_activation_pct=_D(trail_activation_pct),
            trail_pct=_D(trail_pct),
            lookback=lookback,
        ),
        stop_loss_pct=_D(stop_loss_pct),
        trail_activation_pct=_D(trail_activation_pct),
        trail_pct=_D(trail_pct),
        lots=lots,
        starting_equity=_D(equity),
        costs=costs,
    )

    state.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(state)
    try:
        # Charges accrue against the bar a trade *closed* on, so the curve's
        # charge column moves when the cost was actually paid rather than being
        # smeared evenly across the run.
        charged_by_ts = {
            trade.exit_ts: trade.spread_paid + trade.swap_paid + trade.commission_paid
            for trade in result.trades
            if trade.exit_ts is not None
        }
        realised_by_ts = {
            trade.exit_ts: trade.net_pnl
            for trade in result.trades
            if trade.exit_ts is not None
        }

        # Thinned: a chart 900px wide gains nothing from 5,000 points, and each
        # row is its own transaction. Trades are written in full - they are the
        # history, and none may be dropped.
        stride = max(1, len(result.equity_curve) // 800)
        running_charges = _D("0")
        running_realised = _D("0")
        written = 0
        for index, (ts, point_equity, open_positions) in enumerate(result.equity_curve):
            running_charges += charged_by_ts.get(ts, _D("0"))
            running_realised += realised_by_ts.get(ts, _D("0"))
            if index % stride and index != len(result.equity_curve) - 1:
                continue
            store.record_equity(
                EquityRow(
                    ts=ts,
                    equity=point_equity,
                    cash=point_equity,
                    realised=running_realised,
                    unrealised=_D("0"),
                    charges=running_charges,
                    open_positions=open_positions,
                )
            )
            written += 1

        for number, trade in enumerate(result.trades):
            store.record_trade(
                f"replay-{number:05d}",
                trade.entry_ts,
                trade.exit_ts,
                {
                    "trade_id": f"replay-{number:05d}",
                    "strategy_id": strategy_name,
                    "signal_id": f"replay-sig-{number:05d}",
                    "opened_at": iso(trade.entry_ts),
                    "closed_at": iso(trade.exit_ts) if trade.exit_ts else "",
                    "legs": f"{instrument.key}:{trade.side.value}:{trade.lots}",
                    "gross_pnl": str(trade.gross_pnl),
                    "charges_total": str(
                        trade.spread_paid + trade.swap_paid + trade.commission_paid
                    ),
                    "net_pnl": str(trade.net_pnl),
                    # No stop distance is recorded per trade, so there is no
                    # honest R to report - blank, never a fabricated 0.
                    "r_multiple": "",
                    "exit_reason": trade.exit_reason.split(":")[0] if trade.exit_reason else "",
                    "reason": trade.exit_reason,
                },
            )

        store.replace_positions(
            []
            if result.trades and result.trades[-1].exit_ts is not None
            else [
                PositionRow(
                    instrument_key=instrument.key,
                    lots=result.trades[-1].lots,
                    qty=_D(result.trades[-1].lots),
                    average_price=result.trades[-1].entry_price,
                    mark=history[-1].close,
                    updated_at=history[-1].ts,
                )
            ]
            if result.trades
            else []
        )

        now = history[-1].ts
        for key, value in {
            "engine": "replay complete",
            "mode": "backtest",
            "broker": "paper",
            "venue": "MT5 / Vantage",
            "symbol": symbol,
            "currency": "USD",
            "strategy": f"{strategy_name} {timeframe.label}",
            # Commission on this venue *is* verified against real dealing
            # history (`CfdChargeModel.vantage_standard`, D-121), unlike the
            # MCX charge stack this flag was written for. Saying otherwise
            # printed a warning that was simply untrue for a CFD run.
            "costs_verified": "true",
            # True only when a tick-sampled profile actually priced the fills.
            # Without one the spread is a flat constant and the dashboard says
            # so; with one it is real bid/ask, and the coverage note below
            # keeps the claim proportionate to the evidence.
            "spread_measured": "true" if profile else "false",
            **(
                {"spread_note": profile.describe()}
                if profile
                else {}
            ),
            # `margin_calibrated` is deliberately not written. It reports on the
            # SPAN approximation the MCX options path uses; a CFD run has no
            # margin model at all (`margin_cap_pct=None`), so answering the
            # question either way would be inventing an opinion about machinery
            # this venue does not use.
            "kill_switch": "armed",
        }.items():
            store.set_health(key, value, at=now)

        store.record_note(
            now,
            f"Replay of {len(history)} real {symbol} {timeframe.label} bars through "
            f"{strategy_name}. This is history, not a live run - no order was placed.",
        )
    finally:
        store.close()

    net = result.net_pnl
    typer.echo(f"trades        {len(result.trades)} ({result.wins} winners)")
    typer.echo(f"net P&L       {net:,.2f} USD")
    typer.echo(f"  spread      {result.spread_paid:,.2f}")
    typer.echo(f"  swap        {result.swap_paid:,.2f}")
    typer.echo(f"equity points {written} written (thinned from {len(result.equity_curve)})")
    typer.echo(f"\nwritten to    {state}")
    typer.echo(f"serve it with algo serve --state {state}")
