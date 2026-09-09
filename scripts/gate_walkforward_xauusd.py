"""The walk-forward gate: does this strategy earn an expert being written for it?

`cfd_walkforward.py` has held the machinery since D-131 and has been driven with
real bars essentially once - where it immediately showed the project's best
strategy losing to never touching its parameter. Meanwhile D-138 built an expert
in a regime the measurements already said loses, and D-142 shipped a positive
in-sample cell that turned negative on the next two windows tried.
`backtest-history.md` ranks fixing that process above every strategy question,
so this exists: a **verdict**, with an exit code, that runs before an `.mq5`
does.

## The four criteria, and why each one

Everything here is read off `WalkForwardReport`. Nothing is re-derived, and no
threshold was chosen after seeing a result.

1. **Can this run support a conclusion at all?** `Feasibility.confidence` must
   be ADEQUATE - `MIN_OOS_TRADES` (30) out-of-sample trades across at least
   three windows. Below that the answer is INCONCLUSIVE, which is neither a pass
   nor a fail: it means run it on more history, not "try again with different
   parameters".

2. **Is out-of-sample P&L positive?** Taking the better of the optimised path
   and the fixed-parameter baseline, because if leaving the parameters alone
   wins then leaving them alone is the strategy. Negative fails.

3. **Does it beat buy-and-hold over the same out-of-sample days?** This is the
   bar `backtest-history.md` pattern 4 sets, and the one the project's best
   result has never cleared: `TrendlineBreakout` H1's +$162,298 was earned
   against +$199,700 of doing nothing. Buy-and-hold is measured over the
   out-of-sample stretches only - the days the strategy was actually being
   judged on - never the whole history, which would compare against a period the
   walk-forward never scored.

4. **If the optimised path is the one relied on, is the parameter stable?** An
   UNSTABLE verdict means the chosen value alternates window to window, which is
   the D-131 signature. When the baseline is what passes, stability is
   irrelevant and is reported rather than enforced - nothing was optimised.

## What a PASS is not

It is not evidence the strategy makes money. It is the absence of the four
specific ways this project has already watched a result evaporate. A pass means
an expert is worth the effort of writing; it does not mean one is worth running
with money.

## The D-149 precondition

A strategy carrying incremental indicator state *and* unbounded holding time -
no flat stop - must clear `measure_window_sensitivity_xauusd.py` first, because
its result may be a property of where the window starts rather than of the
strategy. This gate detects that combination and refuses rather than quietly
scoring a number that a seven-day shift could invert.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from algo.backtest.cfd_walkforward import run_cfd_walk_forward
from algo.backtest.walkforward import Stability, WalkForwardReport
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_history import fetch_history, resolve_server_offset

SYMBOL = "XAUUSD"
LOTS = 100
BARS_PER_REQUEST = 50_000

TIMEFRAMES: dict[str, Timeframe] = {
    "M15": Timeframe(minutes=15),
    "M30": Timeframe(minutes=30),
    "H1": Timeframe(minutes=60),
}

#: Strategies that carry running indicator state, so the D-149 precondition
#: applies to them when they are run without a flat stop. `TrendlineBreakout`'s
#: Donchian channel is stateless and has forgotten the window start after
#: `lookback` bars, which is why it is not listed.
STATEFUL = frozenset({"macd"})

EXIT_PASS, EXIT_FAIL, EXIT_INCONCLUSIVE, EXIT_REFUSED = 0, 1, 2, 3


def _money(value: Decimal) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.0f}"


def buy_and_hold_out_of_sample(bars: list[Bar], report: WalkForwardReport) -> Decimal:
    """Doing nothing, over the out-of-sample stretches only.

    Summed per window rather than measured end to end: the out-of-sample
    windows are the only days the strategy was scored on, and the in-sample
    stretches between them are days it was being fitted to. Including those
    would compare the strategy against a period nobody validated it over.
    """
    total = Decimal("0")
    for result in report.results:
        window = result.window
        inside = [
            b
            for b in bars
            if window.out_of_sample_start <= b.ts.date() < window.out_of_sample_end
        ]
        if len(inside) >= 2:
            total += (inside[-1].close - inside[0].close) * Decimal(LOTS)
    return total


def verdict(
    report: WalkForwardReport, benchmark: Decimal, *, relied_on_optimiser: bool
) -> tuple[str, list[str]]:
    """PASS / FAIL / INCONCLUSIVE, and the reason for each criterion."""
    lines: list[str] = []
    optimised = report.out_of_sample_net_pnl
    baseline = report.baseline_net_pnl
    best = optimised if baseline is None else max(optimised, baseline)

    if not report.feasibility.supports_a_conclusion:
        lines.append(f"1. feasibility     INCONCLUSIVE  {report.feasibility.message}")
        return "INCONCLUSIVE", lines
    lines.append(
        f"1. feasibility     ok            {report.feasibility.oos_trades} OOS trades "
        f"across {report.feasibility.windows} windows"
    )

    failed = False
    if best > 0:
        lines.append(f"2. OOS positive    ok            best OOS {_money(best)}")
    else:
        failed = True
        lines.append(f"2. OOS positive    FAIL          best OOS {_money(best)}")

    if best > benchmark:
        lines.append(
            f"3. beats buy&hold  ok            {_money(best)} vs {_money(benchmark)}"
        )
    else:
        failed = True
        lines.append(
            f"3. beats buy&hold  FAIL          {_money(best)} vs {_money(benchmark)} "
            "over the same out-of-sample days"
        )

    unstable = [s.name for s in report.stability if s.verdict is Stability.UNSTABLE]
    if not relied_on_optimiser:
        lines.append(
            "4. stability       n/a           the fixed baseline is what passes; "
            "nothing was optimised"
        )
    elif unstable:
        failed = True
        lines.append(f"4. stability       FAIL          UNSTABLE: {', '.join(unstable)}")
    else:
        lines.append("4. stability       ok            no parameter is UNSTABLE")

    return ("FAIL" if failed else "PASS"), lines


def main() -> None:
    parser = argparse.ArgumentParser(description="walk-forward gate, D-131 / item 3")
    parser.add_argument("--strategy", default="breakout", choices=("macd", "breakout"))
    parser.add_argument("--timeframe", default="H1", choices=list(TIMEFRAMES))
    parser.add_argument("--stop-loss-pct", default="0.5")
    parser.add_argument("--in-sample-days", type=int, default=180)
    parser.add_argument("--out-of-sample-days", type=int, default=90)
    parser.add_argument(
        "--lookback-grid",
        default="10,20,40,80",
        help="channel lengths to optimise over (breakout only)",
    )
    args = parser.parse_args()

    stop = Decimal(args.stop_loss_pct)
    if args.strategy in STATEFUL and stop == 0:
        print(
            f"REFUSED: {args.strategy} carries incremental indicator state and this run "
            "has no flat stop.\nThat combination is the one D-149 found unstable to the "
            "window's start date - a\nseven-day shift moved it by more than its own "
            "result. Clear\nmeasure_window_sensitivity_xauusd.py first, or pass a "
            "non-zero --stop-loss-pct."
        )
        raise SystemExit(EXIT_REFUSED)

    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select(SYMBOL, True):
        mt5.shutdown()
        raise SystemExit(f"could not select {SYMBOL}: {mt5.last_error()}")
    offset = resolve_server_offset(mt5, SYMBOL).offset
    timeframe = TIMEFRAMES[args.timeframe]
    bars = fetch_history(
        mt5, symbol=SYMBOL, timeframe=timeframe, count=BARS_PER_REQUEST, offset=offset
    )
    mt5.shutdown()

    base = {
        "stop_loss_pct": str(stop),
        "trail_activation_pct": "0",
        "trail_pct": "0",
        "lookback": "20",
    }
    axes = (
        {"lookback": args.lookback_grid.split(",")}
        if args.strategy == "breakout"
        else {"stop_loss_pct": ["0.25", "0.5", "1.0"]}
    )

    report = run_cfd_walk_forward(
        bars,
        strategy=args.strategy,
        instrument=CfdId(symbol=SYMBOL),
        timeframe=timeframe,
        axes=axes,
        base=base,
        lots=LOTS,
        in_sample_days=args.in_sample_days,
        out_of_sample_days=args.out_of_sample_days,
    )

    optimised = report.out_of_sample_net_pnl
    baseline = report.baseline_net_pnl
    relied_on_optimiser = baseline is None or optimised >= baseline
    benchmark = buy_and_hold_out_of_sample(bars, report)

    print(f"walk-forward gate  {SYMBOL} {args.timeframe} {args.strategy}")
    print(f"  history          {bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d}, {len(bars):,} bars")
    print(f"  windows          {args.in_sample_days} in / {args.out_of_sample_days} out")
    print(f"  grid             {axes}")
    print(f"  base             stop {stop}%, no trail")
    print()
    print(f"  in sample (fitted)          {_money(report.in_sample_net_pnl):>14}")
    print(f"  out of sample               {_money(optimised):>14}")
    if baseline is not None:
        print(f"  fixed params, out of sample {_money(baseline):>14}")
        print(f"  optimising beat doing nothing: {report.optimisation_beat_doing_nothing}")
    print(f"  buy & hold, OOS days only   {_money(benchmark):>14}")
    print()
    for parameter in report.stability:
        print(f"  {parameter.describe()}")
    print()

    result, reasons = verdict(report, benchmark, relied_on_optimiser=relied_on_optimiser)
    for line in reasons:
        print(f"  {line}")
    print()
    print(f"  VERDICT: {result}")
    if result == "PASS":
        print("  A pass is the absence of four known failure modes, not evidence of profit.")
        raise SystemExit(EXIT_PASS)
    if result == "INCONCLUSIVE":
        print("  Run it on more history. This is not a reason to change parameters.")
        raise SystemExit(EXIT_INCONCLUSIVE)
    raise SystemExit(EXIT_FAIL)


if __name__ == "__main__":
    main()
