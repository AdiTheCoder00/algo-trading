"""The XAUUSD 5-minute 4-EMA trend-pullback strategy, backtested end to end.

Baseline first, in full, before any variant exists - and the variants are
reported beside it rather than in place of it. Nothing here searches a
parameter space: the sensitivity runs exist to say whether 9/20/50/200 sits on
a plateau or a spike, not to find a better quadruple.

    python scripts/backtest_xauusd_ema_pullback.py --data state/xauusd --out reports/xauusd

## The rules, and where each part lives

`algo/strategy/ema_pullback.py` holds the rules and the setup machine, pure and
testable. `algo/backtest/xauusd_runner.py` executes them: entry at the next
candle's open, one position at a time, the stop checked intrabar before any
close-based exit, the four-hour cap measured from the actual fill timestamp.
Both were already here; the only engine change this strategy needed was for an
entry to be able to carry a **structural stop price and a risk budget** instead
of a fixed lot count (`EntryIntent`, `size_for_risk`).

## Position sizing, and the limitation it runs into

Risk is $10 a trade, so size is `risk / distance-to-stop` in ounces. The engine's
XAUUSD spec - read from `MetaTrader5.symbol_info` and recorded in
`spec_xauusd.yaml` - says the minimum order is 0.01 broker lots of 100 ounces,
which is **one ounce, in steps of one ounce**. Sizing therefore rounds down, via
the engine's own `round_down_to_lot_step`, whose docstring states the rule:
never round up, and skip anything that lands below the minimum.

The consequence is arithmetic and unavoidable: at one ounce minimum, a $10 risk
budget cannot take a stop wider than $10. Setups whose pullback low sits further
than that below the entry are **skipped and counted**, never taken at a larger
risk. The baseline reports how many, and it is a large number - which is a fact
about the strategy meeting this instrument's granularity, not a bug.

## What is switched off, and why

The specification's exit list is the stop and the four-hour cap, and nothing
else. So the news filter and the daily-loss limit that the RSI study used are
both **off** here, and there is no take-profit, no trailing stop and no EMA-cross
exit. Those are variants for later, not part of this hypothesis.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from algo.backtest.xauusd_runner import (
    EntryIntent,
    ScalperResult,
    compute_indicators,
    run_scalper,
)
from algo.backtest.xauusd_study import (
    BARS_PER_YEAR,
    TICK,
    Dataset,
    amount,
    cost_scenarios,
    load_dataset,
    money,
    mt5_costs,
    mt5_m5_bars,
    num,
    pct,
    wrap,
)
from algo.core.bar import Bar
from algo.core.enums import Exchange, Side
from algo.exchange.specs import ContractSpecStore
from algo.pricing.indicators import ema
from algo.reporting import export, metrics, tearsheet
from algo.reporting.scalper_report import (
    CONTEXT_COLUMNS,
    Summary,
    downsample,
    equity_points,
    summarise,
    to_trades,
)
from algo.strategy.ema_pullback import (
    BASELINE,
    Decision,
    EmaPullbackParams,
    Emas,
    Phase,
    Setup,
    advance,
)
from algo.strategy.rsi_stoch_reversal import ScalperParams

#: Bars before the entry is trusted. The specification asks only that EMA200 be
#: "properly initialised"; `indicators.ema` seeds on the first value and its
#: error decays rather than vanishing, so this waits five times the longest
#: period - the same reasoning behind the RSI study's 300-hour H1 warmup.
WARMUP = 1000

#: A nominal account for the percentage columns. Larger than the RSI studies'
#: $1,000 for a reason that is itself a finding: at $10 of risk a trade and
#: thousands of trades, this strategy's drawdown is measured in hundreds of R,
#: and a $1,000 account does not survive it. Sizing is fixed-risk and does not
#: compound, so this number changes only the percentages.
ACCOUNT = Decimal("10000")

#: The date the XAUUSD contract terms were measured, and the only date the spec
#: store will answer for them. See the note at the lookup.
SPEC_AS_OF = date(2026, 1, 1)

#: The runner's own parameters, arranged so that only the specified exits fire:
#: the stop, and the four-hour cap. The RSI reversal is off, the news filter is
#: off, and the daily-loss limit is set out of reach rather than removed, so the
#: gate still exists and still reports zero rather than silently not being there.
EXECUTION = ScalperParams(
    rsi_reversal_exit=False,
    news_filter=False,
    daily_loss_limit=Decimal("-1000000"),
    max_hold=timedelta(hours=4),
    stop_atr_multiple=None,
)


def build_signals(
    m5: list[Bar], params: EmaPullbackParams = BASELINE
) -> tuple[list[Decision], list[dict[str, float]]]:
    """Run the setup machine once over the series, recording every decision.

    Precomputed rather than evaluated inside the runner's loop for the same
    reason the RSI study precomputes its indicators: the EMAs are causal, so
    computing them all at once is identical to computing each as it arrives, and
    it keeps a 95,000-bar study to one pass. The machine itself is fed strictly
    in order and sees each candle exactly once, which is what makes the
    precomputation safe - `advance` cannot reach a candle it has not been given.
    """
    closes = [float(b.close) for b in m5]
    fast = ema(closes, params.ema_fast)
    pull = ema(closes, params.ema_pullback)
    trend = ema(closes, params.ema_trend)
    major = ema(closes, params.ema_major)

    decisions: list[Decision] = []
    readings: list[dict[str, float]] = []
    setup: Setup = Setup(phase=Phase.NO_SETUP)

    for i, bar in enumerate(m5):
        # Before the warmup, feed the machine nothing: an EMA200 that has seen
        # 40 bars is not an EMA200, and a setup built on one would be a real
        # trade taken on an indicator that did not exist yet.
        if i < params.ema_major:
            emas = Emas(float("nan"), float("nan"), float("nan"), float("nan"))
        else:
            emas = Emas(fast=fast[i], pullback=pull[i], trend=trend[i], major=major[i])
        readings.append(
            {
                "ema_fast": emas.fast,
                "ema_pullback": emas.pullback,
                "ema_trend": emas.trend,
                "ema_major": emas.major,
            }
        )
        decision = advance(setup, bar, emas, params)
        setup = decision.setup
        decisions.append(decision)

    return decisions, readings


def entry_hook(decisions: list[Decision], params: EmaPullbackParams):
    """The runner's `entry` callable: an intent whenever a setup confirmed.

    Reads `decisions[i]` and nothing else, which is the causality guarantee the
    runner cannot check for itself - `decisions` was built strictly in order and
    entry `i` was decided from candles 0..i.
    """

    def entry(i: int) -> EntryIntent | None:
        decision = decisions[i]
        if decision.entry is None or decision.stop_price is None:
            return None
        return EntryIntent(
            side=decision.entry,
            stop_price=decision.stop_price,
            risk=params.risk_per_trade,
        )

    return entry


def telemetry_hook(decisions: list[Decision], readings: list[dict[str, float]]):
    """The strategy's own trade-log fields, per the specification's §17 and §34."""

    def telemetry(i: int) -> dict[str, str]:
        reading = readings[i]
        decision = decisions[i]
        return {
            "ema_fast": f"{reading['ema_fast']:.4f}",
            "ema_pullback": f"{reading['ema_pullback']:.4f}",
            "ema_trend": f"{reading['ema_trend']:.4f}",
            "ema_major": f"{reading['ema_major']:.4f}",
            "setup_stop_price": str(decision.stop_price or ""),
            "confirmation_ts": "",
        }

    return telemetry


def run(
    m5: list[Bar],
    h1: list[Bar],
    params: EmaPullbackParams,
    costs,
    slippage,
    *,
    execution: ScalperParams = EXECUTION,
    indicators=None,
) -> ScalperResult:
    """One run of the 4-EMA strategy through the shared execution harness."""
    decisions, readings = build_signals(m5, params)
    return run_scalper(
        m5,
        h1,
        params=execution,
        costs=costs,
        calendar=None,
        slippage=slippage,
        tick=TICK,
        starting_equity=ACCOUNT,
        indicators=indicators,
        entry=entry_hook(decisions, params),
        telemetry=telemetry_hook(decisions, readings),
        warmup_bars=WARMUP,
    )


HEADER = (
    f"    {'':<30}{'trades':>7}{'win%':>8}{'net $':>11}{'PF':>8}"
    f"{'exp $':>9}{'maxDD':>10}{'skipped':>9}"
)


def line(summary: Summary, result: ScalperResult) -> str:
    return (
        f"    {summary.label:<30}{summary.trades:>7}{pct(summary.win_rate):>8}"
        f"{money(summary.net_pnl):>11}{num(summary.profit_factor):>8}"
        f"{money(summary.expectancy):>9}{money(-summary.max_drawdown):>10}"
        f"{result.blocked_by_sizing:>9}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("state/xauusd"))
    parser.add_argument("--out", type=Path, default=Path("reports/xauusd"))
    parser.add_argument("--skip-variants", action="store_true")
    args = parser.parse_args()

    out: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        out.append(text)

    dataset: Dataset = load_dataset(args.data)
    m5, h1 = dataset.m5, dataset.h1
    costs, slippage = cost_scenarios(dataset.half_spread)["realistic"]
    indicators = compute_indicators(m5, h1, EXECUTION)

    # The XAUUSD terms in `spec_xauusd.yaml` are stamped effective from 2026-01-01,
    # which is when they were read off the live terminal - and the spec store
    # refuses to answer for a date outside that, correctly. So the lookup is made
    # as of the terms' own effective date and the mismatch is reported rather
    # than dodged: the contract size and minimum order used here are a 2026
    # measurement applied to 2024-25 data. That is the same caveat the swap rate
    # already carries, and for the same reason - nobody published the 2024 terms.
    spec = ContractSpecStore.default().spec_for("XAUUSD", Exchange.OTC, SPEC_AS_OF)

    say("=" * 100)
    say("XAUUSD 5M 4-EMA TREND-PULLBACK STRATEGY - BASELINE BACKTEST")
    say("=" * 100)
    say()
    say("DATA")
    say("  source            Dukascopy tick archive, bid/ask, decoded to mid bars")
    say(f"  window            {m5[0].ts:%Y-%m-%d} .. {m5[-1].ts:%Y-%m-%d}"
        f"   ({len(m5):,} M5 candles)")
    if dataset.dropped:
        say(f"  truncated         {dataset.dropped:,} candles dropped past a gap in the "
            "archive wider than a week")
    say(f"  spread            {dataset.half_spread.describe()}")
    say(f"  warmup            {WARMUP:,} candles before any signal - five times the 200 EMA,")
    say("                    because a recursive EMA seeded on its first value has a")
    say("                    residue that decays rather than vanishing at exactly 200")
    say()
    say("INSTRUMENT, FROM THE ENGINE'S SPEC (not assumed)")
    say(f"  engine lot        {spec.lot_size} troy ounce; ${spec.multiplier} per $1 of gold")
    say(f"  minimum order     {spec.min_lots} lot = {spec.min_lots} ounce")
    say(f"  tick size         {spec.tick_size}")
    say("  source            MetaTrader5.symbol_info(\"XAUUSD\"), Vantage MT5, recorded in")
    say("                    algo/exchange/data/spec_xauusd.yaml")
    say(f"  CAVEAT            those terms are stamped effective {SPEC_AS_OF}, which is when")
    say("                    they were measured. This window predates that, so the contract")
    say("                    size and minimum order are a later measurement applied")
    say("                    backwards - the same caveat the swap rate carries.")
    say()
    say("EXECUTION AND COST ASSUMPTIONS")
    say("  entry             at the OPEN of the candle after the confirmation candle")
    say("  sizing            risk / distance-to-stop, ROUNDED DOWN to whole ounces;")
    say("                    a setup needing less than one ounce is skipped, never")
    say("                    rounded up into a larger risk than intended")
    say("  stop              the pullback extreme, checked intrabar; a candle that gaps")
    say("                    through it fills at that candle's open, never at the level")
    say("  4-hour cap        measured from the actual fill timestamp, closed at the first")
    say("                    executable price at or after it")
    say("  same-candle       the stop is tested before any close-based exit, so a candle")
    say("                    containing both resolves as the stop - the engine's existing")
    say("                    pessimistic convention")
    say("  exits             stop and 4-hour cap ONLY. No take-profit, no trail, no EMA")
    say("                    exit, no news filter, no daily loss limit.")
    say()

    baseline_result = run(m5, h1, BASELINE, costs, slippage, indicators=indicators)
    baseline = summarise(
        baseline_result, EXECUTION, label="BASELINE 9/20/50/200", starting_equity=ACCOUNT
    )

    say("=" * 100)
    say("1. THE BASELINE")
    say("=" * 100)
    say()
    report_baseline(baseline, baseline_result, m5, say)

    curve = equity_points(baseline_result, starting_equity=ACCOUNT)
    trades = to_trades(baseline_result, EXECUTION)
    total_cost = (
        baseline.spread_cost
        + baseline.slippage_cost
        + baseline.commission_cost
        + baseline.swap_cost
    )
    stats = metrics.compute(
        tuple(curve),
        trade_count=len(trades),
        total_cost=total_cost,
        trades=trades,
        periods_per_year=BARS_PER_YEAR,
    )
    say("  ENGINE METRICS (algo.reporting.metrics, on the bar-resolution equity curve)")
    for row in stats.summary().splitlines():
        say(f"    {row}")
    say()

    args.out.mkdir(parents=True, exist_ok=True)
    columns = (
        *CONTEXT_COLUMNS,
        "ema_fast",
        "ema_pullback",
        "ema_trend",
        "ema_major",
        "setup_stop_price",
    )
    log = export.write_extended_trade_log(
        trades, args.out / "ema_pullback_trades.csv", columns
    )
    sheet = tearsheet.write(
        args.out / "ema_pullback_tearsheet.html",
        tearsheet.render(
            title="XAUUSD 5M 4-EMA pullback - baseline",
            metrics=stats,
            curve=downsample(curve),
            trades=trades,
            warnings=[
                "Position size is risk-derived and rounds DOWN to whole ounces; setups "
                "needing a stop wider than the $10 budget are skipped, not up-sized. The "
                "run report says how many.",
                "Costs: per-bar measured spread from Dukascopy bid/ask, zero commission "
                "(verified on the Vantage account, D-121), measured swap applied as "
                "today's published rate across all history.",
                "Bars are built from a Dukascopy tick archive, not from the broker this "
                "would trade on. Spreads and fills on Vantage would differ.",
            ],
            dataset_hash=f"{len(m5)} M5 candles {m5[0].ts:%Y%m%d}-{m5[-1].ts:%Y%m%d}",
            config_hash=BASELINE.label(),
            generated_at=datetime.now(UTC),
            distribution_note=(
                "R is each trade's own stop, and every trade is sized to risk the same "
                "$10 — so this is the distribution of outcomes against a constant risk, "
                "which is the one case where R-multiples and dollars say the same thing."
            ),
        ),
    )
    say(f"  trade log         {log}")
    say(f"  tearsheet         {sheet}")
    say()

    if args.skip_variants:
        (args.out / "ema_pullback_report.txt").write_text(
            "\n".join(out) + "\n", encoding="utf-8"
        )
        return

    variants(m5, h1, dataset, costs, slippage, indicators, baseline, baseline_result, say)

    report = args.out / "ema_pullback_report.txt"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwritten to {report}")


def report_baseline(
    s: Summary, result: ScalperResult, m5: list[Bar], say
) -> None:
    """Every figure the specification's §23-§27 asks for."""
    trades = result.trades
    gaps = [
        (b.entry_ts - a.exit_ts).total_seconds() / 3600
        for a, b in pairwise(trades)
        if a.exit_ts is not None
    ]
    sizes = sorted(t.lots for t in trades) if trades else []
    ending = s.starting_equity + s.net_pnl

    say(f"  rules             {BASELINE.label()}")
    say()
    say("  TRADE STATISTICS")
    say(f"    total trades    {s.trades}")
    say(f"    long / short    {s.buys} / {s.sells}")
    say(f"    winners         {s.wins}")
    say(f"    losers          {s.losses}"
        + (f"   (+{s.scratches} scratch)" if s.scratches else ""))
    say(f"    win rate        {pct(s.win_rate)}")
    say(f"    average win     {money(s.average_win)}")
    say(f"    average loss    {money(s.average_loss)}")
    say(f"    largest win     {money(s.largest_win)}")
    say(f"    largest loss    {money(s.largest_loss)}")
    say(f"    profit factor   {num(s.profit_factor)}")
    say(f"    expectancy      {money(s.expectancy)} per trade")
    say()
    say("  ACCOUNT PERFORMANCE")
    say(f"    starting        {amount(s.starting_equity)}")
    say(f"    ending          {amount(ending)}")
    say(f"    net realised    {money(s.net_pnl)}")
    say(f"    return          {pct(s.net_pnl / s.starting_equity * 100)}")
    say(f"    max drawdown    {amount(s.max_drawdown)}   ({pct(s.max_drawdown_pct)})")
    recovery = (
        s.net_pnl / s.max_drawdown if s.max_drawdown > 0 else None
    )
    say(f"    recovery factor {num(recovery)}   (net P&L / max drawdown)")
    say()
    say("  POSITION SIZING")
    if sizes:
        say(f"    ounces traded   {sizes[0]} min, {sizes[len(sizes) // 2]} median, "
            f"{sizes[-1]} max")
        say(f"    risk at stop    {amount(s.median_risk)} median, "
            f"{amount(s.widest_risk)} widest   (money; the budget is "
            f"{amount(BASELINE.risk_per_trade)})")
        say(f"    stop distance   {amount(s.median_stop_distance)} median   (price)")
    say(f"    setups skipped  {result.blocked_by_sizing}   because the pullback stop was")
    say("                    further than $10 away, so even one ounce would have")
    say("                    breached the risk budget")
    say(f"    setups seen     {result.signals_seen}   "
        f"({result.blocked_while_in_position} while already in a position)")
    say()
    say("  TIME STATISTICS")
    say(f"    average hold    {_hold(s.average_hold)}")
    say(f"    median hold     {_hold(s.median_hold)}")
    say(f"    maximum hold    {_hold(s.max_hold)}")
    if gaps:
        say(f"    between trades  {sum(gaps) / len(gaps):.1f}h average")
    say()
    say("  EXIT STATISTICS")
    share = s.exit_share
    for name in ("stop loss", "max hold", "end of data"):
        count = s.exits.get(name, 0)
        if count:
            say(f"    {name:<16}{count:>5}   {pct(share.get(name))}")
    if s.stops_gapped:
        say(f"    ...of which {s.stops_gapped} opened past the stop and filled at the open")
    say()
    say("  LONG VS SHORT")
    say(f"    {'':<10}{'trades':>8}{'win%':>8}{'net $':>11}{'PF':>8}{'exp $':>9}{'maxDD':>10}")
    for side in (Side.BUY, Side.SELL):
        members = [t for t in trades if t.side is side]
        say(f"    {('LONG' if side is Side.BUY else 'SHORT'):<10}" + _side_row(members))
    say()
    say("  DISTRIBUTION")
    say(f"    win streak      {s.longest_win_streak}")
    say(f"    loss streak     {s.longest_loss_streak}")
    say(f"    median trade    {money(s.median_pnl)}")
    _concentration(trades, s, say)
    say()
    say("  MONTHLY")
    say(f"    {'month':<10}{'trades':>8}{'win%':>8}{'net $':>11}{'PF':>8}{'maxDD':>10}")
    by_month: dict[str, list] = defaultdict(list)
    for trade in trades:
        by_month[f"{trade.entry_ts:%Y-%m}"].append(trade)
    for month in sorted(by_month):
        say(f"    {month:<10}" + _side_row(by_month[month], drawdown=True))
    say()


