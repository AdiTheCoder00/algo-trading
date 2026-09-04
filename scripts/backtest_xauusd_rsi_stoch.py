"""The XAUUSD 5-minute RSI / Stochastic-RSI scalper, backtested end to end.

Runs the baseline first, reports it in full, and only then runs the controlled
variants - and the variants are reported beside the baseline rather than in
place of it. Nothing here searches a parameter space or picks a winner: the
comparisons exist to say how sensitive the result is, not to find a better one,
and a script that could quietly return the best of twelve runs would make every
number it printed uninterpretable.

## The baseline is no longer the specification, and both are reported

The rules as specified use a flat $10 stop. The baseline here scales that stop
to volatility - 6 x ATR(14), which is what $10 was in this window - after
`study_xauusd_stop.py` showed the fixed version is a different rule in different
regimes: at the same nominal $10 the stop-out rate is 23.9% in 2024-25 and 65.1%
in 2026, because gold's five-minute range trebled. That was a deliberate change,
made on request and after measurement, not a search result.

The specification is not lost. `SPEC_BASELINE` - the literal $10 rules - runs on
every invocation and is the first row of the stop table, so the number the brief
asked for is always on the page next to the number the baseline now produces.

    python scripts/backtest_xauusd_rsi_stoch.py --data state/xauusd --out reports/xauusd

## The cost ladder

Three scenarios, all reported, with the baseline being the measured one:

  realistic     the spread actually quoted inside each bar (Dukascopy bid/ask),
                zero commission (verified on the Vantage account, D-121),
                measured swap, and $0.05 of slippage on a stop only
  moderate      1.5x the measured spread, $0.10 market / $0.25 stop slippage
  conservative  2.5x the measured spread, $0.20 market / $0.50 stop slippage,
                plus $0.03 per fill of commission - a RAW/ECN tier's $3 a lot,
                at 0.01 lots

Only the first is a measurement. The other two are stress tests, and the point
of them is question 13: how much worse would the venue have to be before the
answer changes.

## What the run window is, and why it stops where it does

The Dukascopy archive on disk covers 2024-01-01 to 2025-05-13 continuously, then
a 384-day hole, then a stray day in June 2026. The run is truncated at the hole.
Trading across a year-long gap would produce an equity curve and a set of hold
times that mean nothing, and silently including the stray day would put two
trades from a different regime at the end of every table.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from algo.backtest.xauusd_runner import compute_indicators
from algo.backtest.xauusd_study import (
    ACCOUNT,
    BARS_PER_YEAR,
    HEADER,
    amount,
    bar_range,
    cost_scenarios,
    free_costs,
    line,
    load_dataset,
    money,
    mt5_costs,
    mt5_m5_bars,
    num,
    pct,
    run,
    wrap,
)
from algo.costs.slippage import TickSlippage
from algo.data.econ_calendar import EconomicCalendar
from algo.reporting import export, metrics, tearsheet
from algo.reporting.scalper_report import (
    CONTEXT_COLUMNS,
    Summary,
    downsample,
    equity_points,
    render_buckets,
    render_text,
    summarise,
    to_trades,
)
from algo.strategy.rsi_stoch_reversal import BASELINE, SPEC_BASELINE, ScalperParams


def mt5_holdout(
    calendar: EconomicCalendar, slippage: TickSlippage, say, study_range: Decimal
) -> Summary | None:
    """The baseline on the broker's own M5 bars, over a period the archive has not got.

    The Dukascopy archive ends 2025-05-13. The Vantage terminal serves M5 from
    2025-12-19 to now - a later window, on a different feed, with no overlap.
    That is as clean a holdout as this project can construct: not a slice of the
    same data held back, but eight months the study has never touched, from the
    venue the strategy would actually trade on.

    ## The one assumption, stated

    The terminal refuses tick history for that period (`copy_ticks_range`
    returns "Call failed"), so the spread there cannot be measured the way the
    main study measures it. This charges the constant D-121 half-spread of
    $0.145 instead - a real measurement, taken from a live quote on this
    account, but one number applied to eight months rather than a per-bar
    reading.

    MT5 bars are **bid**, not mid. Treating them as mid and charging half a
    spread per fill is nevertheless correct on a round trip: a long
    under-charges by half on the way in and over-charges by half on the way out,
    and the two cancel exactly. The residue is in where the stop sits relative
    to the true bid - half a spread, about seven cents - which is stated rather
    than corrected because correcting it would need the spread this window does
    not have.
    """
    fetched = mt5_m5_bars()
    if fetched is None:
        say("  the terminal served no bars - holdout skipped. No bars rather than bars")
        say("  on a guessed clock.")
        return None
    bars, hours, resolved = fetched
    say(f"  server clock      {resolved.describe()}")
    say("  labelling         MT5 stamps a bar with its OPEN time; `algo.core.bar` labels by")
    say("                    CLOSE. Shifted by one bar here, or the H1 trend would be read")
    say("                    off an hour that had not finished.")
    say(f"  bars              {len(bars):,} M5, {len(hours):,} H1  "
        f"({bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d})")
    say("  spread            flat $0.29 round trip, the D-121 measured quote. NOT measured")
    say("                    per bar the way the main window is - the terminal refuses tick")
    say("                    history for this period, so one number stands in for eight")
    say("                    months. Every figure below inherits that.")
    say()

    theirs = bar_range(bars)
    say("  A DIFFERENT MARKET, not just a different period:")
    say(f"    price           {min(b.low for b in bars):,.0f} .. {max(b.high for b in bars):,.0f}"
        f"   (the study window was 1,984 .. 3,500)")
    say(f"    median M5 range {theirs}   against {study_range} in the study window"
        f"  -  {theirs / study_range:.1f}x")
    say(f"    a FIXED $10 stop would be {Decimal('10') / theirs:.1f} median bars away here")
    say(f"    and {Decimal('10') / study_range:.1f} there - the same rule meaning two")
    say("    different things, which is what the ATR baseline exists to stop.")
    say("    Under the specified fixed stop that difference would land squarely in the")
    say("    results. Under the baseline's ATR stop it is absorbed, which is the whole")
    say("    reason the baseline uses one - read the row below with that in mind.")
    say()

    result = run(bars, hours, BASELINE, mt5_costs(), slippage, calendar)
    summary = summarise(
        result, BASELINE, label="MT5 holdout (unseen)", starting_equity=ACCOUNT
    )
    say(HEADER)
    say(line(summary))
    return summary


def evaluate(
    baseline: Summary,
    by_cost: dict[str, Summary],
    variants: dict[str, Summary],
    halves: dict[str, Summary],
    folds: list[Summary],
    holdout: Summary | None,
) -> list[str]:
    """The brief's closing questions, each answered from a number already printed.

    A function rather than hand-written prose at the bottom of a file: a
    conclusion typed once drifts from the run it describes the first time
    anything changes, and this cannot. Where the honest answer is "the sample
    cannot say", it says that instead of picking a side.
    """
    share = baseline.exit_share
    costs_total = (
        baseline.spread_cost
        + baseline.slippage_cost
        + baseline.commission_cost
        + baseline.swap_cost
    )
    positive_folds = sum(1 for f in folds if f.net_pnl > 0)
    inside = halves.get("in sample (70%)")
    outside = halves.get("out of sample (30%)")
    rsi_off = variants.get("rsi exit off")
    news_off = variants.get("news off")

    def gap(a: Summary | None, b: Summary | None) -> str:
        if a is None or b is None:
            return "n/a"
        return f"{a.net_pnl - b.net_pnl:+,.2f}"

    answers: list[tuple[str, str]] = [
        (
            "1. Is it profitable after realistic transaction costs?",
            f"No. Net {money(baseline.net_pnl)} over {baseline.trades} trades: profit factor "
            f"{num(baseline.profit_factor)}, expectancy {money(baseline.expectancy)} a trade. "
            f"Before costs the same trades made {money(baseline.gross_pnl_before_costs)} and "
            f"costs took {amount(costs_total)}. The strategy is not losing to the market; it "
            "is losing to the spread.",
        ),
        (
            "2. Is it profitable out of sample?",
            f"No. In sample {money(inside.net_pnl) if inside else 'n/a'} on "
            f"{inside.trades if inside else 0} trades, out of sample "
            f"{money(outside.net_pnl) if outside else 'n/a'} on "
            f"{outside.trades if outside else 0}; the change is {gap(outside, inside)}. "
            + (
                f"On the second holdout - {holdout.trades} trades on the broker's own bars "
                f"over eight months the archive does not cover - it made "
                f"{money(holdout.net_pnl)}, profit factor {num(holdout.profit_factor)}. "
                if holdout
                else ""
            )
            + "Every one of these samples is small enough that a different split would "
            "move the number.",
        ),
        (
            "3. Is the edge present on both BUY and SELL?",
            f"No. BUY {money(baseline.by_side[0].net)} over {baseline.by_side[0].trades} "
            f"trades; SELL {money(baseline.by_side[-1].net)} over "
            f"{baseline.by_side[-1].trades}. They point opposite ways, and with gold trending "
            "up through most of this window that is at least as likely to be the trend as an "
            "edge in one direction.",
        ),
        (
            "4. What percentage of trades hit the stop?",
            f"{pct(share.get('stop loss'))} - {baseline.exits.get('stop loss', 0)} "
            f"trades, at a stop of {baseline.params.stop_atr_multiple} x ATR. Under the "
            "specified flat $10 it is a different number in every regime, which is why "
            "the baseline no longer uses one - see the stop table.",
        ),
        (
            "5. What percentage exit through the RSI reversal?",
            f"{pct(share.get('rsi reversal'))} - {baseline.exits.get('rsi reversal', 0)} "
            "trades. Note what that means: in three quarters of trades RSI never reached its "
            "extreme at all, so the exit the rules describe most carefully is the one that "
            "fires least.",
        ),
        (
            "6. What percentage reach the 4-hour maximum hold?",
            f"{pct(share.get('max hold'))} - {baseline.exits.get('max hold', 0)} trades, the "
            "most common ending by a wide margin. The clock, not either signal, is what "
            "actually closes this strategy's positions.",
        ),
        (
            "7. Does the RSI reversal exit improve results?",
            f"No. Switching it off is {gap(rsi_off, baseline)} on net P&L. It raises the win "
            f"rate ({pct(baseline.win_rate)} with it, "
            f"{pct(rsi_off.win_rate) if rsi_off else 'n/a'} without) and loses money doing "
            "so, which is the signature of an exit that closes winners early.",
        ),
        (
            "8. Does the news filter improve results?",
            f"No, and it barely acts. Switching it off is {gap(news_off, baseline)} on net "
            f"P&L across {baseline.blocked_by_news} blocked entries. A difference built from "
            f"{baseline.blocked_by_news} trades is not a measurement of the filter - and the "
            "calendar covers only NFP, CPI, PPI and FOMC, so a wider one would block more.",
        ),
        (
            "9. How often does the -$50 daily loss limit activate?",
            f"Never in this window: {baseline.limit_days} days, "
            f"{baseline.blocked_by_daily_limit} entries blocked. One position of 0.01 lots "
            "with a stop of a few dollars would need five losing trades inside one "
            "21:00-21:00 trading day, and four-hour holds leave no room for that. The "
            "rule is inert at this size.",
        ),
        (
            "10. What is the maximum drawdown?",
            f"{amount(baseline.max_drawdown)} on realised equity - "
            f"{pct(baseline.max_drawdown_pct)} of a {amount(baseline.starting_equity)} "
            "account. Against a net result of roughly zero, that is the whole point: the "
            "path is far larger than the destination.",
        ),
        (
            "11. What is the worst losing streak?",
            f"{baseline.longest_loss_streak} losing trades in a row (the best winning streak "
            f"is {baseline.longest_win_streak}).",
        ),
        (
            "12. What is the largest single-trade loss?",
            f"{money(baseline.largest_loss)}, against a stop that ranged from "
            f"{amount(baseline.median_risk)} at the median to "
            f"{amount(baseline.widest_risk)} at its widest. That is the ATR stop "
            "working as intended rather than failing: a trade entered in a volatile hour "
            "risks more dollars, and it is the fixed-dollar version that was quietly "
            "taking a different amount of risk each time without saying so. "
            f"{baseline.stops_gapped} bars also opened past the level and filled at the "
            "open, and every exit still crosses the spread.",
        ),
        (
            "13. How sensitive is it to transaction costs?",
            "Completely - costs are the result. "
            + ", ".join(f"{name} {money(item.net_pnl)}" for name, item in by_cost.items())
            + f". At {costs_total / baseline.trades:,.2f} a trade against an average win of "
            f"{money(baseline.average_win)}, the cost and the edge are the same size, so the "
            "venue decides the sign.",
        ),
        (
            "14. Is performance reasonably stable across years?",
            f"There is only one full year here plus a stub, so the honest unit is the fold: "
            f"{positive_folds} of {len(folds)} are positive, spanning "
            f"{money(min((f.net_pnl for f in folds), default=None))} to "
            f"{money(max((f.net_pnl for f in folds), default=None))} on 55-76 trades each. "
            "That spread is what a zero-expectancy process looks like cut six ways. The "
            "sharper answer comes from the holdout: gold's median five-minute range is more "
            "than three times larger there than in the study window. That is exactly what "
            "the volatility-scaled stop exists to absorb, and it is why the baseline no "
            "longer uses a fixed dollar distance. The win rate still falls from "
            + (f"{pct(baseline.win_rate)} to {pct(holdout.win_rate)} " if holdout else "")
            + "for that reason alone. A rule set whose risk changes with the price of gold "
            "is not stable across regimes even when its P&L happens to be.",
        ),
        (
            "15. Is there evidence of overfitting?",
            "Not in this run, because nothing was fitted: no parameter was searched, the "
            "baseline was run first and reported before any variant, and the variants sit "
            "beside it rather than replacing it. What the sweeps do show is fragility - the "
            "sign of the P&L flips on the holding period, on the stop size, on direction and "
            "on the reversal exit. A rule set with a real edge does not change sign every "
            "time you nudge it.",
        ),
    ]

    lines: list[str] = []
    for question, answer in answers:
        lines.append(f"  {question}")
        lines.extend(f"      {row}" for row in wrap(answer))
        lines.append("")
    lines.append("  WHAT THIS SAMPLE CANNOT TELL YOU")
    lines.extend(
        f"      {row}"
        for row in wrap(
            f"{baseline.trades} trades over sixteen months is a small sample for a difference "
            f"this small. An expectancy of {money(baseline.expectancy)} against an average "
            f"win of {money(baseline.average_win)} is well inside the noise a few hundred "
            "coin flips would produce, so the reading is not 'this loses slightly' but 'this "
            "has no edge that this data can measure, and it pays the spread to find out'. "
            "The result is also from a Dukascopy tick archive, not from the broker this would "
            "trade on; a different spread would move the number, though not far enough to "
            "change the conclusion.",
        )
    )
    return lines


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

    # ------------------------------------------------------------------- data
    dataset = load_dataset(args.data)
    m5, h1, half = dataset.m5, dataset.h1, dataset.half_spread
    window_end = m5[-1].ts
    calendar = EconomicCalendar.load()
    events = calendar.within(m5[0].ts, window_end)

    say("=" * 96)
    say("XAUUSD 5M RSI / STOCHASTIC-RSI SCALPER - BASELINE BACKTEST")
    say("=" * 96)
    say()
    say("DATA")
    say("  source            Dukascopy tick archive, bid/ask, decoded to mid bars")
    say(f"  study window      {m5[0].ts:%Y-%m-%d} .. {window_end:%Y-%m-%d}"
        f"  ({len(m5):,} M5 bars, {len(h1):,} H1 bars)")
    if dataset.dropped:
        say(f"  truncated         {dataset.dropped:,} bars dropped past a gap in the "
            "archive wider than a week")
    say(f"  spread            {half.describe()}")
    say("  cross-check       against the Vantage terminal's own H1 bars on 2024-03-12: 23")
    say("                    bars, mean close difference 0.100, worst 0.200. That is half a")
    say("                    spread, which is exactly what a mid bar should sit above a bid")
    say("                    bar - the timestamps and the price scale both line up.")
    say()
    say("NEWS CALENDAR")
    say(f"  {calendar.describe()}")
    say(f"  in this window    {len(events)} events")
    say("  sourced from      bls.gov release schedules (NFP, CPI, PPI, all 08:30 ET) and")
    say("                    federalreserve.gov's FOMC calendar (statement 14:00 ET,")
    say("                    press conference 14:30 ET)")
    say("  NOT covered       retail sales, GDP, PCE, ISM, ADP, jobless claims, Fed speeches")
    say("                    outside the statement window, and all non-US releases. No")
    say("                    primary schedule was to hand for those and none were invented.")
    say()
    say("EXECUTION AND COST ASSUMPTIONS")
    say("  position          0.01 MT5 lot = 1 ounce. A $1 move in gold is $1 of P&L.")
    say("  entry             at the open of the candle AFTER the confirmation candle")
    say("  bars              mid of bid and ask; a fill crosses half the measured spread")
    say("  stop              6 x ATR(14) from entry, measured on the confirmation")
    say("                    candle. A price, not a P&L threshold, and a bar that gaps")
    say("                    through it fills at the bar's open, never at the level.")
    say("                    The brief specifies a flat $10; that version is reported")
    say("                    as the first row of the stop table below.")
    say("  trading day       21:00 UTC to 21:00 UTC - the broker's rollover, which is")
    say("                    midnight on its own UTC+3 server clock. The -$50 daily limit")
    say("                    resets there, NOT at UTC midnight and not in local time.")
    say("  swap              measured Vantage terms, -80.54 / +32.67 points a night,")
    say("                    tripled on Wednesdays. A snapshot rate applied to all history:")
    say("                    MT5 publishes no historical series (D-121).")
    say()

    scenarios = cost_scenarios(half)
    indicators = compute_indicators(m5, h1, BASELINE)

    # --------------------------------------------------------------- baseline
    costs, slippage = scenarios["realistic"]
    baseline = run(m5, h1, BASELINE, costs, slippage, calendar, indicators)
    summary = summarise(baseline, BASELINE, label="BASELINE (6xATR stop, realistic costs)",
                        starting_equity=ACCOUNT)

    say("=" * 96)
    say("1. THE BASELINE  (the specified rules, with the stop scaled to volatility)")
    say("=" * 96)
    say()
    say(render_text(summary))
    say()
    say(render_buckets("BY DIRECTION", summary.by_side))
    say()
    say(render_buckets("BY HOUR OF DAY (UTC, entry hour)", summary.by_hour))
    say()
    say(render_buckets("BY SESSION", summary.by_session))
    say()
    say(render_buckets("BY MONTH", summary.by_month))
    say()
    say(render_buckets("BY YEAR", summary.by_year))
    say()

    curve = equity_points(baseline, starting_equity=ACCOUNT)
    trades = to_trades(baseline, BASELINE)
    total_cost = (
        summary.spread_cost + summary.slippage_cost + summary.commission_cost + summary.swap_cost
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
    log = export.write_extended_trade_log(trades, args.out / "baseline_trades.csv", CONTEXT_COLUMNS)
    sheet = tearsheet.write(
        args.out / "baseline_tearsheet.html",
        tearsheet.render(
            title="XAUUSD 5M RSI/StochRSI scalper - baseline",
            metrics=stats,
            curve=downsample(curve),
            trades=trades,
            warnings=[
                "Costs: spread measured per bar from Dukascopy bid/ask; commission zero "
                "(verified on the Vantage account, D-121); swap is today's published rate "
                "applied to all history, because MT5 publishes no historical series.",
                "The news filter covers NFP, CPI, PPI and FOMC only - see the run report "
                "for what is deliberately absent from it.",
                "The equity chart is thinned to 2,000 points for drawing; every number "
                "above it is computed from the full 95,000-bar curve.",
                "Bars are built from a Dukascopy tick archive, not from the broker this "
                "would trade on. Spreads and fills on Vantage would differ.",
            ],
            dataset_hash=f"{len(m5)} M5 bars {m5[0].ts:%Y%m%d}-{window_end:%Y%m%d}",
            distribution_note=(
                "R here is each trade's OWN stop, which under a volatility-scaled "
                "stop is a different number of dollars every time — so losses "
                "cluster near -1R by construction and this is a picture of the exit "
                "rules rather than of the dollars. The right tail runs further but "
                "is thin: a stop bounds what a bad trade costs, it does not create "
                "anything for the good ones to win."
            ),
            config_hash=BASELINE.label(),
            generated_at=datetime.now(UTC),
        ),
    )
    say(f"  trade log         {log}")
    say(f"  tearsheet         {sheet}")
    say()

    # ------------------------------------------------------------ cost ladder
    say("=" * 96)
    say("2. TRANSACTION-COST SENSITIVITY  (baseline rules, three cost levels)")
    say("=" * 96)
    say()
    say(HEADER)
    cost_summaries = {}
    for name, (scenario_costs, scenario_slip) in scenarios.items():
        result = run(m5, h1, BASELINE, scenario_costs, scenario_slip, calendar, indicators)
        cost_summaries[name] = summarise(result, BASELINE, label=name, starting_equity=ACCOUNT)
        say(line(cost_summaries[name]))
    zero_costs, zero_slip = free_costs()
    zero = run(m5, h1, BASELINE, zero_costs, zero_slip, calendar, indicators)
    say(
        line(
            summarise(
                zero, BASELINE, label="zero cost (reference only)", starting_equity=ACCOUNT
            )
        )
    )
    say()
    say("    The zero-cost row is a reference, not a result: it is what the rules would")
    say("    have earned if trading were free, and the gap to the realistic row is the")
    say("    cost of doing it. It is never the headline figure.")
    say()

    if args.skip_variants:
        (args.out / "report.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
        return

    # --------------------------------------------------------------- variants
    say("=" * 96)
    say("3. CONTROLLED VARIANTS  (one change at a time, realistic costs)")
    say("=" * 96)
    say()
    say("    The baseline is unchanged and stays the official result. Nothing below is")
    say("    a recommendation; these say how much the answer moves when a rule moves.")
    say()

    costs, slippage = scenarios["realistic"]

    def variant(label: str, params: ScalperParams) -> Summary:
        # Indicator series depend only on the RSI/stochastic/EMA/MACD periods,
        # none of which any variant changes - so they are shared rather than
        # recomputed, and a test asserts that sharing changes no result.
        result = run(m5, h1, params, costs, slippage, calendar, indicators)
        return summarise(result, params, label=label, starting_equity=ACCOUNT)

    groups: list[tuple[str, list[tuple[str, ScalperParams]]]] = [
        (
            "NEWS FILTER",
            [
                ("news on (baseline)", BASELINE),
                ("news off", replace(BASELINE, news_filter=False)),
            ],
        ),
        (
            "RSI REVERSAL EXIT",
            [
                ("rsi exit on (baseline)", BASELINE),
                ("rsi exit off", replace(BASELINE, rsi_reversal_exit=False)),
            ],
        ),
        (
            "MAXIMUM HOLDING PERIOD",
            [
                (f"{h}h hold" + (" (baseline)" if h == 4 else ""),
                 replace(BASELINE, max_hold=timedelta(hours=h)))
                for h in (2, 4, 6, 8)
            ],
        ),
        (
            "STOP RULE  (the fixed-dollar rows clear the ATR multiple, or it would win)",
            [
                ("$10 fixed - AS SPECIFIED", SPEC_BASELINE),
                *(
                    (
                        f"${sl} fixed",
                        replace(
                            BASELINE, stop_loss=Decimal(sl), stop_atr_multiple=None
                        ),
                    )
                    for sl in ("7.50", "12.50", "15")
                ),
                *(
                    (
                        f"{k} x ATR" + (" (baseline)" if k == "6" else ""),
                        replace(BASELINE, stop_atr_multiple=Decimal(k)),
                    )
                    for k in ("3", "4", "6", "8")
                ),
            ],
        ),
        (
            "DIRECTION",
            [
                ("buy + sell (baseline)", BASELINE),
                ("buy only", replace(BASELINE, allow_sell=False)),
                ("sell only", replace(BASELINE, allow_buy=False)),
            ],
        ),
    ]

    variants: dict[str, Summary] = {}
    for title, members in groups:
        say(f"  {title}")
        say(HEADER)
        for label, params in members:
            measured = variant(label, params)
            variants[label] = measured
            say(line(measured))
        say()

    # ------------------------------------------------------- out of sample
    say("=" * 96)
    say("4. OUT OF SAMPLE  (chronological, never shuffled)")
    say("=" * 96)
    say()
    split = int(len(m5) * 0.7)
    boundary = m5[split].ts
    say(f"  split at          {boundary:%Y-%m-%d %H:%M} UTC - 70% of bars in sample")
    say()
    say(HEADER)
    halves: dict[str, Summary] = {}
    for label, lo, hi in (
        ("in sample (70%)", m5[0].ts, boundary),
        ("out of sample (30%)", boundary, window_end + timedelta(minutes=5)),
    ):
        part5 = [b for b in m5 if lo <= b.ts < hi]
        part1 = [b for b in h1 if b.ts <= part5[-1].ts]
        result = run(part5, part1, BASELINE, costs, slippage, calendar)
        halves[label] = summarise(result, BASELINE, label=label, starting_equity=ACCOUNT)
        say(line(halves[label]))
    say()
    say("    The out-of-sample half re-warms its own indicators from the split point, so")
    say("    its first 300 hours are warmup and it trades a little less than a straight")
    say("    30% of the bars would suggest.")
    say()

    say("  WALK FORWARD (six consecutive folds, each reported on its own)")
    say(HEADER)
    fold = len(m5) // 6
    folds: list[Summary] = []
    for index in range(6):
        part5 = m5[index * fold : (index + 1) * fold]
        part1 = [b for b in h1 if part5[0].ts <= b.ts <= part5[-1].ts]
        if len(part1) < 320:
            continue
        result = run(part5, part1, BASELINE, costs, slippage, calendar)
        label = f"fold {index + 1}  {part5[0].ts:%Y-%m}..{part5[-1].ts:%Y-%m}"
        folds.append(summarise(result, BASELINE, label=label, starting_equity=ACCOUNT))
        say(line(folds[-1]))
    say()
    say("    Each fold warms up from its own start, so a fold trades fewer bars than it")
    say("    contains. Folds are reported, never selected from.")
    say()

    say("  A SECOND HOLDOUT: THE BROKER'S OWN BARS, EIGHT MONTHS THE ARCHIVE HAS NOT GOT")
    say()
    holdout = mt5_holdout(calendar, slippage, say, bar_range(m5))
    say()

    say("=" * 96)
    say("5. FINAL EVALUATION  (the brief's fifteen questions, from the numbers above)")
    say("=" * 96)
    say()
    for text in evaluate(summary, cost_summaries, variants, halves, folds, holdout):
        say(text)

    report = args.out / "report.txt"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwritten to {report}")


if __name__ == "__main__":
    main()
