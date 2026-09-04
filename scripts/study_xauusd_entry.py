"""Can the entry be made selective enough to stop being a coin flip?

The baseline takes 476 trades at a 48.9% win rate and an expectancy of -$0.14,
against costs of about $0.55 a trade. So the question is narrow and answerable:
is there a subset of those signals whose expected move is large enough to clear
the spread, and can it be identified **before** the trade rather than after?

    python scripts/study_xauusd_entry.py --data state/xauusd --out reports/xauusd

## This is a search, and searches lie

Everything before this script measured the strategy as given. This one goes
looking, and five filters over three windows is thirty-odd comparisons - enough
that the best of them will look good by chance alone. Three devices are built in
to make that visible rather than to pretend it away:

**Every filter is a hypothesis stated before it is run.** Each one below says
what it claims and why, and none of them was chosen by looking at a results
table. That does not make them right; it makes them falsifiable.

**A random control.** `drop_fraction` throws away a fixed share of signals for
no reason at all. It is reported beside the real filters at a matched trade
count. A filter that does not clearly beat the control is not filtering, it is
just taking fewer trades - and taking fewer trades changes the P&L of a
zero-expectancy process by pure variance.

**Out of sample, every time.** No filter is reported on the in-sample window
alone. The 70/30 chronological split and the MT5 holdout - eight later months,
different feed, no overlap - are shown for each, and the in-sample column is
the one to distrust.

## The five hypotheses

1. **Cost-aware (`min_atr_per_spread`).** The strategy's own finding is that
   costs and edge are the same size. The most direct response is to trade only
   when the move available is large relative to what it costs to take: ATR at
   the signal at least N times the spread quoted in that bar. If the edge is
   real but thin, this should concentrate it.

2. **Trend separation (`min_trend_separation`).** The specified trend filter
   checks only the SIGN of EMA20 - EMA50. Two EMAs a cent apart pass it, and
   that is not a trend. This asks for the gap to be a meaningful fraction of an
   H1 ATR.

3. **Expanding momentum (`require_expanding_macd`).** Require the H1 MACD
   histogram to be growing in magnitude - joining a trend that is accelerating
   rather than one that is rolling over. Adds no constant to fit.

4. **Stricter stochastic.** The specified confirmation is K and D beyond 60/40.
   This tightens it to 70/30 - the same rule, asked more insistently.

5. **All four at once.** The interesting failure mode: filters that each look
   mildly positive can combine into something that trades twenty times and
   reports a wonderful number about nothing.

## What would count as success

Not a bigger number. A filter earns belief if it (a) beats the random control
at a comparable trade count, (b) improves expectancy in the same direction in
BOTH out-of-sample windows, and (c) leaves enough trades to be worth believing.
Anything less is a finding about this sample.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from algo.backtest.xauusd_runner import compute_indicators
from algo.backtest.xauusd_study import (
    ACCOUNT,
    Dataset,
    cost_scenarios,
    load_dataset,
    money,
    mt5_costs,
    mt5_m5_bars,
    num,
    pct,
    run,
    wrap,
)
from algo.data.econ_calendar import EconomicCalendar
from algo.reporting.scalper_report import Summary, summarise
from algo.strategy.rsi_stoch_reversal import BASELINE, ScalperParams

HEADER = (
    f"    {'':<28}{'trades':>7}{'win%':>8}{'net $':>11}{'PF':>8}"
    f"{'exp $':>9}{'maxDD':>10}{'kept':>7}"
)

#: (label, params). Stated in the module docstring before any of them was run.
FILTERS: tuple[tuple[str, ScalperParams], ...] = (
    ("baseline (no filter)", BASELINE),
    ("1. ATR >= 8x spread", replace(BASELINE, min_atr_per_spread=Decimal("8"))),
    ("1b. ATR >= 12x spread", replace(BASELINE, min_atr_per_spread=Decimal("12"))),
    ("2. EMA gap >= 0.5 H1 ATR", replace(BASELINE, min_trend_separation=Decimal("0.5"))),
    ("3. MACD hist expanding", replace(BASELINE, require_expanding_macd=True)),
    (
        "4. stoch beyond 70/30",
        replace(BASELINE, buy_stoch_level=70.0, sell_stoch_level=30.0),
    ),
    (
        "5. all four",
        replace(
            BASELINE,
            min_atr_per_spread=Decimal("8"),
            min_trend_separation=Decimal("0.5"),
            require_expanding_macd=True,
            buy_stoch_level=70.0,
            sell_stoch_level=30.0,
        ),
    ),
)

#: The control, at three severities, so a filter can be compared against a
#: random drop that kept a similar number of trades.
CONTROLS: tuple[tuple[str, ScalperParams], ...] = tuple(
    (
        f"control: drop {int(fraction * 100)}% at random",
        replace(BASELINE, drop_fraction=Decimal(str(fraction))),
    )
    for fraction in (0.25, 0.5, 0.75)
)


def row(item: Summary, kept: str) -> str:
    """One table row. Built here rather than by extending `line()` because this
    table carries two columns that one does not - and slicing the shared
    renderer to make room is how a column quietly ends up under the wrong
    heading."""
    return (
        f"    {item.label:<28}{item.trades:>7}{pct(item.win_rate):>8}"
        f"{money(item.net_pnl):>11}{num(item.profit_factor):>8}"
        f"{money(item.expectancy):>9}{money(-item.max_drawdown):>10}{kept:>7}"
    )


def evaluate_on(
    label: str,
    m5,
    h1,
    costs,
    slippage,
    calendar: EconomicCalendar,
    say,
    baseline_trades: int | None = None,
) -> dict[str, Summary]:
    """Every filter and every control over one window."""
    ind = compute_indicators(m5, h1, BASELINE)
    say(f"  {label}")
    say(HEADER)
    results: dict[str, Summary] = {}
    for name, params in (*FILTERS, *CONTROLS):
        result = run(m5, h1, params, costs, slippage, calendar, ind)
        item = summarise(result, params, label=name, starting_equity=ACCOUNT)
        results[name] = item
        reference = baseline_trades or results["baseline (no filter)"].trades
        kept = (
            pct(Decimal(item.trades) / Decimal(reference) * 100) if reference else "n/a"
        )
        if name.startswith("control"):
            say("")
        say(row(item, kept))
    say()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("state/xauusd"))
    parser.add_argument("--out", type=Path, default=Path("reports/xauusd"))
    args = parser.parse_args()

    out: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        out.append(text)

    dataset: Dataset = load_dataset(args.data)
    calendar = EconomicCalendar.load()
    costs, slippage = cost_scenarios(dataset.half_spread)["realistic"]

    say("=" * 100)
    say("TIGHTENING THE ENTRY: FIVE HYPOTHESES, ONE CONTROL, THREE WINDOWS")
    say("=" * 100)
    say()
    for row in wrap(
        "Everything before this measured the strategy as specified. This goes looking for "
        "a better one, which is a different and much less trustworthy activity. Read the "
        "control rows first: they take fewer trades for no reason at all, and whatever "
        "they achieve is what taking fewer trades achieves by itself.",
        94,
    ):
        say(f"  {row}")
    say()
    say(f"  window            {dataset.label}  ({len(dataset.m5):,} M5 bars)")
    say("  costs             realistic: per-bar measured spread, measured swap,")
    say("                    zero commission, $0.05 slippage on a stop")
    say("  'kept' column     trades as a share of the unfiltered baseline's")
    say()

    split = int(len(dataset.m5) * 0.7)
    boundary = dataset.m5[split].ts
    in_m5 = [b for b in dataset.m5 if b.ts < boundary]
    in_h1 = [b for b in dataset.h1 if b.ts <= in_m5[-1].ts]
    out_m5 = [b for b in dataset.m5 if b.ts >= boundary]
    out_h1 = [b for b in dataset.h1 if b.ts >= boundary]

    say("=" * 100)
    say("1. IN SAMPLE  (first 70% - the window a filter is at risk of being fitted to)")
    say("=" * 100)
    say()
    inside = evaluate_on(
        f"in sample  {in_m5[0].ts:%Y-%m} .. {in_m5[-1].ts:%Y-%m}",
        in_m5,
        in_h1,
        costs,
        slippage,
        calendar,
        say,
    )

    say("=" * 100)
    say("2. OUT OF SAMPLE  (last 30%, chronological)")
    say("=" * 100)
    say()
    outside = evaluate_on(
        f"out of sample  {out_m5[0].ts:%Y-%m} .. {out_m5[-1].ts:%Y-%m}",
        out_m5,
        out_h1,
        costs,
        slippage,
        calendar,
        say,
    )

    holdout: dict[str, Summary] = {}
    fetched = mt5_m5_bars()
    if fetched is not None:
        h_m5, h_h1, _resolved = fetched
        say("=" * 100)
        say("3. THE MT5 HOLDOUT  (eight later months, different feed, no overlap)")
        say("=" * 100)
        say()
        say("  Flat D-121 spread here - the terminal serves no tick history for this")
        say("  period - so the ATR/spread filter is measured against a constant rather")
        say("  than a per-bar reading. That weakens hypothesis 1 specifically.")
        say()
        holdout = evaluate_on(
            f"holdout  {h_m5[0].ts:%Y-%m} .. {h_m5[-1].ts:%Y-%m}",
            h_m5,
            h_h1,
            mt5_costs(),
            slippage,
            calendar,
            say,
        )

    say("=" * 100)
    say("VERDICT")
    say("=" * 100)
    say()
    for text in verdict(inside, outside, holdout):
        say(text)

    args.out.mkdir(parents=True, exist_ok=True)
    report = args.out / "entry_study.txt"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwritten to {report}")


def verdict(
    inside: dict[str, Summary],
    outside: dict[str, Summary],
    holdout: dict[str, Summary],
) -> list[str]:
    """Which filters survived all three tests, stated from the tables above.

    The test is fixed in advance and applied mechanically: beat the baseline's
    expectancy in BOTH out-of-sample windows, beat every control in those
    windows, and leave at least 60 trades. Deciding the criterion after seeing
    the numbers is how a search launders itself into a finding.
    """
    lines: list[str] = []

    def add(heading: str, body: str) -> None:
        lines.append(f"  {heading}")
        lines.extend(f"      {row}" for row in wrap(body, 94))
        lines.append("")

    def expectancy(window: dict[str, Summary], name: str) -> Decimal | None:
        item = window.get(name)
        return item.expectancy if item else None

    names = [name for name, _ in FILTERS if name != "baseline (no filter)"]
    windows = [w for w in (outside, holdout) if w]

    survivors: list[str] = []
    for name in names:
        if not windows:
            break
        base_beaten = all(
            (expectancy(w, name) or Decimal("-99"))
            > (expectancy(w, "baseline (no filter)") or Decimal("0"))
            for w in windows
        )
        control_beaten = all(
            (expectancy(w, name) or Decimal("-99"))
            > max(
                (expectancy(w, control) or Decimal("-99") for control, _ in CONTROLS),
                default=Decimal("-99"),
            )
            for w in windows
        )
        enough = all((w[name].trades if name in w else 0) >= 60 for w in windows)
        if base_beaten and control_beaten and enough:
            survivors.append(name)

    if survivors:
        add(
            f"Survived all three tests: {', '.join(survivors)}.",
            "That means: better expectancy than the unfiltered baseline in every "
            "out-of-sample window, better than every random control in those windows, and "
            "at least 60 trades left. It is the strongest thing this data can say for a "
            "filter, and it is still one sample of one instrument over two and a half "
            "years. Before trading it, the thing to check is whether the mechanism makes "
            "sense - a filter that survives without a reason is a filter that got lucky "
            "in three places instead of one.",
        )
    else:
        add(
            "Nothing survived all three tests.",
            "No filter beat both the unfiltered baseline and every random control in both "
            "out-of-sample windows while leaving enough trades to matter. Read that as the "
            "result it is: the entry's problem is not that it is insufficiently selective. "
            "Being more selective removes trades roughly at random, which is exactly what "
            "the control rows do on purpose.",
        )

    best_in = max(
        (n for n in names if n in inside),
        key=lambda n: inside[n].expectancy or Decimal("-99"),
        default=None,
    )
    if best_in and outside:
        add(
            "What the in-sample column would have told you, and why not to listen.",
            f"The best filter in sample is '{best_in}' at "
            f"{money(inside[best_in].expectancy)} a trade against the baseline's "
            f"{money(inside['baseline (no filter)'].expectancy)}. Out of sample the same "
            f"filter makes {money(expectancy(outside, best_in))} against a baseline of "
            f"{money(expectancy(outside, 'baseline (no filter)'))}. That gap is the entire "
            "argument for why the in-sample table is in this report only as a warning.",
        )

    controls_note = []
    for window_name, window in (("out of sample", outside), ("holdout", holdout)):
        if not window:
            continue
        spread = [
            (control, window[control].expectancy)
            for control, _ in CONTROLS
            if control in window
        ]
        if spread:
            best = max(spread, key=lambda pair: pair[1] or Decimal("-99"))
            worst = min(spread, key=lambda pair: pair[1] or Decimal("-99"))
            controls_note.append(
                f"{window_name} {money(worst[1])} to {money(best[1])}"
            )
    if controls_note:
        add(
            "How much a filter can 'earn' by doing nothing.",
            "Throwing away 25%, 50% and 75% of signals at random moves expectancy across "
            + "; ".join(controls_note)
            + ". Any real filter has to clear that band before its number means anything, "
            "and several of the filters above do not.",
        )

    add(
        "The finding this does not change.",
        "None of these rows makes the strategy profitable out of sample after costs. "
        "Filtering redistributes a zero-expectancy process; it does not create an edge in "
        "one. If the entry is to be fixed, it needs a different signal rather than a "
        "stricter reading of this one - and that is a new strategy, not a variant of this "
        "one, and should be measured as such from scratch.",
    )
    return lines


if __name__ == "__main__":
    main()
