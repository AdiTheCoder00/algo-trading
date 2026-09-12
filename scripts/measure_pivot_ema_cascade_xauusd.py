"""Shape test: the Fibonacci-pivot / five-EMA cascade on real XAUUSD bars.

`algo/strategy/pivot_ema_cascade.py` implements the rule exactly as it was
described: a candle closes through a pivot line, then through the 10, 20, 50,
100 and 200 EMAs in order, and the close beyond the 200 is the entry. This
measures it, because a rule that has not been measured is a hypothesis with a
chart attached.

## What is swept, and what that is worth

The rule names **M5**, so M5 is the row that answers the question as asked. M15
and M30 are run beside it as a robustness check and nothing more: D-124
established that the bar interval is the dominant term for this instrument -
trade count roughly halves per step while the round-trip spread is charged per
round trip - so a result that exists on M5 and nowhere else is a result about
the spread, not about the pattern.

Three windows, D-140's, so these numbers sit beside every other expert measured
in this repo. **Reading the best cell as the result is the error D-131 names.**
The question is whether the rule shows an edge that survives changing the
window, not which cell is largest.

Nothing is tuned here. Every parameter is a stated default: the five EMAs the
rule names, the Fibonacci pivot type it names, and the project's usual 0.5%
stop with the trail off (D-124's finding on running a trail without a flat
stop, and D-154's on the give-back trail, both apply unchanged).

## The falsification block is not optional

The second table prices gold itself over the same window and splits the
strategy's own P&L into longs and shorts. A cascade rule fires on strong
directional moves, and in a market that trended up over the window that is
indistinguishable - from the net figure alone - from having simply been long.
D-152 is the entry where exactly that split rejected a strategy whose headline
number looked fine.

## Warmup

The 200 EMA needs 285 bars to shed its seed and the pivots need one complete
session before them, so each window is fed from well before its start and only
trades **opened inside** the window are scored. On M5 that warmup is about a day
of bars, which is the point: a window that started blind would be reporting the
first day's trades as though the indicator existed.

## Two ways in, because MT5 is Windows-only

The default source is the MetaTrader 5 terminal, as every other study here.
That makes those studies unrunnable anywhere else - including on the machine
this script was written on, which has neither the terminal nor any market data
reachable from it - so `--csv` takes the same bars from a file instead, through
the engine's own `read_csv_bars`. Same runner, same costs, same tables; only the
source differs, and the header says which one produced the numbers.

Usage:
    python scripts/measure_pivot_ema_cascade_xauusd.py
    python scripts/measure_pivot_ema_cascade_xauusd.py --csv M5=bars/xauusd_m5.csv
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from algo.backtest.cfd_runner import CfdCosts, CfdResult, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.instrument import CfdId
from algo.data.csv_feed import read_csv_bars
from algo.strategy.pivot_ema_cascade import PivotEmaCascade

XAUUSD = CfdId(symbol="XAUUSD")

#: One MT5 lot = 100 engine lots (ounces), matching every other study here so
#: the net figures sit on the same scale as D-124's matrix.
LOTS = 100
STARTING_EQUITY = Decimal("100000")

STOP_LOSS_PCT = Decimal("0.5")
TRAIL_ACTIVATION_PCT = Decimal("2")
TRAIL_PCT = Decimal("0")

#: M5 is the rule. The other two are the robustness check - see the docstring.
TIMEFRAMES = {
    "M5": (Timeframe(minutes=5), "TIMEFRAME_M5"),
    "M15": (Timeframe(minutes=15), "TIMEFRAME_M15"),
    "M30": (Timeframe(minutes=30), "TIMEFRAME_M30"),
}

#: D-140's three windows.
WINDOWS = [
    ("2026.06-08", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),
    ("2026.01-05", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
    ("2025.06-12", datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
]

#: A full session of M5 bars on top of the EMA's own warmup, so the first
#: scored bar has pivot lines drawn from a session the strategy watched end to
#: end rather than from the fragment it happened to be handed.
SESSION_BARS = 288


def fetch_bars(tf: Timeframe, mt5_constant: str, *, count: int = 200_000) -> list[Bar]:
    """Real closed bars. Position 1, not 0 - the forming bar's close can still
    change, the same exclusion `Mt5BarFeed.closed_bars` makes.

    `count` is larger than the other studies use because M5 over D-140's oldest
    window is roughly 60,000 bars on its own; MT5 returns what it has.

    The import is inside the function rather than at module scope: the package
    is a Windows-only extra (`pyproject.toml` says why it is not in
    `dependencies`), and the `--csv` path has to work on a machine without it.
    """
    import MetaTrader5 as mt5

    from algo.data.mt5_feed import measure_server_offset

    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select("XAUUSD", True):
        raise SystemExit(f"could not select XAUUSD: {mt5.last_error()}")
    offset = measure_server_offset(mt5, "XAUUSD")
    raw = mt5.copy_rates_from_pos("XAUUSD", getattr(mt5, mt5_constant), 1, count)
    mt5.shutdown()
    if raw is None or len(raw) == 0:
        raise SystemExit(f"MT5 returned no {mt5_constant} bars")
    bars = [
        Bar(
            ts=datetime.fromtimestamp(int(row["time"]), UTC) - offset,
            timeframe=tf,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["tick_volume"]),
        )
        for row in raw
    ]
    bars.sort(key=lambda b: b.ts)
    return bars


def run_cell(bars: list[Bar], tf: Timeframe) -> CfdResult:
    return run_cfd_backtest(
        bars,
        instrument=XAUUSD,
        timeframe=tf,
        strategy_factory=lambda: PivotEmaCascade(
            instrument=XAUUSD,
            stop_loss_pct=STOP_LOSS_PCT,
            trail_activation_pct=TRAIL_ACTIVATION_PCT,
            trail_pct=TRAIL_PCT,
            # Pinned OFF, as D-154 requires of every study that is not about
            # the give-back trail itself.
            giveback_frac=Decimal("0"),
        ),
        stop_loss_pct=STOP_LOSS_PCT,
        trail_activation_pct=TRAIL_ACTIVATION_PCT,
        trail_pct=TRAIL_PCT,
        lots=LOTS,
        starting_equity=STARTING_EQUITY,
        costs=CfdCosts(),
    )


def warmup_for() -> int:
    return PivotEmaCascade(instrument=XAUUSD).warmup_bars() + SESSION_BARS


def slice_for(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    """The window, preceded by enough bars to warm the EMAs and draw pivots."""
    first = next((i for i, b in enumerate(bars) if b.ts >= start), None)
    if first is None:
        return []
    return [b for b in bars[max(0, first - warmup_for()) :] if b.ts <= end]


def load_series(csv_paths: dict[str, Path]) -> dict[str, list[Bar]]:
    """Bars per timeframe, from files when given and from the terminal otherwise.

    A `--csv` run measures only the timeframes it was given files for. Silently
    falling back to MT5 for the rest would produce a table whose rows came from
    two different sources with nothing on the page saying so.
    """
    if csv_paths:
        return {
            name: read_csv_bars(path, TIMEFRAMES[name][0]) for name, path in csv_paths.items()
        }
    return {name: fetch_bars(tf, const) for name, (tf, const) in TIMEFRAMES.items()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        action="append",
        default=[],
        metavar="TF=PATH",
        help=(
            "read one timeframe's bars from a CSV (columns: ts, open, high, low, "
            "close[, volume]) instead of from MetaTrader 5, e.g. M5=bars/xauusd_m5.csv. "
            "Repeatable. Given at all, MT5 is not opened."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_paths: dict[str, Path] = {}
    for entry in args.csv:
        name, _, raw = entry.partition("=")
        if name not in TIMEFRAMES or not raw:
            raise SystemExit(f"--csv wants TF=PATH with TF in {list(TIMEFRAMES)}, got {entry!r}")
        csv_paths[name] = Path(raw)

    print("Fibonacci pivots + 10/20/50/100/200 EMA cascade on XAUUSD")
    print(
        f"{LOTS} engine lots (1.00 MT5 lot), ${STARTING_EQUITY} equity, "
        f"stop {STOP_LOSS_PCT}%, trail off. Costs: D-121 measured."
    )
    print(
        f"Warmup is {warmup_for()} bars before each window (285 for the 200 EMA "
        "plus one session for the pivots), and only trades opened INSIDE the "
        "window are scored.\n"
    )

    series = load_series(csv_paths)
    print(f"Source: {'CSV' if csv_paths else 'MetaTrader 5'}")
    for name, bars in series.items():
        print(f"  {name}: {len(bars)} bars, {bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d}")

    header = (
        f"\n{'window':<12} {'tf':<4} {'trades':>7} {'win%':>6} "
        f"{'PF':>6} {'net $':>12} {'maxDD%':>7} {'spread $':>10}"
    )
    print(header)
    print("-" * len(header))

    for label, start, end in WINDOWS:
        for tf_name, (tf, _const) in TIMEFRAMES.items():
            if tf_name not in series:
                continue
            sliced = slice_for(series[tf_name], start, end)
            if not sliced:
                continue
            result = run_cell(sliced, tf)
            scored = [t for t in result.trades if t.entry_ts >= start]
            net = sum((t.net_pnl for t in scored), Decimal("0"))
            won = sum((t.net_pnl for t in scored if t.net_pnl > 0), Decimal("0"))
            lost = -sum((t.net_pnl for t in scored if t.net_pnl <= 0), Decimal("0"))
            # `None` rather than "infinity" with no losing trade: that is a
            # sample-size statement, not a performance one.
            pf = (won / lost) if lost > 0 else None
            wr = (
                Decimal(sum(1 for t in scored if t.net_pnl > 0)) / Decimal(len(scored)) * 100
                if scored
                else None
            )
            spread = sum((t.spread_paid for t in scored), Decimal("0"))
            dd = result.max_drawdown_pct
            print(
                f"{label:<12} {tf_name:<4} {len(scored):>7} "
                f"{(f'{wr:.1f}' if wr is not None else '-'):>6} "
                f"{(f'{pf:.2f}' if pf is not None else '-'):>6} "
                f"{net:>12,.0f} "
                f"{(f'{dd:.1f}' if dd is not None else '-'):>7} "
                f"{spread:>10,.0f}"
            )
        print()

    # ------------------------------------------------------------------
    # The falsification D-124 point 4 asks for, and D-152 acted on. A
    # cascade fires on strong directional moves; in a window where gold
    # trended, that is not distinguishable from having been long.
    # ------------------------------------------------------------------
    print()
    print("### Falsification: is this edge, or is it just gold moving? ###")
    head = (
        f"{'window':<12} {'tf':<4} {'buy&hold $':>12} {'strategy $':>12} "
        f"{'long $':>11} {'short $':>11} {'longs':>6} {'shorts':>7}"
    )
    print(head)
    print("-" * len(head))
    for label, start, end in WINDOWS:
        for tf_name, (tf, _const) in TIMEFRAMES.items():
            if tf_name not in series:
                continue
            sliced = slice_for(series[tf_name], start, end)
            inside = [b for b in sliced if b.ts >= start]
            if not inside:
                continue
            hold = (inside[-1].close - inside[0].close) * LOTS
            scored = [t for t in run_cell(sliced, tf).trades if t.entry_ts >= start]
            longs = [t for t in scored if t.side is Side.BUY]
            shorts = [t for t in scored if t.side is Side.SELL]
            ln = sum((t.net_pnl for t in longs), Decimal("0"))
            sn = sum((t.net_pnl for t in shorts), Decimal("0"))
            print(
                f"{label:<12} {tf_name:<4} {hold:>12,.0f} {ln + sn:>12,.0f} "
                f"{ln:>11,.0f} {sn:>11,.0f} {len(longs):>6} {len(shorts):>7}"
            )
        print()

    # ------------------------------------------------------------------
    # How the trades ended. A cascade rule that mostly exits on the flat
    # stop is not the rule that was described - it is a stop being hit
    # before the 10/20 EMA exit ever gets a say, and the exit rule would
    # then be untested however good the net figure looked.
    # ------------------------------------------------------------------
    print()
    print("### How trades ended (M5 only) ###")
    tf, _const = TIMEFRAMES["M5"]
    for label, start, end in WINDOWS if "M5" in series else []:
        sliced = slice_for(series["M5"], start, end)
        if not sliced:
            continue
        scored = [t for t in run_cell(sliced, tf).trades if t.entry_ts >= start]
        by_kind: dict[str, int] = {}
        for trade in scored:
            kind = (
                "stop"
                if trade.exit_reason.startswith("stop loss")
                else "trail"
                if trade.exit_reason.startswith(("trailing stop", "give-back"))
                else "10/20 EMA"
                if "back " in trade.exit_reason
                else "open at end"
            )
            by_kind[kind] = by_kind.get(kind, 0) + 1
        counts = ", ".join(f"{kind} {count}" for kind, count in sorted(by_kind.items()))
        print(f"{label:<12} {len(scored):>5} trades: {counts or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
