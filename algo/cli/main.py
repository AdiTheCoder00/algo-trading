"""Command line entry points.

Three commands: `verify` proves the data pipeline end to end on synthetic bars,
`config` shows exactly what settings a run would use, and `backtest` runs the
Milestone 3 falsification.

`backtest` deliberately does not accept a real dataset yet. There is no recorded
data to point it at, and a command that quietly ran on generated bars while
looking like a strategy result would be worse than one that says what it is.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from dotenv import load_dotenv

from algo.config.loader import config_hash, load_config
from algo.config.modes import LIVE_FLAG, resolve_mode
from algo.core.bar import Timeframe
from algo.data.resample import expected_bar_count, resample
from algo.data.synthetic import one_minute_session
from algo.data.validate import validate_bars
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.specs import ContractSpecStore

if TYPE_CHECKING:
    from algo.config.schema import AppConfig
    from algo.core.clock import SystemClock
    from algo.data.smartapi_feed import SmartConnectTransport
    from algo.exchange.master import InstrumentMaster

app = typer.Typer(add_completion=False, help="MCX GOLDM short-strangle engine")

#: Credentials live in .env (gitignored). Loading it here means the whole CLI
#: sees the same environment without anyone hardcoding a secret.
load_dotenv()

#: One day inside US daylight saving and one outside, so both session-length
#: regimes are exercised every time `verify` runs.
US_DST_DAY = date(2026, 8, 19)
STANDARD_DAY = date(2026, 11, 10)


@app.command()
def verify(timeframe_minutes: int = 30) -> None:
    """Run the data pipeline on synthetic bars and report what came out.

    This is the Milestone 1 proof: session boundaries, resampling, the partial-bar
    stub and the quality gates, exercised in both DST regimes.
    """
    calendar = synthetic_calendar()
    tf = Timeframe(minutes=timeframe_minutes)

    typer.echo(f"Synthetic pipeline check, {tf.label} bars\n")
    for label, day in (("US DST     ", US_DST_DAY), ("standard   ", STANDARD_DAY)):
        minute_bars = one_minute_session(calendar, day, seed=20260819)
        bars = resample(minute_bars, calendar=calendar, timeframe=tf)
        report = validate_bars(bars, calendar=calendar, timeframe=tf)
        partial = sum(1 for b in bars if b.is_partial)
        typer.echo(
            f"  {label} {day}  session {calendar.session_minutes(day)} min"
            f"  ->  {len(bars)} bars"
            f" (expected {expected_bar_count(calendar, day, tf)}, {partial} partial)"
        )
        typer.echo(f"               {report.summary()}")

    typer.echo("\nContract specifications in force:")
    store = ContractSpecStore.default()
    for underlying, exchange in store.underlyings():
        spec = store.spec_for(underlying, exchange, US_DST_DAY)
        typer.echo(
            f"  {underlying} on {exchange}: lot {spec.lot_size}, tick {spec.tick_size}, "
            f"multiplier {spec.multiplier}, strikes every {spec.strike_interval}"
        )
        typer.echo(f"    source: {spec.source.strip()}")


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
    typer.echo(
        f"exits         TP {config.strategy.exit.take_profit_value}% / "
        f"SL {config.strategy.exit.stop_loss_value}% of "
        f"{config.strategy.exit.stop_loss_kind.removeprefix('PCT_OF_').lower()}"
    )
    typer.echo(f"entry         {config.strategy.entry_bars_ist[0]} IST, {config.strategy.cadence}")
    typer.echo(f"equity        {config.risk.starting_equity}")


@app.command()
def backtest(
    strategy: str = typer.Option("coin_flip", help="coin_flip | buy_and_hold"),
    spread_ticks: int = typer.Option(2, help="Modelled spread, in ticks"),
    seed: int = typer.Option(20260819),
    tearsheet: Path | None = typer.Option(None, help="Write an HTML tearsheet here"),
    trade_log: Path | None = typer.Option(None, help="Write the trade log CSV here"),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="A config file whose sizing, caps, kill switch and equity this run uses",
    ),
    state: Path | None = typer.Option(
        None,
        "--state",
        help="Stream equity, positions, signals, notes and health to this dashboard "
        "state file, and act on kill-switch requests each bar",
    ),
) -> None:
    """Run the Milestone 3 falsification on synthetic bars.

    This is not a strategy result and does not pretend to be one. It runs the two
    reference strategies over generated data to show that the engine's cost
    arithmetic behaves - buy-and-hold tracking the instrument, and a coin flip on
    a flat market losing exactly its costs and nothing else.

    `--config` makes the risk settings real: sizing lots, caps, kill-switch
    limits, margin cap and starting equity come from the file instead of the
    defaults. `--state` feeds the monitoring dashboard; halt requests recorded
    there trip the kill switch on the next bar.
    """
    from decimal import Decimal

    from algo.backtest.engine import BacktestEngine
    from algo.core.instrument import FutureId
    from algo.costs.charges import McxChargeModel
    from algo.costs.margin import SpanApproxMargin
    from algo.costs.slippage import TickSlippage
    from algo.costs.spread import FixedTickSpread
    from algo.data.resample import resample
    from algo.data.synthetic import flat_session, one_minute_session
    from algo.execution.fills import FillSimulator
    from algo.persistence.state import StateStore
    from algo.portfolio.book import Portfolio
    from algo.reporting import metrics as metrics_mod
    from algo.risk.engine import FixedLotSizer, RiskEngine
    from algo.risk.killswitch import KillSwitch
    from algo.strategy.buy_and_hold import BuyAndHold
    from algo.strategy.coin_flip import CoinFlip

    calendar = synthetic_calendar()
    tf = Timeframe(minutes=30)
    instrument = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))

    flat = strategy == "coin_flip"
    source = (
        flat_session(calendar, US_DST_DAY, price=Decimal("156640.00"))
        if flat
        else one_minute_session(calendar, US_DST_DAY, seed=seed)
    )
    bars = resample(source, calendar=calendar, timeframe=tf)

    if config is not None:
        from algo.config.loader import load_config as resolve

        cfg = resolve(config)
        starting_equity = cfg.risk.starting_equity
        lots = cfg.risk.sizing.fixed_lots
        max_concurrent = cfg.risk.caps.max_concurrent_positions
        max_lots = cfg.risk.caps.max_lots_per_underlying
        margin_cap_pct = cfg.risk.caps.max_total_margin_pct
        kill_switch = KillSwitch(
            daily_loss_limit_pct=cfg.risk.kill_switch.daily_loss_limit_pct,
            max_consecutive_losses=cfg.risk.kill_switch.max_consecutive_losses,
            max_drawdown_pct=cfg.risk.kill_switch.max_drawdown_pct,
        )
        margin = SpanApproxMargin()
        stop_viability = cfg.strategy.exit.min_stop_to_cost_ratio
        on_breach = cfg.strategy.exit.on_stop_viability_breach
        mode = cfg.mode.value
        typer.echo(f"config        {config} (mode {mode}, {lots} lot(s), equity {starting_equity})")
    else:
        starting_equity = Decimal("1000000.00")
        lots = 1
        max_concurrent = 1
        max_lots = 5
        margin_cap_pct = None
        kill_switch = None
        margin = None
        stop_viability = None
        on_breach = "warn"
        mode = "backtest"

    store: StateStore | None = None
    if state is not None:
        store = StateStore(state)
        kill_switch = kill_switch or KillSwitch(
            daily_loss_limit_pct=Decimal("2"),
            max_consecutive_losses=3,
            max_drawdown_pct=Decimal("10"),
        )

    try:
        engine = BacktestEngine(
            bars=bars,
            calendar=calendar,
            specs=ContractSpecStore.default(),
            strategy=(
                CoinFlip(instrument, seed=seed)
                if strategy == "coin_flip"
                else BuyAndHold(instrument)
            ),
            risk=RiskEngine(
                sizer=FixedLotSizer(lots),
                spec_for=None,
                max_concurrent_positions=max_concurrent,
                max_lots_per_underlying=max_lots,
                margin_cap_pct=margin_cap_pct,
            ),
            simulator=FillSimulator(
                spread=FixedTickSpread(spread_ticks),
                slippage=TickSlippage(market_ticks=0, stop_ticks=2),
                charges=McxChargeModel.default(),
            ),
            portfolio=Portfolio(starting_equity),
            instrument=instrument,
            timeframe=tf,
            is_option=False,
            kill_switch=kill_switch,
            margin=margin,
            state=store,
            mode=mode,
            broker="backtest",
            stop_viability_threshold=stop_viability,
            on_stop_viability_breach=on_breach,
        )
        result = engine.run()
    finally:
        if store is not None:
            store.close()

    typer.echo(f"strategy      {strategy}")
    typer.echo(f"market        {'flat (cost isolation)' if flat else 'seeded random walk'}")
    typer.echo(f"bars          {len(bars)}   dataset {result.dataset_hash}")
    typer.echo("")
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
    if flat:
        typer.echo("")
        typer.echo(
            f"  FALSIFICATION: gross P&L on a flat market = {result.gross_pnl} "
            f"(must be exactly 0)"
        )
    for warning in result.warnings:
        typer.echo(f"  ! {warning}")

    if trade_log is not None:
        from algo.reporting.export import write_trade_log

        typer.echo(f"  trade log  -> {write_trade_log(result.trades, trade_log)}")
    if tearsheet is not None:
        from algo.reporting.tearsheet import render, write

        markup = render(
            title=f"GOLDM engine - {strategy}",
            metrics=summary,
            curve=result.equity_curve,
            trades=result.trades,
            warnings=result.warnings,
            dataset_hash=result.dataset_hash,
            config_hash=result.config_hash,
        )
        typer.echo(f"  tearsheet  -> {write(tearsheet, markup)}")


@app.command()
def live(
    path: Path = typer.Argument(Path("config/goldm.yaml"), help="Config file to resolve"),
    refresh_master: bool = typer.Option(
        False, "--refresh-master", help="Re-download the instrument master before starting"
    ),
    expiry: str | None = typer.Option(
        None, help="Option expiry to quote (YYYY-MM-DD); default: nearest listed"
    ),
    real_money_flag: bool = typer.Option(
        False, LIVE_FLAG, help="Acknowledge this is a real account"
    ),
) -> None:
    """Connect to Kotak Neo (live broker) + Angel SmartAPI (candles), and report.

    The Milestone 7 wiring drill: it proves the live path end to end -
    both credential sets, both instrument masters, both sessions, order ledger,
    reconciliation - without placing an order. A live session never runs
    without --i-understand-this-is-real-money, whatever the config says.
    """
    from algo.core.clock import SystemClock
    from algo.data.smartapi_feed import (
        SmartConnectTransport,
    )
    from algo.data.smartapi_feed import (
        credentials_from_env as smart_credentials_from_env,
    )
    from algo.exchange.master import (
        HttpMasterSource,
        InstrumentMaster,
        KotakMasterSource,
        fetch_master,
    )
    from algo.execution.kotak import (
        KotakBroker,
        NeoTransport,
        default_totp,
    )
    from algo.execution.kotak import (
        credentials_from_env as kotak_credentials_from_env,
    )
    from algo.execution.reconcile import Reconciler
    from algo.persistence.journal import OrderJournal

    config = load_config(path)
    resolve_mode(config.mode, real_money_flag=real_money_flag)

    clock = SystemClock()

    smart_credentials = smart_credentials_from_env()
    if not smart_credentials.has_all():
        typer.echo("  ! ALGO_SMARTAPI_* credentials are missing (see .env.example)")
        raise typer.Exit(code=1)
    kotak_credentials = kotak_credentials_from_env()
    if not kotak_credentials.has_all():
        typer.echo("  ! ALGO_KOTAK_* credentials are missing (see .env.example)")
        raise typer.Exit(code=1)

    snapshot = config.data.master_snapshot
    if refresh_master or not snapshot.exists():
        typer.echo(f"  fetching instrument master -> {snapshot}")
        fetch_master(HttpMasterSource(), snapshot, now=clock.now())
    master = InstrumentMaster.from_snapshot(snapshot)
    underlying = config.instruments[0].underlying
    exchange = config.instruments[0].exchange
    n_expiries = len(master.option_expiries(underlying, exchange))
    typer.echo(f"  master snapshot (bars): {n_expiries} listed option expiries for {underlying}")

    live_snapshot = config.data.live_master_snapshot
    market_data_key = kotak_credentials.market_data_key or kotak_credentials.consumer_key
    if refresh_master or not live_snapshot.exists():
        typer.echo(f"  fetching Kotak live master -> {live_snapshot}")
        fetch_master(
            KotakMasterSource(consumer_key=market_data_key),
            live_snapshot,
            now=clock.now(),
        )
    live_master = InstrumentMaster.from_snapshot(live_snapshot)
    n_live_expiries = len(live_master.option_expiries(underlying, exchange))
    typer.echo(
        f"  live master snapshot: {n_live_expiries} listed option expiries for {underlying}"
    )

    smart_transport = SmartConnectTransport(smart_credentials.api_key)
    smart_transport.connect(
        smart_credentials.client_id,
        smart_credentials.password,
        default_totp(smart_credentials.totp_seed)(),
    )
    typer.echo(f"  smartapi session for {smart_credentials.client_id} (candles)")

    broker = KotakBroker(
        transport=NeoTransport(kotak_credentials.consumer_key),
        master=live_master,
        credentials=kotak_credentials,
        clock=clock,
    )
    broker.restore(config.persistence.live_broker_state)
    broker.connect()

    with OrderJournal(config.persistence.live_db) as journal:
        report = Reconciler(broker, journal).reconcile(
            now=clock.now(), since=clock.now() - timedelta(days=1)
        )
        broker.save(config.persistence.live_broker_state)

    typer.echo(f"  session         {broker.health().detail}")
    typer.echo(f"  adapter         {broker!r}")
    typer.echo("")
    typer.echo(report.summary())

    if expiry is not None:
        _quote_chain(market_data_key, live_master, config, expiry, clock)

    _bars_from_candles(smart_transport, master, config, clock)

    broker.disconnect()
    typer.echo("  session ended.")


def _quote_chain(
    consumer_key: str,
    master: InstrumentMaster,
    config: AppConfig,
    expiry: str,
    clock: SystemClock,
) -> None:
    """One chain snapshot for the given expiry, printed as a table."""
    from datetime import date as _date

    from algo.data.kotak_feed import KotakChainFeed, NeoQuotesTransport
    from algo.data.live import SessionWindow
    from algo.exchange.calendar import synthetic_calendar

    feed = KotakChainFeed(
        transport=NeoQuotesTransport(consumer_key),
        master=master,
        underlying=config.instruments[0].underlying,
        clock=clock,
        session=SessionWindow(synthetic_calendar()),
        poll_interval_s=0.0,
    )
    option_expiry = _date.fromisoformat(expiry)
    try:
        snapshot = next(iter(feed.snapshots(option_expiry)))
    except StopIteration:
        typer.echo("  no chain snapshot: outside session hours")
        return
    typer.echo(
        f"\nchain {snapshot.underlying} {snapshot.option_expiry} @ "
        f"{snapshot.futures_price} (futures)"
    )
    for row in snapshot.rows:
        quote = row.quote
        bid = f"{quote.bid}" if quote.bid is not None else "-"
        ask = f"{quote.ask}" if quote.ask is not None else "-"
        ltp = f"{quote.ltp}" if quote.ltp is not None else "-"
        typer.echo(f"  {row.strike:>8} {row.right:<3} bid {bid:>8}  ask {ask:>8}  ltp {ltp:>8}")


def _bars_from_candles(
    transport: SmartConnectTransport,
    master: InstrumentMaster,
    config: AppConfig,
    clock: SystemClock,
) -> None:
    """One read of today's closed bars, as the live loop would get them."""
    from algo.core.bar import Timeframe
    from algo.core.instrument import FutureId
    from algo.data.live import SessionWindow
    from algo.data.smartapi_feed import SmartApiBarFeed
    from algo.exchange.calendar import synthetic_calendar

    underlying = config.instruments[0].underlying
    exchange = config.instruments[0].exchange
    futures = master.future_rows(underlying, exchange)
    if not futures:
        typer.echo("  candle proof: no futures contract in the master snapshot")
        return
    row = futures[-1]
    if row.expiry is None:
        typer.echo("  candle proof: futures contract has no expiry; skipping")
        return
    feed = SmartApiBarFeed(
        transport=transport,
        master=master,
        instrument=FutureId(underlying=underlying, expiry=row.expiry, exchange=exchange),
        timeframe=Timeframe(minutes=config.market.bar.timeframe_minutes),
        clock=clock,
        session=SessionWindow(synthetic_calendar()),
    )
    try:
        bars = list(feed)
    except Exception as exc:  # noqa: BLE001 - a drill must degrade, not crash
        typer.echo(f"  candle proof: no bars yet ({exc})")
        return
    typer.echo(
        f"  candle proof: {len(bars)} closed bar(s) today for {row.tradingsymbol}"
    )


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


