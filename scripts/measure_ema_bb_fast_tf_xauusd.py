"""Is there a defensible GoldEmaBollinger setting for M1..M15 on XAUUSD?

D-124 measured this instrument as heavily net-negative below M15 because the
round-trip spread is charged per trade and trade count roughly doubles per step
down. This asks whether any parameter choice survives that at M1, M5 and M15.

The lever under test is `bb_stdev`, because it is the one parameter that
directly reduces trade count: a wider band fires less often, so it pays the
spread less often. `ema_period`, `bb_period` and the stop are held at their
defaults - sweeping them too would turn a comparison into a multi-dimensional
search over one dataset, which is the shape D-131 rejected.

Each timeframe is split into three consecutive equal windows and judged on
consistency across them, not on its best cell.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

XAUUSD = CfdId(symbol="XAUUSD")
LOTS = 100
EQUITY = Decimal("100000")
STOP = Decimal("0.5")
ACTIVATION = Decimal("2")
GIVEBACK = Decimal("0.5")
STDEVS = [2.0, 2.5, 3.0]

TFS = [("M1", 1, "TIMEFRAME_M1"), ("M5", 5, "TIMEFRAME_M5"), ("M15", 15, "TIMEFRAME_M15")]


def fetch(const: str, tf: Timeframe) -> list[Bar]:
    if not mt5.initialize():
        raise SystemExit("no MT5")
    mt5.symbol_select("XAUUSD", True)
    off = measure_server_offset(mt5, "XAUUSD")
    raw = mt5.copy_rates_from_pos("XAUUSD", getattr(mt5, const), 1, 50000)
    mt5.shutdown()
    bars = [
        Bar(ts=datetime.fromtimestamp(int(r["time"]), UTC) - off, timeframe=tf,
            open=Decimal(str(r["open"])), high=Decimal(str(r["high"])),
            low=Decimal(str(r["low"])), close=Decimal(str(r["close"])),
            volume=int(r["tick_volume"]))
        for r in raw
    ]
    bars.sort(key=lambda b: b.ts)
    return bars


def run(bars, tf, mode, stdev):
    return run_cfd_backtest(
        bars, instrument=XAUUSD, timeframe=tf,
        strategy_factory=lambda: EmaBollinger(
            instrument=XAUUSD, mode=mode, bb_stdev=stdev,
            stop_loss_pct=STOP, trail_activation_pct=ACTIVATION,
            trail_pct=Decimal("0"), giveback_frac=GIVEBACK),
        stop_loss_pct=STOP, trail_activation_pct=ACTIVATION,
        trail_pct=Decimal("0"), giveback_frac=GIVEBACK,
        lots=LOTS, starting_equity=EQUITY, costs=CfdCosts())


def pf(trades):
    w = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal("0"))
    losses = -sum((t.net_pnl for t in trades if t.net_pnl <= 0), Decimal("0"))
    return (w / losses) if losses > 0 else None


def main() -> int:
    print("GoldEmaBollinger on XAUUSD, fast timeframes, D-121 costs.")
    print(f"stop {STOP}% | activation {ACTIVATION}% | giveback {GIVEBACK} | {LOTS} oz\n")
    best = []
    for name, mins, const in TFS:
        tf = Timeframe(minutes=mins)
        bars = fetch(const, tf)
        warm = EmaBollinger(instrument=XAUUSD).warmup_bars() + 5
        n = len(bars)
        cuts = [(n * k // 3, n * (k + 1) // 3) for k in range(3)]
        print(f"=== {name}  {bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d} "
              f"({n} bars, 3 windows) ===")
        print(f"{'mode':<9}{'sd':>5}{'win1 net':>12}{'win2 net':>12}{'win3 net':>12}"
              f"{'trades':>8}{'pooled PF':>11}{'+wins':>7}")
        for mode in (BREAKOUT, PULLBACK):
            for sd in STDEVS:
                nets, alltr = [], []
                for lo, hi in cuts:
                    seg = bars[max(0, lo - warm):hi]
                    start = bars[lo].ts
                    res = run(seg, tf, mode, sd)
                    scored = [t for t in res.trades if t.entry_ts >= start]
                    nets.append(sum((t.net_pnl for t in scored), Decimal("0")))
                    alltr += scored
                p = pf(alltr)
                pos = sum(1 for v in nets if v > 0)
                print(f"{mode:<9}{sd:>5.1f}" + "".join(f"{float(v):>12,.0f}" for v in nets)
                      + f"{len(alltr):>8}"
                      + (f"{float(p):>11.2f}" if p else f"{'n/a':>11}")
                      + f"{pos:>5}/3")
                best.append((name, mode, sd, sum(nets), pos, len(alltr), p))
        print()

    print("=== configurations positive in all three windows ===")
    good = [b for b in best if b[4] == 3 and b[3] > 0]
    if not good:
        print("  NONE.")
    for b in sorted(good, key=lambda b: -b[3]):
        print(f"  {b[0]:<4} {b[1]:<9} sd {b[2]}  total {float(b[3]):>12,.0f}  "
              f"trades {b[5]}  PF {float(b[6]):.2f}")
    print("\n=== least-bad per timeframe (total net, all windows) ===")
    for name, _, _ in TFS:
        rows = [b for b in best if b[0] == name]
        top = max(rows, key=lambda b: b[3])
        print(f"  {name:<4} {top[1]:<9} sd {top[2]}  total {float(top[3]):>12,.0f}  "
              f"windows+ {top[4]}/3  trades {top[5]}  "
              + (f"PF {float(top[6]):.2f}" if top[6] else "PF n/a"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
