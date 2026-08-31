"""``algo live-mt5`` and ``algo mt5-replay`` commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from algo.exchange.specs import ContractSpecStore

if TYPE_CHECKING:  # annotations only - the command bodies import lazily so
    # `algo live-mt5 --help` does not drag in MetaTrader5.
    from algo.live.alerts import Alerter
    from algo.live.loop import PassResult

app = typer.Typer()

#: Alert bodies are multi-line; named so the joins below read cleanly.
NEWLINE = "\n"


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
    equity: str = typer.Option(
        "",
        help=(
            "Starting equity for the engine's book. Default: 100000 on --broker "
            "paper, the account's real balance on --broker live"
        ),
    ),
    broker_kind: str = typer.Option(
        "paper",
        "--broker",
        help="paper | live. 'live' sends real orders to the logged-in account",
    ),
    allow_real_money: bool = typer.Option(
        False,
        "--allow-real-money",
        help="Permit --broker live on an account that is not a demo",
    ),
    ledger_path: Path = typer.Option(
        Path("state/mt5_ledger.json"),
        "--ledger",
        help="Where --broker live persists our order-id -> MT5 ticket mapping",
    ),
    passes: int = typer.Option(1, help="Poll this many times, then stop"),
    poll_interval_s: float = typer.Option(30.0, help="Seconds between polls"),
    state: Path = typer.Option(
        Path("state/dashboard.db"), help="State file the dashboard reads"
    ),
    journal_path: Path = typer.Option(
        Path("state/mt5_journal.db"), "--journal", help="Order journal"
    ),
    stop_file: Path = typer.Option(
        Path("state/STOP"),
        "--stop-file",
        help="Creating this file asks the loop to stop after the current bar",
    ),
) -> None:
    """Run a CFD strategy on live MT5 bars, against paper or the real account.

    `--broker paper` (the default) takes real bars from a running terminal and
    simulates every fill with the same `FillSimulator` the backtest uses. No
    order reaches the broker.

    `--broker live` sends market orders through `Mt5Broker` to whatever account
    the terminal is logged into. **It is refused on a non-demo account** unless
    `--allow-real-money` is also passed - see `_require_tradeable_account`. The
    engine's book still starts from the account's real balance so the two
    numbers on the dashboard are comparable rather than one being a fiction.

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
    from algo.execution.broker import Broker
    from algo.execution.fills import FillSimulator
    from algo.execution.mt5_broker import Mt5Broker
    from algo.execution.paper import PaperBroker
    from algo.live.alerts import build_alerter
    from algo.live.mt5_runner import XAUUSD_SPREAD_TICKS, build_mt5_paper_loop, strategy_for
    from algo.live.shutdown import StopFile, graceful_shutdown
    from algo.persistence.journal import OrderJournal
    from algo.persistence.state import StateStore

    live = broker_kind.strip().lower() == "live"
    if broker_kind.strip().lower() not in ("paper", "live"):
        raise typer.BadParameter(f"--broker is paper or live, not {broker_kind!r}")

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
        if live:
            _require_tradeable_account(account, allow_real_money=allow_real_money)
            typer.echo(
                f"broker        LIVE - orders go to account {account.login} "
                f"({_TRADE_MODE_NAMES.get(int(getattr(account, 'trade_mode', -1)), 'unknown')})"
            )
        else:
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
        # The book starts from what the account actually holds on a live run.
        # Charting a synthetic 100000 beside a real balance would put two
        # unrelated numbers on one screen and invite reading them as one.
        book_equity = equity or (str(account.balance) if live else "100000")

        broker: Broker
        if live:
            broker = Mt5Broker(terminal=mt5, symbol=symbol, clock=clock)
            # MT5 overwrites the order comment, so our client-order-ids live in
            # this file and nowhere else. Restoring it before the first
            # reconcile is what stops a restart from disowning its own open
            # orders - see algo/execution/mt5_broker.py's module docstring.
            broker.restore(ledger_path)
            typer.echo(f"ledger        {ledger_path}")
        else:
            broker = PaperBroker(
                simulator=FillSimulator(
                    spread=FixedTickSpread(XAUUSD_SPREAD_TICKS),
                    slippage=NoSlippage(),
                    charges=CfdChargeModel.vantage_standard(),
                ),
                specs=ContractSpecStore.default(),
                quote=lambda key: seed[-1].close if key == instrument.key else None,
                clock=clock,
                starting_cash=_D(book_equity),
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
                starting_equity=_D(book_equity),
                lots=lots,
                max_lots=lots,
                state=store,
                mode="live" if live else "paper",
                broker_label="mt5" if live else "paper",
            )
            typer.echo(
                f"strategy      {strategy_name} on {timeframe.label}, {lots} oz, "
                f"stop {stop_loss_pct}% / trail {trail_pct}% from {trail_activation_pct}%"
            )
            alerter = build_alerter()
            sentinel = StopFile(stop_file)
            if sentinel.clear():
                # Loud, not silent: "I asked it to stop and it started anyway"
                # is worth being told now rather than discovered later.
                typer.echo(f"stop file     cleared a stale {stop_file}")
            typer.echo(f"polling       {passes} pass(es), {poll_interval_s}s apart")
            typer.echo(f"alerts        {alerter.channels} channel(s)")
            typer.echo(
                f"stop          Ctrl-C, or:  algo stop --stop-file {stop_file}\n"
                "              either one finishes the current bar first\n"
            )

            # `mt5-replay` has always recorded these; the live loop never did,
            # so its runs rendered a dollar-settled book behind a rupee sign -
            # the exact confusion `money()` in the dashboard exists to prevent.
            # The currency comes from the account rather than a constant,
            # because the account is the thing that settles.
            for key, value in {
                "venue": f"MT5 / {account.server}",
                "symbol": symbol,
                "currency": str(account.currency),
                "strategy": f"{strategy_name} {timeframe.label}",
            }.items():
                store.set_health(key, value, at=clock.now())

            alerter.info(
                "live-mt5 started",
                f"{strategy_name} on {timeframe.label}, {lots} oz, "
                f"{'LIVE - real orders' if live else 'paper broker'}. "
                f"Account {account.login} ({account.server}).",
                at=clock.now(),
            )
            # Written before the first pass so the dashboard has the account the
            # moment the run starts, rather than after the first bar closes -
            # which on a 60m timeframe is up to an hour of a blank panel.
            if live:
                _record_account(store, broker, at=clock.now())

            def announce(count: int, name: str) -> None:
                typer.echo(
                    f"\n  {name}: finishing the current bar, then stopping. "
                    "Press again to force."
                    if count == 1
                    else f"\n  {name} again - forcing."
                )

            with graceful_shutdown(on_request=announce) as stopping:
                for result in run.loop.run(
                    max_passes=passes,
                    sleep=_time.sleep,
                    poll_interval_s=poll_interval_s,
                    # Either route asks the same question, and both are
                    # answered at a pass boundary rather than mid-pass.
                    should_stop=lambda: stopping.requested or sentinel.requested,
                ):
                    typer.echo(f"  {iso(result.ts)}  {result.summary()}")
                    _alert_on(alerter, result)
                    if live:
                        # After the pass, so an order placed during it is in the
                        # file before the next one can be. A crash between
                        # `order_send` and here still leaves the intent in the
                        # order journal, which is what reconcile reads first.
                        broker.save(ledger_path)
                        _record_account(store, broker, at=result.ts)

                # Recorded either way, so a restart can tell "asked to stop and
                # did" from "died mid-bar" - the two states a journal left in
                # SENT cannot distinguish on its own.
                if stopping.requested:
                    ended = f"live-mt5 stopped cleanly: {stopping.reason}"
                elif sentinel.requested:
                    ended = f"live-mt5 stopped cleanly: {sentinel.reason}"
                else:
                    ended = "live-mt5 finished its requested passes"
                # Cleared on the way out so it cannot stop the next run for a
                # reason the operator has already dealt with.
                sentinel.clear()
                if live:
                    broker.save(ledger_path)
                    _record_account(store, broker, at=clock.now())
                store.record_note(clock.now(), ended)
                store.set_health("engine", "stopped", at=clock.now())
                # A loop that stops is worth knowing about even when the reason
                # is dull: "it is no longer trading" is the fact, and nobody
                # should have to infer it from silence.
                alerter.warning(
                    "live-mt5 stopped",
                    f"{ended}. {len(run.broker.positions())} position(s) open.",
                    at=clock.now(),
                )
                typer.echo(f"\n{ended}")
            store.close()
    finally:
        mt5.shutdown()


