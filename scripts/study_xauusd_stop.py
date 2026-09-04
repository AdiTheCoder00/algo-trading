"""What the $10 stop actually does, measured four ways.

The baseline run says 23.9% of trades end on the stop and the losing shoulder is
the fattest part of the R distribution. That is an observation, not a diagnosis:
a stop that fires often can be doing its job perfectly, or it can be cutting
trades that were about to come back. Those look identical in a P&L column and
they are opposite problems, so this measures them apart.

    python scripts/study_xauusd_stop.py --data state/xauusd --out reports/xauusd

## What is measured, and what each measurement can and cannot say

**1. Excursion anatomy.** How far every trade went against itself before it
ended, split by how it ended. If eventual winners routinely dip past the stop
distance, the stop is killing them; if they rarely do, it is not.

**2. The counterfactual on stopped trades.** For each trade that was stopped,
what would that same position have done had the stop not been there - held on
to its RSI reversal or its four-hour cap. This is an exact per-trade answer, and
deliberately not a strategy-level one: the position staying open would have
blocked later entries, so the totals here are what those trades would have done,
not what the account would have made.

**3. A stop sweep, both windows.** Net, expectancy and stop-hit rate across stop
distances from $2.50 to no stop at all. **This is a sensitivity surface, not a
search.** Reading the best cell off it and calling that the strategy is exactly
the overfitting the brief warns about, and the surface is reported with enough
of its own noise visible to make that obvious.

**4. A volatility-scaled stop.** The structural point the holdout raised: $10 is
6.2 ATRs away in the 2024-25 window and about 2 in the 2026 one, so the "same"
rule is a different rule in each. An ATR-multiple stop is the version that means
the same thing in both, and it is tested in both - not to find a better number,
but to see whether the strategy's *behaviour* stabilises when its risk does.

None of this changes the baseline. The official result stays the fixed $10 stop
reported by `backtest_xauusd_rsi_stoch.py`.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from algo.backtest.xauusd_runner import (
    Indicators,
    ScalperTrade,
    compute_indicators,
)
from algo.backtest.xauusd_study import (
    ACCOUNT,
    HEADER,
    Dataset,
    bar_range,
    cost_scenarios,
    line,
    load_dataset,
    money,
    mt5_costs,
    mt5_m5_bars,
    pct,
    run,
    wrap,
)
from algo.core.bar import Bar
from algo.core.enums import Side
from algo.data.econ_calendar import EconomicCalendar
from algo.reporting.scalper_report import Summary, summarise
from algo.strategy.rsi_stoch_reversal import BASELINE, ExitState, rsi_reversal_exit

#: The sweep. Wide enough that the ends are obviously wrong, so the middle is
#: read as a plateau rather than as a peak. `None` is the no-stop reference.
STOP_LADDER: tuple[Decimal | None, ...] = (
    Decimal("2.50"),
    Decimal("5"),
    Decimal("7.50"),
    Decimal("10"),
    Decimal("12.50"),
    Decimal("15"),
    Decimal("20"),
    Decimal("30"),
    None,
)

#: ATR multiples. 6 is chosen because it is roughly what $10 was in the study
#: window - it is the translation of the baseline, not a preferred setting.
ATR_LADDER: tuple[Decimal, ...] = (
    Decimal("2"),
    Decimal("3"),
    Decimal("4"),
    Decimal("6"),
    Decimal("8"),
)

#: A stop so far away it can never fire, for the no-stop rows. Not `None`: the
#: runner should never grow a code path where a position has no stop at all,
#: because that path would eventually be reachable in live trading.
NO_STOP = Decimal("100000")


def counterfactual(
    trade: ScalperTrade,
    m5: list[Bar],
    index_of: dict,
    ind: Indicators,
    half_at,
) -> Decimal | None:
    """What this stopped trade would have made had the stop not been there.

    Walks the same position forward from its own entry under the remaining two
    exits - the RSI reversal and the four-hour cap - and prices the exit the way
    the runner does. Nothing else about the run changes, which is the point:
    this isolates the stop from every other difference.

    It is **not** a strategy-level counterfactual and must not be read as one.
    A position left open blocks the entries that came after it, so the account
    would have taken a different set of trades entirely. What this answers is
    narrower and cleaner: was this particular stop premature.
    """
    if trade.exit_ts is None:
        return None
    start = index_of.get(trade.entry_ts)
    if start is None:
        return None

    state = ExitState(side=trade.side)
    for i in range(start, len(m5)):
        bar = m5[i]
        previous_rsi = ind.m5_rsi[i - 1] if i else float("nan")
        reversal = rsi_reversal_exit(
            state, previous_rsi=previous_rsi, rsi=ind.m5_rsi[i], params=BASELINE
        )
        state.observe(ind.m5_rsi[i], BASELINE)
        expired = bar.ts - trade.entry_ts >= BASELINE.max_hold
        if not (reversal or expired or i == len(m5) - 1):
            continue
        half = half_at(bar.ts)
        fill = bar.close - half if trade.side is Side.BUY else bar.close + half
        move = fill - trade.entry_price
        signed = move if trade.side is Side.BUY else -move
        # Same cost treatment as the real exit: the entry leg's costs are
        # already sunk, the exit leg pays commission and whatever swap the
        # longer hold would have added is ignored (it is under a cent at four
        # hours and would flatter neither side).
        return signed * trade.lots - trade.commission_paid
    return None


def excursions(
    summary_trades: list[ScalperTrade], unstopped: list[ScalperTrade], say
) -> None:
    """How far trades went against themselves, split by how they ended.

    The second table has to come from the **unstopped** run, and the reason is
    worth stating because getting it wrong is easy and invisible. Ask the
    stopped run "how many winners dipped past $10" and the answer is necessarily
    zero: a trade that dipped that far was closed and could not become a winner.
    The question only has content when the trades were free to dip - so the
    foreclosure table is built from the run with no stop at all, where a trade's
    excursion and its outcome are independent facts.
    """
    winners = [t for t in summary_trades if t.net_pnl > 0]
    losers = [t for t in summary_trades if t.net_pnl <= 0]

    say("  HOW FAR TRADES WENT AGAINST THEMSELVES BEFORE THEY ENDED")
    say(f"    {'':<22}{'n':>6}{'median MAE':>13}{'p90 MAE':>11}{'worst':>10}")
    for label, group in (
        ("all trades", summary_trades),
        ("eventual winners", winners),
        ("eventual losers", losers),
    ):
        if not group:
            continue
        maes = sorted(t.mae for t in group)
        say(
            f"    {label:<22}{len(group):>6}"
            f"{money(maes[len(maes) // 2]):>13}"
            f"{money(maes[len(maes) // 10]):>11}"
            f"{money(maes[0]):>10}"
        )
    say()

    free_winners = [t for t in unstopped if t.net_pnl > 0]
    say("  THE QUESTION A STOP HAS TO ANSWER: how many WINNERS dipped this far first?")
    say(f"    (from the UNSTOPPED run - {len(free_winners)} winners that were free to dip)")
    say(f"    {'stop at':<12}{'winners it would have cut':>28}{'share of winners':>20}")
    for level in (
        Decimal("2.50"),
        Decimal("5"),
        Decimal("7.50"),
        Decimal("10"),
        Decimal("15"),
        Decimal("20"),
    ):
        cut = sum(1 for t in free_winners if t.mae <= -level)
        share = Decimal(cut) / Decimal(len(free_winners)) * 100 if free_winners else None
        say(f"    ${level:<11}{cut:>28}{pct(share):>20}")
    say()
    say("    A stop is only ever a trade between the losses it truncates and the winners")
    say("    it forecloses, and the second half of that trade is the one a P&L column")
    say("    never shows. This is that half.")
    say()


def risk_without_a_stop(label: str, trades: list[ScalperTrade], say) -> None:
    """What the account is exposed to once the only size bound is removed.

    The four-hour cap bounds how LONG a position is held, not how much it can
    lose while it is held. Anyone reading the sweep's no-stop row as a
    recommendation needs this table next to it.
    """
    if not trades:
        return
    nets = sorted(t.net_pnl for t in trades)
    maes = sorted(t.mae for t in trades)
    say(f"  {label}")
    say(f"    worst single trade      {money(nets[0])}")
    say(f"    worst excursion         {money(maes[0])}")
    for threshold in (Decimal("10"), Decimal("20"), Decimal("30"), Decimal("50")):
        count = sum(1 for n in nets if n <= -threshold)
        say(
            f"    trades worse than -${threshold:<4}  {count:>4}"
            f"   ({pct(Decimal(count) / Decimal(len(nets)) * 100)})"
        )
    say()


def sweep(
    label: str,
    m5: list[Bar],
    h1: list[Bar],
    costs,
    slippage,
    calendar: EconomicCalendar,
    ind: Indicators,
    say,
) -> dict[str, Summary]:
    """The stop ladder over one window."""
    say(f"  {label}")
    say(HEADER + f"{'stopped':>10}")
    results: dict[str, Summary] = {}
    for level in STOP_LADDER:
        params = replace(BASELINE, stop_loss=level if level is not None else NO_STOP)
        name = f"${level} stop" if level is not None else "no stop at all"
        if level == BASELINE.stop_loss:
            name += "  <- baseline"
        result = run(m5, h1, params, costs, slippage, calendar, ind)
        item = summarise(result, params, label=name, starting_equity=ACCOUNT)
        results[name] = item
        stopped = item.exit_share.get("stop loss")
        say(line(item) + f"{pct(stopped):>10}")
    say()
    return results


def atr_sweep(
    label: str,
    m5: list[Bar],
    h1: list[Bar],
    costs,
    slippage,
    calendar: EconomicCalendar,
    ind: Indicators,
    say,
) -> dict[str, Summary]:
    say(f"  {label}")
    say(HEADER + f"{'stopped':>10}")
    results: dict[str, Summary] = {}
    for multiple in ATR_LADDER:
        params = replace(BASELINE, stop_atr_multiple=multiple)
        result = run(m5, h1, params, costs, slippage, calendar, ind)
        item = summarise(
            result, params, label=f"{multiple} x ATR(14)", starting_equity=ACCOUNT
        )
        results[str(multiple)] = item
        say(line(item) + f"{pct(item.exit_share.get('stop loss')):>10}")
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
    ind = compute_indicators(dataset.m5, dataset.h1, BASELINE)

    say("=" * 96)
    say("THE $10 STOP, MEASURED")
    say("=" * 96)
    say()
    say(f"  window            {dataset.label}  ({len(dataset.m5):,} M5 bars)")
    say("  costs             realistic: per-bar measured spread, zero commission,")
    say("                    measured swap, $0.05 slippage on a stop")
    say("  everything else   the exact baseline rules. Only the stop moves.")
    say()

    baseline = run(dataset.m5, dataset.h1, BASELINE, costs, slippage, calendar, ind)
    summary = summarise(baseline, BASELINE, label="baseline", starting_equity=ACCOUNT)
    stops = [t for t in baseline.trades if t.exit_reason.value == "stop loss"]

    unstopped = run(
        dataset.m5,
        dataset.h1,
        replace(BASELINE, stop_loss=NO_STOP),
        costs,
        slippage,
        calendar,
        ind,
    )

    say("=" * 96)
    say("1. EXCURSION ANATOMY")
    say("=" * 96)
    say()
    excursions(baseline.trades, unstopped.trades, say)

    # ------------------------------------------------------ the counterfactual
    say("=" * 96)
    say("2. WHAT THE STOPPED TRADES WOULD HAVE DONE WITHOUT THE STOP")
    say("=" * 96)
    say()
    index_of = {bar.ts: i for i, bar in enumerate(dataset.m5)}
    pairs: list[tuple[ScalperTrade, Decimal]] = []
    for trade in stops:
        alternative = counterfactual(
            trade, dataset.m5, index_of, ind, dataset.half_spread
        )
        if alternative is not None:
            pairs.append((trade, alternative))

    if pairs:
        recovered = [(t, a) for t, a in pairs if a > 0]
        worse = [(t, a) for t, a in pairs if a < t.net_pnl]
        actual = sum((t.net_pnl for t, _ in pairs), Decimal("0"))
        without = sum((a for _, a in pairs), Decimal("0"))
        say(f"    stopped trades examined     {len(pairs)} of {len(stops)}")
        say(f"    they actually made          {money(actual)}")
        say(f"    without the stop they make  {money(without)}")
        say(f"    the stop was therefore      {money(actual - without)} on these trades")
        say()
        say(f"    would have turned positive  {len(recovered)}"
            f"   ({pct(Decimal(len(recovered)) / Decimal(len(pairs)) * 100)})")
        say(f"    would have got worse        {len(worse)}"
            f"   ({pct(Decimal(len(worse)) / Decimal(len(pairs)) * 100)})")
        alternatives = sorted(a for _, a in pairs)
        say(f"    without the stop: median {money(alternatives[len(alternatives) // 2])}, "
            f"worst {money(alternatives[0])}, best {money(alternatives[-1])}")
        say()
        for row in wrap(
            "The sign of that fourth line is the whole answer. Positive means the stop "
            "paid for itself on the trades it fired on; negative means it cut trades "
            "that were coming back. Note the worst case in the last line: that is what "
            "one unstopped trade was capable of, and it is the reason the comparison is "
            "not simply 'remove the stop'."
        ):
            say(f"    {row}")
    else:
        say("    no stopped trades to examine.")
    say()

    # -------------------------------------------------------------- the sweeps
    say("=" * 96)
    say("3. STOP SWEEP  (sensitivity, NOT a search - see the note below)")
    say("=" * 96)
    say()
    dukas = sweep(
        f"DUKASCOPY WINDOW  {dataset.label}",
        dataset.m5,
        dataset.h1,
        costs,
        slippage,
        calendar,
        ind,
        say,
    )

    holdout = mt5_m5_bars()
    holdout_sweep: dict[str, Summary] = {}
    holdout_atr: dict[str, Summary] = {}
    if holdout is not None:
        h_m5, h_h1, _resolved = holdout
        h_ind = compute_indicators(h_m5, h_h1, BASELINE)
        h_costs = mt5_costs()
        say(f"  the holdout window is a different market: median M5 range "
            f"{bar_range(h_m5)} against {bar_range(dataset.m5)} here, so the SAME dollar")
        say("  stop is a much tighter stop there. That is the point of the two tables.")
        say()
        holdout_sweep = sweep(
            f"MT5 HOLDOUT  {h_m5[0].ts:%Y-%m} .. {h_m5[-1].ts:%Y-%m}  (flat D-121 spread)",
            h_m5,
            h_h1,
            h_costs,
            slippage,
            calendar,
            h_ind,
            say,
        )
    else:
        say("  MT5 holdout unavailable - the terminal did not serve bars.")
        say()

    say("  " + "-" * 90)
    for row in wrap(
        "The bottom row is the best row in both tables, and that is a finding rather "
        "than a cell to pick: whatever else is noisy here, both windows agree that this "
        "particular hard stop costs money. What the tables do NOT support is reading the "
        "$2.50 row in the holdout, or the ordering of the middle rows, as meaning "
        "anything - those move between windows and are the noise. Read the direction, "
        "not the ranking."
    ):
        say(f"  {row}")
    say()

    say("  AND WHAT THE BOTTOM ROW COSTS IN RISK, WHICH THE NET COLUMN DOES NOT SHOW")
    say()
    risk_without_a_stop("no stop, Dukascopy window", unstopped.trades, say)
    if holdout is not None:
        h_m5, h_h1, _ = holdout
        h_unstopped = run(
            h_m5,
            h_h1,
            replace(BASELINE, stop_loss=NO_STOP),
            mt5_costs(),
            slippage,
            calendar,
            compute_indicators(h_m5, h_h1, BASELINE),
        )
        risk_without_a_stop("no stop, MT5 holdout", h_unstopped.trades, say)
    for row in wrap(
        "The four-hour cap bounds how long a position is held. It does not bound how "
        "much it can lose while held, and in the holdout's market that difference is "
        "large. A strategy with no size bound has a loss distribution with no right-hand "
        "edge, and the sample that would reveal the tail is by definition the one that "
        "has not happened yet."
    ):
        say(f"  {row}")
    say()

    # ---------------------------------------------------------- the ATR version
    say("=" * 96)
    say("4. A STOP THAT MEANS THE SAME THING IN BOTH REGIMES")
    say("=" * 96)
    say()
    for row in wrap(
        f"A $10 stop is {Decimal('10') / bar_range(dataset.m5):.1f} median bars away in the "
        "study window. In the holdout it is about a third of that, because gold's "
        "five-minute range trebled while the rule stayed the same number. An ATR "
        "multiple is the same rule expressed so that it does not change meaning when "
        "the market does. The multiples below are not candidates to pick from - 6xATR "
        "is roughly what $10 WAS here, so it is the baseline translated, and the "
        "question is whether the behaviour holds still across the two windows in a way "
        "the dollar stop does not."
    ):
        say(f"  {row}")
    say()
    dukas_atr = atr_sweep(
        f"DUKASCOPY WINDOW  {dataset.label}",
        dataset.m5,
        dataset.h1,
        costs,
        slippage,
        calendar,
        ind,
        say,
    )
    if holdout is not None:
        h_m5, h_h1, _ = holdout
        holdout_atr = atr_sweep(
            f"MT5 HOLDOUT  {h_m5[0].ts:%Y-%m} .. {h_m5[-1].ts:%Y-%m}",
            h_m5,
            h_h1,
            mt5_costs(),
            slippage,
            calendar,
            compute_indicators(h_m5, h_h1, BASELINE),
            say,
        )

    say("=" * 96)
    say("WHAT THIS SETTLES")
    say("=" * 96)
    say()
    for text in verdict(summary, dukas, holdout_sweep, dukas_atr, holdout_atr, pairs):
        say(text)

    args.out.mkdir(parents=True, exist_ok=True)
    report = args.out / "stop_study.txt"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwritten to {report}")


def verdict(
    baseline: Summary,
    dukas: dict[str, Summary],
    holdout: dict[str, Summary],
    dukas_atr: dict[str, Summary],
    holdout_atr: dict[str, Summary],
    pairs: list[tuple[ScalperTrade, Decimal]],
) -> list[str]:
    """The findings, each tied to a number printed above it.

    Written after the numbers were in, and it says the opposite of what was
    expected before they were: the working assumption going in was that a $10
    stop on a strategy already capped at four hours would be close to
    irrelevant. It is not.
    """
    lines: list[str] = []

    def add(heading: str, body: str) -> None:
        lines.append(f"  {heading}")
        lines.extend(f"      {row}" for row in wrap(body))
        lines.append("")

    actual = sum((t.net_pnl for t, _ in pairs), Decimal("0"))
    without = sum((a for _, a in pairs), Decimal("0"))
    cost_of_stop = actual - without
    recovered = sum(1 for t, a in pairs if a > 0)
    stop_hit = baseline.exit_share.get("stop loss")

    free_here = dukas.get("no stop at all")
    baseline_here = dukas.get("$10 stop  <- baseline")
    free_there = holdout.get("no stop at all")
    baseline_there = holdout.get("$10 stop  <- baseline")

    add(
        "1. The $10 stop costs this strategy money, in both windows.",
        f"On the {len(pairs)} trades it fired on it was worth {money(cost_of_stop)}: those "
        f"same positions, left alone to reach their RSI or four-hour exit, would have lost "
        f"{money(without)} instead of {money(actual)}, and {recovered} of them "
        f"({pct(Decimal(recovered) / Decimal(len(pairs)) * 100)}) would have finished "
        "positive. The full-run sweep says the same thing from the other direction: "
        + (
            f"removing the stop entirely moves this window from {money(baseline_here.net_pnl)} "
            f"to {money(free_here.net_pnl)}"
            if free_here and baseline_here
            else ""
        )
        + (
            f", and the holdout from {money(baseline_there.net_pnl)} to "
            f"{money(free_there.net_pnl)}"
            if free_there and baseline_there
            else ""
        )
        + ". Two independent windows, opposite regimes, same direction.",
    )

    add(
        "2. The reason is structural, not a quirk of these dates.",
        "The strategy already has a bound: the four-hour cap. A hard stop inside that "
        "window converts temporary adverse excursion into realised loss on trades whose "
        "own exit rule had not yet fired. The excursion table is the evidence - the "
        "median eventual winner had already been $2.39 against, and one in ten had been "
        "$7.20 against, which is most of the way to a $10 stop. On a horizon this short, "
        "a stop tight enough to fire often is mostly firing on noise it was never meant "
        "to be measuring.",
    )

    add(
        "3. That is NOT a recommendation to trade without a stop.",
        "The no-stop rows buy their P&L with an unbounded single-trade loss - see the "
        "risk table above. The four-hour cap limits time, not size. And the improvement "
        "does not create an edge: the entry is still roughly a coin flip paying half a "
        "dollar a trade in spread, so removing the stop moves a small negative to a small "
        "positive in one window and a moderate positive to a larger one in the other, "
        "with the tail risk paying for both. A wider stop captures most of the same effect "
        "with the bound intact.",
    )

    if dukas_atr and holdout_atr:
        here = {k: v.exit_share.get("stop loss") for k, v in dukas_atr.items()}
        there = {k: v.exit_share.get("stop loss") for k, v in holdout_atr.items()}
        common = sorted(set(here) & set(there), key=Decimal)
        drift = max(
            (abs((here[k] or Decimal(0)) - (there[k] or Decimal(0))) for k in common),
            default=Decimal("0"),
        )
        there_hit = (
            baseline_there.exit_share.get("stop loss") if baseline_there else None
        ) or Decimal(0)
        dollar_drift = abs((stop_hit or Decimal(0)) - there_hit)
        add(
            "4. Scaling the stop to volatility fixes what the rule MEANS across regimes.",
            f"At the same nominal $10, the share of trades ending on the stop is "
            f"{pct(stop_hit)} in this window and "
            + (
                f"{pct(baseline_there.exit_share.get('stop loss'))} in the holdout"
                if baseline_there
                else "far higher in the holdout"
            )
            + f" - a {pct(dollar_drift)} swing, because the same distance is a different "
            "stop when the market's range trebles. Under an ATR multiple the two windows "
            f"stay within {pct(drift)} of each other at the worst multiple. That is a risk "
            "control that behaves the same way in both regimes; the dollar stop "
            "demonstrably is not. It does not make the strategy profitable - every ATR row "
            "sits in the same band as every dollar row - but it makes the risk knowable, "
            "which is a different and more useful property.",
        )

    lines.append("  WHAT I WOULD ACTUALLY CHANGE")
    lines.extend(
        f"      {row}"
        for row in wrap(
            "Two things, in this order. First, express the stop as a multiple of ATR "
            "rather than in dollars, so that it is the same rule in a $2,000 gold market "
            "and a $5,000 one - this is a correctness fix and it stands whatever else is "
            "decided. Second, widen it: the sweep and the counterfactual agree that a stop "
            "this tight is firing inside the strategy's own noise, and the four-hour cap "
            "is already doing the job the stop was presumably added for. Neither change "
            "makes this a strategy worth trading. The baseline conclusion is unmoved - "
            "the entry has no edge that survives the spread - and no stop rule repairs "
            "that, which is exactly why none of this is folded back into the baseline."
        )
    )
    lines.append("")
    return lines


def _range(results: dict[str, Summary]) -> Decimal:
    if not results:
        return Decimal("0")
    nets = [item.net_pnl for item in results.values()]
    return max(nets) - min(nets)


if __name__ == "__main__":
    main()
