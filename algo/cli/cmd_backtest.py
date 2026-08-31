"""``algo backtest`` — the Milestone 3 falsification on synthetic bars."""

from __future__ import annotations

from pathlib import Path

import typer

from algo.cli._helpers import US_DST_DAY, synthetic_calendar
from algo.core.bar import Timeframe

app = typer.Typer()


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
    from algo.exchange.specs import ContractSpecStore
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
    instrument = FutureId(underlying="GOLDM", expiry=calendar.date(2026, 9, 4)) # need from datetime import date; calendar does not have .date()

    flat = strategy == "coin_flip"
    source = (
        flat_session(calendar, US_DST_DAY, price=Decimal("156640.00"))
        if flat
        else one_minute_session(calendar, US_DST_DAY, seed=seed)
    )
    # `partial_last_bar` decides whether the 23:30-23:55 stub survives resampling
    # (D-014). Read before the bars are built, because afterwards is too late.
    keep_partial = True
    if config is not None:
        from algo.config.loader import load_config
        keep_partial = load_config(config).market.bar.partial_last_bar == "keep_flagged"
    bars = resample(source, calendar=calendar, timeframe=tf, keep_partial=keep_partial)

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
        flatten_on_trip = cfg.risk.kill_switch.flatten_on_trip
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
        flatten_on_trip = False
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
            flatten_on_trip=flatten_on_trip,
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
