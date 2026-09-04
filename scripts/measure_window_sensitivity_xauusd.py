"""Does the result survive starting the window a few weeks later?

The check behind D-149. It exists because D-148's reproduction column showed
two cells disagreeing with their published figures by more than the figures
themselves, while everything around them reproduced within a few percent - and
the cause turned out to be the window's start date rather than anything in the
harness.

## What it varies, and what it does not

The bars are fetched once and the *end* is fixed. Only the first bar moves,
0 to 28 days later. Every run therefore sees a strict subset of the same
history, ending at the same instant, priced by the same costs. If a result is a
property of the strategy it should barely move; if it is a property of where the
series happened to begin, it will.

## Why it is not run on everything

D-149's 2x2 found three of four combinations stable, and named the two
ingredients an unstable one needs:

- **incremental indicator state**, so the seeding at bar zero perturbs the
  signal - `MacdCrossover` carries three EMAs; `TrendlineBreakout`'s Donchian
  channel is the max and min of the last `lookback` bars and has forgotten the
  start after `lookback` bars
- **unbounded holding time**, so a perturbed entry can land on the wrong side of
  a large trend leg - at 100 ounces a $1,000 move in gold is $100,000

Either alone is stable. Both together moved MACD M15 by $160,000 and flipped
MACD H1's sign. So this is a targeted check, not a blanket one: run it on any
strategy that carries running state *and* can hold indefinitely, and skip it
otherwise.

It is also cheap enough to run before walk-forward rather than after. D-131
caught this class of problem the expensive way.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_history import fetch_history, resolve_server_offset
from algo.live.mt5_runner import strategy_for

SYMBOL = "XAUUSD"
LOTS = 100
BARS_PER_REQUEST = 50_000
ZERO = Decimal("0")

#: How far to push the start, in days. Wide enough to cross several trend legs,
#: short enough that every run still shares almost all of its history - which is
#: what makes a large spread damning rather than expected.
SHIFTS = (0, 3, 7, 14, 21, 28)

#: The stop widths compared. Zero is the ingredient under test (unbounded
#: holding time); 0.5% is the control that removes it.
STOPS = (ZERO, Decimal("0.5"))

TIMEFRAMES: dict[str, Timeframe] = {
    "M15": Timeframe(minutes=15),
    "M30": Timeframe(minutes=30),
    "H1": Timeframe(minutes=60),
}


def fetch(labels: list[str]) -> dict[str, list[Bar]]:
    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select(SYMBOL, True):
        mt5.shutdown()
        raise SystemExit(f"could not select {SYMBOL}: {mt5.last_error()}")
    offset = resolve_server_offset(mt5, SYMBOL).offset
    series = {
        label: fetch_history(
            mt5,
            symbol=SYMBOL,
            timeframe=TIMEFRAMES[label],
            count=BARS_PER_REQUEST,
            offset=offset,
        )
        for label in labels
    }
    mt5.shutdown()
    return series


def net_for(bars: list[Bar], tf: Timeframe, strategy: str, stop: Decimal) -> tuple[int, Decimal]:
    instrument = CfdId(symbol=SYMBOL)
    result = run_cfd_backtest(
        bars,
        instrument=instrument,
        timeframe=tf,
        strategy_factory=lambda: strategy_for(
            strategy,
            instrument=instrument,
            stop_loss_pct=stop,
            trail_activation_pct=ZERO,
            trail_pct=ZERO,
        ),
        stop_loss_pct=stop,
        trail_activation_pct=ZERO,
        trail_pct=ZERO,
        lots=LOTS,
    )
    return len(result.trades), result.net_pnl


def _money(value: Decimal) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.0f}"


def scan(strategy: str, label: str, bars_all: list[Bar]) -> None:
    tf = TIMEFRAMES[label]
    base = bars_all[0].ts
    print()
    print(f"=== {strategy} {label} === {len(bars_all):,} bars ending {bars_all[-1].ts:%Y-%m-%d}")
    header = f"{'start':>8}{'first bar':>13}{'bars':>9}"
    for stop in STOPS:
        tag = "no stop" if stop == ZERO else f"{stop}% stop"
        header += f"{tag + ' net':>17}{'trades':>8}"
    print(header)

    spreads: dict[Decimal, list[Decimal]] = {stop: [] for stop in STOPS}
    for shift in SHIFTS:
        cut = base + timedelta(days=shift)
        bars = [b for b in bars_all if b.ts >= cut]
        row = f"{'+' + str(shift) + 'd':>8}{bars[0].ts:%Y-%m-%d}{len(bars):>9,}"
        for stop in STOPS:
            trades, net = net_for(bars, tf, strategy, stop)
            spreads[stop].append(net)
            row += f"{_money(net):>17}{trades:>8}"
        print(row)

    print(f"{'spread':>8}{'':>13}{'':>9}", end="")
    for stop in STOPS:
        values = spreads[stop]
        width = max(values) - min(values)
        flips = min(values) < 0 < max(values)
        note = "  SIGN FLIP" if flips else ""
        print(f"{_money(width):>17}{note:>8}", end="")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="start-date sensitivity, D-149")
    parser.add_argument("--strategy", default="macd", choices=("macd", "breakout"))
    parser.add_argument(
        "--timeframes", nargs="*", default=["M15", "H1"], choices=list(TIMEFRAMES)
    )
    parser.add_argument(
        "--end",
        default=None,
        help="clamp the window end, YYYY-MM-DD (default: the newest closed bar)",
    )
    args = parser.parse_args()

    series = fetch(args.timeframes)
    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(
            hour=23, minute=59, tzinfo=UTC
        )
        series = {label: [b for b in bars if b.ts <= end] for label, bars in series.items()}

    for label in args.timeframes:
        scan(args.strategy, label, series[label])

    print()
    print("Only the first bar moves; the end and the costs are fixed, so every run")
    print("sees a subset of the same history. A spread of the same order as the result")
    print("means the number describes the start date. See D-149 for the 2x2.")


if __name__ == "__main__":
    main()
