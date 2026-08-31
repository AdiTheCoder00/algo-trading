"""``algo live`` — the Milestone 7 wiring drill and paper-trading loop."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from algo.cli._helpers import strangle_from_config
from algo.config.loader import load_config
from algo.config.modes import LIVE_FLAG, resolve_mode
from algo.core.logging import configure_logging

if TYPE_CHECKING:  # annotations only - the bodies import these lazily so
    # `algo live --help` does not drag in a broker SDK.
    from algo.config.schema import AppConfig
    from algo.core.clock import SystemClock
    from algo.data.smartapi_feed import SmartConnectTransport
    from algo.exchange.master import InstrumentMaster

app = typer.Typer()


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
    passes: int = typer.Option(
        0,
        "--passes",
        help="Run the PAPER trading loop for this many passes (0 = wiring drill "
        "only, place nothing). Bounded on purpose: a trading loop with no "
        "stopping condition keeps trading after you have gone home.",
    ),
    poll_interval_s: float = typer.Option(
        30.0, "--poll", help="Seconds between passes when --passes is set"
    ),
    wait_for_bar_min: float = typer.Option(
        45.0,
        "--wait-for-bar",
        help="Minutes to wait for the first closed bar before giving up. A "
        "session started at the open has none until the first bar closes.",
    ),
    state: Path | None = typer.Option(
        None,
        "--state",
        help="Dashboard state file. Also where the strategy's traded-cycle "
        "cadence is persisted and restored from across a restart",
    ),
) -> None:
    """Connect to Kotak Neo (live broker) + Angel SmartAPI (candles), and report.

    The Milestone 7 wiring drill: it proves the live path end to end -
    both credential sets, both instrument masters, both sessions, order ledger,
    reconciliation - without placing an order. A live session never runs
    without --i-understand-this-is-real-money, whatever the config says.
    """
    from datetime import timedelta

    from algo.core.clock import SystemClock
    from algo.core.enums import Mode
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
    configure_logging(
        level=config.logging.level,
        json_format=config.logging.json_format,
        file=config.logging.file,
    )
    mode = resolve_mode(config.mode, real_money_flag=real_money_flag)

    # Checked here, before any credential is read or any session opened: nothing
    # in this path has yet placed an order against a real account, so refusing
    # after connecting to one would be refusing in the wrong place (D-109).
    if passes and mode is not Mode.BACKTEST and mode is not Mode.PAPER:
        typer.echo(
            f"  ! --passes runs the PAPER loop only; this config says mode: {mode}. "
            "Routing the loop to a real account is not wired yet. Refusing."
        )
        raise typer.Exit(code=1)

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

    if passes:
        _run_paper_loop(
            config=config,
            master=master,
            live_master=live_master,
            market_data_key=market_data_key,
            transport=smart_transport,
            clock=clock,
            passes=passes,
            poll_interval_s=poll_interval_s,
            wait_for_bar_min=wait_for_bar_min,
            state=state,
        )
    else:
        _bars_from_candles(smart_transport, master, config, clock)

    broker.disconnect()
    typer.echo("  session ended.")


def _run_paper_loop(
    *,
    config: AppConfig,
    master: InstrumentMaster,
    live_master: InstrumentMaster,
    market_data_key: str,
    transport: SmartConnectTransport,
    clock: SystemClock,
    passes: int,
    poll_interval_s: float,
    wait_for_bar_min: float,
    state: Path | None,
) -> None:
    """Run `LiveLoop` against the **paper** broker.

    Paper only, and the refusal below is not a placeholder to be relaxed casually:
    nothing in this path has yet placed an order against a real account, so the
    first thing it does must not be one. The paper broker uses the same
    `FillSimulator` as the backtest and keeps its own separately-persisted books,
    which is what makes a crash-recovery drill mean anything.
    """
    import time as _time
    from datetime import timedelta

    from algo.backtest.engine import BacktestEngine
    from algo.backtest.prices import BarPriceSource, CompositePriceSource
    from algo.core.bar import Bar, Timeframe
    from algo.core.enums import Mode
    from algo.core.errors import AlgoError, DomainError
    from algo.core.instrument import FutureId
    from algo.core.timeutil import iso as _iso
    from algo.core.timeutil import ist_date
    from algo.costs.charges import McxChargeModel
    from algo.costs.margin import SpanApproxMargin
    from algo.costs.slippage import TickSlippage
    from algo.costs.spread import FixedTickSpread
    from algo.data.kotak_feed import KotakChainFeed, NeoQuotesTransport
    from algo.data.live import SessionWindow
    from algo.data.smartapi_feed import SmartApiBarFeed
    from algo.exchange.calendar import mcx_calendar
    from algo.exchange.expiries import expiries_from_master
    from algo.exchange.specs import ContractSpecStore
    from algo.execution.fills import FillSimulator
    from algo.execution.paper import PaperBroker
    from algo.execution.router import OrderRouter
    from algo.live.chain import LiveChainProvider
    from algo.live.feeds import BrokerFillFeed, IterableBarFeed
    from algo.live.loop import LiveLoop
    from algo.persistence.journal import OrderJournal
    from algo.persistence.state import StateStore
    from algo.portfolio.book import Portfolio
    from algo.risk.devolvement import DevolvementGuard
    from algo.risk.engine import FixedLotSizer, RiskEngine
    from algo.risk.killswitch import KillSwitch

    # `live` refuses a non-paper mode before opening any session; this is the
    # belt to that braces, so the function cannot be called wrongly from
    # somewhere else later.
    if config.mode is Mode.LIVE:
        raise DomainError("_run_paper_loop must never be reached in live mode")

    underlying = config.instruments[0].underlying
    exchange = config.instruments[0].exchange
    futures = master.future_rows(underlying, exchange)
    if not futures or futures[-1].expiry is None:
        typer.echo("  paper loop: no futures contract in the master snapshot")
        return
    instrument = FutureId(
        underlying=underlying, expiry=futures[-1].expiry, exchange=exchange
    )

    calendar = mcx_calendar(
        holidays_file=config.market.holidays_file,
        allow_unverified=config.market.allow_unverified_calendar,
    )
    timeframe = Timeframe(minutes=config.market.bar.timeframe_minutes)
    bar_source = SmartApiBarFeed(
        transport=transport,
        master=master,
        instrument=instrument,
        timeframe=timeframe,
        clock=clock,
        session=SessionWindow(calendar),
    )
    def read_bars() -> list[Bar]:
        """Closed bars so far, treating "none yet" as empty rather than fatal."""
        try:
            return list(bar_source)
        except Exception as exc:  # noqa: BLE001 - a feed hiccup must not end the session
            typer.echo(f"    bar feed not ready ({exc})")
            return []

    # Wait for the first closed bar rather than giving up on it.
    #
    # A session started at 09:00 has no closed 30-minute bar until 09:30, so
    # exiting on an empty first read would abandon the whole day - which is
    # exactly what the first scheduled run would have done. Bounded, because a
    # feed that never produces a bar is a real failure and must not be waited on
    # forever.
    seed = read_bars()
    if not seed:
        deadline = clock.now() + timedelta(minutes=wait_for_bar_min)
        typer.echo(
            f"  paper loop: no closed bar yet; waiting up to {wait_for_bar_min}m "
            "for the first one"
        )
        while not seed and clock.now() < deadline:
            _time.sleep(poll_interval_s)
            seed = read_bars()
    if not seed:
        typer.echo(
            f"  paper loop: no closed bar within {wait_for_bar_min}m - the session "
            "may not have opened, or the candle feed is down. Nothing to decide on."
        )
        return
    typer.echo(f"  paper loop: first closed bar at {_iso(seed[-1].ts)}")

    # The option chain, polled and greeked. Without it the strategy selects on a
    # delta that is None on every row and silently never trades (D-112).
    chain_provider = LiveChainProvider(
        feed=KotakChainFeed(
            transport=NeoQuotesTransport(market_data_key),
            master=live_master,
            underlying=underlying,
            clock=clock,
            session=SessionWindow(calendar),
            poll_interval_s=0.0,
        ),
        max_staleness_s=float(config.data.quality.max_stale_seconds),
    )
    expiries = expiries_from_master(live_master, underlying, exchange)
    try:
        cycle = expiries.nearest_expiry_on_or_after(underlying, ist_date(clock.now()))
    except AlgoError as exc:
        typer.echo(f"  paper loop: cannot resolve an option expiry ({exc})")
        return
    typer.echo(f"  paper loop: trading the {cycle.option_expiry} cycle")

    simulator = FillSimulator(
        spread=FixedTickSpread(2),
        slippage=TickSlippage(market_ticks=0, stop_ticks=2),
        charges=McxChargeModel.default(),
    )
    store = StateStore(state) if state is not None else None
    strategy = strangle_from_config(config, underlying)
    engine = BacktestEngine(
        bars=seed[:1],
        calendar=calendar,
        specs=ContractSpecStore.default(),
        strategy=strategy,
        risk=RiskEngine(
            sizer=FixedLotSizer(config.risk.sizing.fixed_lots),
            spec_for=None,
            max_concurrent_positions=2,
            max_lots_per_underlying=config.risk.caps.max_lots_per_underlying,
            margin_cap_pct=config.risk.caps.max_total_margin_pct,
        ),
        simulator=simulator,
        portfolio=Portfolio(config.risk.starting_equity),
        instrument=instrument,
        timeframe=timeframe,
        is_option=True,
        # Futures bars price the underlying; the live chain prices every option
        # leg. Composed rather than chosen, because a strangle run holds both.
        price_source=CompositePriceSource(
            chain_provider, BarPriceSource(instrument, seed[:1])
        ),
        chain_provider=chain_provider,
        expiries=expiries,
        devolvement=DevolvementGuard(
            calendar=calendar,
            force_exit_sessions_before_expiry=(
                config.risk.devolvement.force_exit_sessions_before_expiry
            ),
            block_new_entries_within_dte=(
                config.risk.devolvement.block_new_entries_within_dte
            ),
        ),
        kill_switch=KillSwitch(
            daily_loss_limit_pct=config.risk.kill_switch.daily_loss_limit_pct,
            max_consecutive_losses=config.risk.kill_switch.max_consecutive_losses,
            max_drawdown_pct=config.risk.kill_switch.max_drawdown_pct,
        ),
        # `backtest_bhavcopy`/`backtest_smartapi` wire all three of these from
        # config; this path silently fell back to BacktestEngine's own
        # defaults (no flatten, no stop-viability guard) instead - the exact
        # "same edit missed at one of several call sites" bug class D-117
        # already names, recurring at the one command that runs live/paper.
        flatten_on_trip=config.risk.kill_switch.flatten_on_trip,
        margin=SpanApproxMargin(),
        state=store,
        mode="paper",
        broker="paper",
        stop_viability_threshold=config.strategy.exit.min_stop_to_cost_ratio,
        on_stop_viability_breach=config.strategy.exit.on_stop_viability_breach,
    )
    if engine.restore_strategy_state():
        typer.echo("  paper loop: restored the strategy's traded-cycle cadence")

    broker = PaperBroker(
        clock=clock,
        specs=ContractSpecStore.default(),
        simulator=simulator,
        quote=lambda key: engine.mark_for(key),
    )
    broker.connect()

    with OrderJournal(config.persistence.live_db) as journal:
        router = OrderRouter(broker=broker, journal=journal, clock=clock)
        router.reconcile()
        loop = LiveLoop(
            engine=engine,
            bars=IterableBarFeed(lambda: list(bar_source)),
            fills=BrokerFillFeed(
                broker=broker,
                clock=clock,
                instruments={instrument.key: instrument},
            ),
            place=router.place_all,
            clock=clock,
            state=store,
            # One poll per bar, before anything asks the chain a question.
            chain=lambda _bar: chain_provider.refresh(cycle.option_expiry),
        )
        typer.echo(f"  paper loop: {passes} pass(es), {poll_interval_s}s apart")
        results = loop.run(
            max_passes=passes,
            on_pass=lambda r: typer.echo(f"    {_iso(r.ts)}  {r.summary()}"),
            sleep=_time.sleep,
            poll_interval_s=poll_interval_s,
        )
    acted = sum(1 for r in results if r.acted)
    typer.echo(f"  paper loop: {len(results)} pass(es), {acted} that did something")
    broker.disconnect()
    if store is not None:
        store.close()


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