@app.command("credentials")
def credentials() -> None:
    """Report which broker credentials are loaded - without printing any of them.

    Brief section 2.7. This shows presence and length only, which is enough to diagnose a
    truncated key or a stray newline and reveals nothing useful to anyone else. A
    trading system's output gets pasted into chat windows and issue trackers, so
    the safest secret is one that was never rendered.
    """
    from algo.api.app import TOKEN_ENV
    from algo.data.smartapi_feed import (
        credentials_from_env as smart_credentials_from_env,
    )
    from algo.execution.kotak import credentials_from_env as kotak_credentials_from_env

    def status(prefix: str, fields: list[str]) -> None:
        import os

        for name in fields:
            raw = os.environ.get(f"{prefix}_{name}", "")
            if raw:
                mark = "set"
                detail = f"{len(raw)} chars"
                if raw != raw.strip():
                    detail += "  ! has leading/trailing whitespace"
            else:
                mark, detail = "MISSING", ""
            typer.echo(f"  {prefix}_{name:<28} {mark:<8} {detail}")

    smart = smart_credentials_from_env()
    typer.echo("SmartAPI (historical bars - from .env or the environment):")
    status(
        "ALGO_SMARTAPI",
        ["API_KEY", "CLIENT_ID", "PASSWORD", "TOTP_SEED"],
    )
    typer.echo("")
    if smart.has_all():
        typer.echo("  all four SmartAPI credentials are present.")
    else:
        typer.echo("  MISSING: " + ", ".join(smart.missing()))

    typer.echo("")
    kotak = kotak_credentials_from_env()
    typer.echo("Kotak Neo (live broker - from .env or the environment):")
    status(
        "ALGO_KOTAK",
        ["CONSUMER_KEY", "MOBILE_NUMBER", "UCC", "TOTP_SEED", "MPIN", "MARKET_DATA_KEY"],
    )
    typer.echo("")
    if kotak.has_all():
        typer.echo("  all five Kotak credentials are present.")
    else:
        typer.echo("  MISSING: " + ", ".join(kotak.missing()))
        typer.echo("  Copy .env.example to .env and fill it in. An API key alone")
        typer.echo("  authenticates nothing - login needs the client code, the MPIN")
        typer.echo("  and the TOTP secret as well.")

    typer.echo("")
    token = os.environ.get(TOKEN_ENV, "")
    if token:
        typer.echo(f"  {TOKEN_ENV:<26} set      {len(token)} chars")
    else:
        typer.echo(f"  {TOKEN_ENV:<26} MISSING  (the monitoring API will not start)")


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


if __name__ == "__main__":
    app()