#: MT5's `account_info().trade_mode`, for the echo. The gate below reads the
#: broker's own `is_demo` rather than this map - one place decides what counts
#: as play money, and it is not the CLI.
_TRADE_MODE_NAMES = {0: "demo", 1: "contest", 2: "real"}


def _require_tradeable_account(account: object, *, allow_real_money: bool) -> None:
    """Refuse `--broker live` on a real-money account unless told otherwise.

    The check is deliberately one-directional: anything not positively
    identified as demo or contest is treated as real. An unrecognised
    `trade_mode` from a future terminal build should stop the run, not be
    waved through because it did not match the string we expected.

    `trade_allowed` is checked here too rather than only in `Mt5Broker.connect`,
    so "Algo Trading is switched off in the terminal" is reported before the
    run prints a strategy line and looks like it is about to work.
    """
    mode = int(getattr(account, "trade_mode", -1))
    if mode not in (0, 1) and not allow_real_money:
        raise typer.BadParameter(
            f"--broker live refused: account {getattr(account, 'login', '?')} is "
            f"{_TRADE_MODE_NAMES.get(mode, f'trade_mode {mode}')}, not a demo. "
            "This sends market orders with real money. If that is genuinely the "
            "intent, pass --allow-real-money as well."
        )
    if not getattr(account, "trade_allowed", False):
        raise typer.BadParameter(
            "the terminal reports trading is not allowed on this account. "
            "Switch on Algo Trading in the MT5 terminal, then start again."
        )