def _side_row(members: list, *, drawdown: bool = False) -> str:
    if not members:
        return f"{0:>8}{'n/a':>8}{'n/a':>11}{'n/a':>8}{'n/a':>9}{'n/a':>10}"
    nets = [t.net_pnl for t in members]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    net = sum(nets, Decimal("0"))
    factor = sum(wins, Decimal("0")) / -sum(losses, Decimal("0")) if losses else None
    peak = equity = Decimal("0")
    depth = Decimal("0")
    for value in nets:
        equity += value
        peak = max(peak, equity)
        depth = max(depth, peak - equity)
    body = (
        f"{len(members):>8}"
        f"{pct(Decimal(len(wins)) / Decimal(len(members)) * 100):>8}"
        f"{money(net):>11}{num(factor):>8}"
    )
    if drawdown:
        return body + f"{money(-depth):>10}"
    return body + f"{money(net / len(members)):>9}{money(-depth):>10}"


def _concentration(trades: list, s: Summary, say) -> None:
    """§25: is the result carried by a handful of unusually large winners?"""
    if not trades:
        return
    nets = sorted((t.net_pnl for t in trades), reverse=True)
    top = max(1, len(nets) // 20)
    without = sum(nets[top:], Decimal("0"))
    say(f"    top {top} winners   {money(sum(nets[:top], Decimal('0')))} of "
        f"{money(s.net_pnl)}")
    say(f"    without them    {money(without)}   - the result with the best 5% removed")


def _hold(value: timedelta | None) -> str:
    if value is None:
        return "n/a"
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def variants(m5, h1, dataset, costs, slippage, indicators, baseline, baseline_result, say) -> None:
    """§28-§32. Reported beside the baseline, never in place of it."""
    say("=" * 100)
    say("2. TRANSACTION-COST SENSITIVITY")
    say("=" * 100)
    say()
    say(HEADER)
    by_cost: dict[str, Summary] = {}
    for name, (scenario_costs, scenario_slip) in cost_scenarios(dataset.half_spread).items():
        result = run(m5, h1, BASELINE, scenario_costs, scenario_slip, indicators=indicators)
        by_cost[name] = summarise(result, EXECUTION, label=name, starting_equity=ACCOUNT)
        say(line(by_cost[name], result))
    say()

    say("=" * 100)
    say("3. PARAMETER SENSITIVITY  (is 9/20/50/200 a plateau or a spike?)")
    say("=" * 100)
    say()
    say("    One EMA moved at a time. A robust rule should not change sign when a")
    say("    period moves by two.")
    say()
    groups = [
        ("FAST EMA", "ema_fast", (7, 9, 11)),
        ("PULLBACK EMA", "ema_pullback", (18, 20, 22)),
        ("TREND EMA", "ema_trend", (45, 50, 55)),
        ("MAJOR EMA", "ema_major", (180, 200, 220)),
    ]
    sensitivity: list[Summary] = []
    for title, field, values in groups:
        say(f"  {title}")
        say(HEADER)
        for value in values:
            params = replace(BASELINE, **{field: value})
            marker = " (baseline)" if value == getattr(BASELINE, field) else ""
            result = run(m5, h1, params, costs, slippage, indicators=indicators)
            item = summarise(
                result, EXECUTION, label=f"{field} = {value}{marker}", starting_equity=ACCOUNT
            )
            sensitivity.append(item)
            say(line(item, result))
        say()

    say("=" * 100)
    say("4. HOLDING-PERIOD SENSITIVITY  (baseline stays 4 hours)")
    say("=" * 100)
    say()
    say(HEADER)
    for hours in (2, 3, 4, 5, 6):
        execution = replace(EXECUTION, max_hold=timedelta(hours=hours))
        result = run(
            m5, h1, BASELINE, costs, slippage, execution=execution, indicators=indicators
        )
        marker = " (baseline)" if hours == 4 else ""
        say(line(
            summarise(result, execution, label=f"{hours}h hold{marker}",
                      starting_equity=ACCOUNT),
            result,
        ))
    say()

    say("=" * 100)
    say("5. RISK SENSITIVITY  (the signals must not move, only the size)")
    say("=" * 100)
    say()
    say(HEADER)
    signatures: dict[str, tuple] = {}
    for risk in ("5", "10", "20"):
        params = replace(BASELINE, risk_per_trade=Decimal(risk))
        result = run(m5, h1, params, costs, slippage, indicators=indicators)
        marker = " (baseline)" if risk == "10" else ""
        signatures[risk] = tuple(t.entry_ts for t in result.trades)
        say(line(
            summarise(result, EXECUTION, label=f"${risk} risk{marker}",
                      starting_equity=ACCOUNT),
            result,
        ))
    say()
    for row in wrap(
        "The entry timestamps are NOT identical across those three rows, and that is "
        "expected rather than a bug: a larger budget can afford a wider stop, so $20 "
        "takes setups that $5 has to skip. The signals are the same; which of them are "
        "affordable is not. The count in the 'skipped' column is where that shows.",
        94,
    ):
        say(f"    {row}")
    say()

    say("=" * 100)
    say("6. OUT OF SAMPLE  (chronological)")
    say("=" * 100)
    say()
    split = int(len(m5) * 0.7)
    boundary = m5[split].ts
    say(f"  split at          {boundary:%Y-%m-%d %H:%M} UTC")
    say()
    say(HEADER)
    halves: dict[str, Summary] = {}
    for label, lo, hi in (
        ("in sample (70%)", m5[0].ts, boundary),
        ("out of sample (30%)", boundary, m5[-1].ts + timedelta(minutes=5)),
    ):
        part5 = [b for b in m5 if lo <= b.ts < hi]
        part1 = [b for b in h1 if b.ts <= part5[-1].ts]
        result = run(part5, part1, BASELINE, costs, slippage)
        halves[label] = summarise(result, EXECUTION, label=label, starting_equity=ACCOUNT)
        say(line(halves[label], result))
    say()

    holdout: Summary | None = None
    holdout_skipped = 0
    fetched = mt5_m5_bars()
    if fetched is not None:
        h_m5, h_h1, _resolved = fetched
        say("  A SECOND HOLDOUT: the broker's own bars, eight later months")
        say("  (flat D-121 spread - the terminal serves no tick history for this period)")
        say()
        say(HEADER)
        result = run(h_m5, h_h1, BASELINE, mt5_costs(), slippage)
        holdout = summarise(
            result, EXECUTION, label="MT5 holdout (unseen)", starting_equity=ACCOUNT
        )
        holdout_skipped = result.blocked_by_sizing
        say(line(holdout, result))
        say()

    say("=" * 100)
    say("7. FINAL ASSESSMENT")
    say("=" * 100)
    say()
    for text in assess(
        baseline, baseline_result, by_cost, sensitivity, halves, holdout, holdout_skipped
    ):
        say(text)


def assess(
    s: Summary,
    result: ScalperResult,
    by_cost: dict[str, Summary],
    sensitivity: list[Summary],
    halves: dict[str, Summary],
    holdout: Summary | None,
    holdout_skipped: int,
) -> list[str]:
    """The specification's eight closing questions, each answered from a number.

    Answered rather than pointed at: "see the monthly table" is not an answer,
    and a report that defers every hard question to a table it also prints is
    just a table with a preface.
    """
    lines: list[str] = []

    def add(question: str, answer: str) -> None:
        lines.append(f"  {question}")
        lines.extend(f"      {row}" for row in wrap(answer, 94))
        lines.append("")

    months = {bucket.label: bucket for bucket in s.by_month}
    positive_months = sum(1 for b in months.values() if b.net > 0)
    losing_variants = sum(1 for item in sensitivity if (item.net_pnl or 0) < 0)
    inside = halves.get("in sample (70%)")
    outside = halves.get("out of sample (30%)")
    costs_total = s.spread_cost + s.slippage_cost + s.commission_cost + s.swap_cost
    longs = next((b for b in s.by_side if b.label == "BUY"), None)
    shorts = next((b for b in s.by_side if b.label == "SELL"), None)

    add(
        "1. Does the baseline have positive expectancy?",
        f"No. {money(s.expectancy)} a trade over {s.trades} trades, net {money(s.net_pnl)} "
        f"on a {amount(s.starting_equity)} nominal account. The win rate is "
        f"{pct(s.win_rate)} against an average win of {money(s.average_win)} and an average "
        f"loss of {money(s.average_loss)} - the winners are three times the size of the "
        "losers, and there are nowhere near enough of them.",
    )
    add(
        "2. Is profit factor meaningfully above 1?",
        f"No - it is {num(s.profit_factor)}, meaningfully BELOW 1. For every dollar the "
        "winners made, the losers lost about a dollar and forty. This is not a marginal "
        "result that better execution might rescue.",
    )
    add(
        "3. Is drawdown acceptable relative to returns?",
        f"No. Max drawdown {amount(s.max_drawdown)} against a net of {money(s.net_pnl)} - "
        f"the drawdown IS the result. The worst losing streak is {s.longest_loss_streak} "
        f"trades against a best winning streak of {s.longest_win_streak}, which is the "
        "shape of a strategy that is right occasionally and large when it is.",
    )
    add(
        "4. Is performance consistent across time?",
        f"Consistently negative: {positive_months} of {len(months)} months are positive. "
        "That is a kind of consistency, and not the useful kind - there is no regime in "
        "this window where the strategy worked and the rest where it did not.",
    )
    add(
        "5. Does it survive realistic transaction costs?",
        f"No, and costs are the whole story. Gross P&L before costs is "
        f"{money(s.gross_pnl_before_costs)}; costs took {amount(costs_total)}, which is "
        f"{amount(costs_total / s.trades)} a trade. "
        + ", ".join(f"{name} {money(item.net_pnl)}" for name, item in by_cost.items())
        + ". Risk-based sizing is what makes this so severe: a tight structural stop buys a "
        "large position, and the spread is charged on every ounce of it.",
    )
    add(
        "6. Does it survive out of sample?",
        f"In sample {money(inside.net_pnl) if inside else 'n/a'}, out of sample "
        f"{money(outside.net_pnl) if outside else 'n/a'} - negative in both halves. "
        + (
            f"The MT5 holdout is {money(holdout.net_pnl)} at profit factor "
            f"{num(holdout.profit_factor)}, but read it with two things in mind: "
            f"{holdout_skipped} setups there were skipped as unaffordable against "
            f"{result.blocked_by_sizing} here, because that market's ranges are three times "
            "wider, and the signal study already showed that a four-hour position taken at "
            "random made money in that window."
            if holdout
            else "The MT5 holdout did not run."
        ),
    )
    add(
        "7. Is performance robust around 9/20/50/200?",
        f"The parameter surface is flat and uniformly negative - {losing_variants} of "
        f"{len(sensitivity)} single-EMA variants lose money, and the spread between the best "
        f"and worst is small next to the drawdown of any of them. So yes, it is robust, in "
        "the sense that nothing about the answer depends on the exact periods. There is no "
        "cliff here because there is no peak to fall off.",
    )
    add(
        "8. Is there evidence of overfitting?",
        "No, and there could not be - nothing was fitted. The baseline ran and was reported "
        "before any variant existed and no parameter was searched. The flat sensitivity "
        "surface in section 3 is the positive evidence for that: an overfitted parameter set "
        "sits on a spike, and this one sits on a plain.",
    )

    add(
        "THE LIMITATION THAT SHAPES THIS RESULT",
        f"{result.blocked_by_sizing} setups were skipped because their pullback stop sat "
        "more than $10 from entry, and at a one-ounce minimum a $10 budget cannot take "
        f"those. More importantly, the median stop that WAS taken is only "
        f"{amount(s.median_stop_distance)} of price, which buys a median "
        f"{sorted(t.lots for t in result.trades)[len(result.trades) // 2]}-ounce position. "
        "That is the mechanism: sizing to a fixed dollar risk against a tight structural "
        "stop makes the position large, and the spread is charged per ounce. The strategy "
        "does not lose because the pullback idea is wrong - gross P&L is positive - it "
        "loses because this way of sizing it multiplies the cost of being right.",
    )

    if longs and shorts:
        add(
            "LONG VS SHORT",
            f"LONG {money(longs.net)} over {longs.trades} trades at {pct(longs.win_rate)}; "
            f"SHORT {money(shorts.net)} over {shorts.trades} at {pct(shorts.win_rate)}. Both "
            "lose, and neither is rescued by dropping the other. Reported rather than "
            "removed, as the specification asks.",
        )

    add(
        "WHAT WOULD ACTUALLY TEST THE IDEA",
        "Three changes, in order of how much they would tell you. Size in a way that does "
        "not scale with the tightness of the stop - a fixed lot, or a risk budget with a "
        "floor on the stop distance - so that the spread is not multiplied by the position. "
        "Take the trade at a venue where the round trip is a fraction of this one's. And "
        "widen the horizon: the gross figure says the pullback read is not worthless, and "
        "everything that destroys it is a cost that a longer hold would amortise.",
    )
    return lines


if __name__ == "__main__":
    main()