def _record_account(store: object, broker: object, *, at: datetime) -> None:
    """Snapshot the broker's account into the state file for the dashboard.

    Never raises. `alerts.py` states the rule this follows: a panel that cannot
    be drawn is not a reason to stop trading, and `account_info` returning
    nothing on one poll is a blip, not a halt. The stale snapshot stays, which
    is why the row carries `updated_at` - a number that has stopped moving is
    visible as one.
    """
    from algo.persistence.state import AccountRow

    try:
        snapshot = broker.account()  # type: ignore[attr-defined]
        store.record_account(  # type: ignore[attr-defined]
            AccountRow(
                login=snapshot.login,
                server=snapshot.server,
                currency=snapshot.currency,
                trade_mode=snapshot.trade_mode,
                leverage=snapshot.leverage,
                balance=snapshot.balance,
                equity=snapshot.equity,
                margin_used=snapshot.margin_used,
                margin_free=snapshot.margin_free,
                margin_level=snapshot.margin_level,
                floating_pnl=snapshot.floating_pnl,
                open_tickets=snapshot.open_tickets,
                updated_at=at,
            )
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        typer.echo(f"  ! account snapshot skipped: {exc}")


def _alert_on(alerter: Alerter, result: PassResult) -> None:
    """Alert on what a person would want woken for, and nothing else.

    A message per poll would be noise nobody reads, and an alerter people mute
    is an alerter that is not there. So: routed orders and router refusals only.
    A quiet pass - the overwhelming majority of them - says nothing.
    """
    if not result.routed:
        return
    from algo.execution.router import Outcome

    placed = [r for r in result.routed if r.outcome is Outcome.PLACED]
    refused = [r for r in result.routed if r.outcome is not Outcome.PLACED]

    if placed:
        alerter.info(
            f"{len(placed)} order(s) placed",
            NEWLINE.join(f"{r.client_order_id} -> {r.outcome.value}" for r in placed),
            at=result.ts,
        )
    if refused:
        # A refusal is the reconcile-before-send rule working as designed, but
        # it also means intent and reality may now differ - which is exactly
        # what deserves a person rather than a log line.
        alerter.warning(
            f"{len(refused)} order(s) not placed",
            NEWLINE.join(
                f"{r.client_order_id} -> {r.outcome.value}: {r.detail}" for r in refused
            ),
            at=result.ts,
        )


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
