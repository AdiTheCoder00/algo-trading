"""GoldEmaBollinger on BTCUSD - measured with BTCUSD's own costs, not gold's.

The default `CfdCosts` are the measured Vantage XAUUSD terms, and using them
here would be the single easiest way to produce a confident wrong answer: the
whole finding for this strategy is that cost dominates gross, and BTCUSD's cost
is a different animal.

  spread   XAUUSD  $0.28 on a 4,404 price = 0.0064% of price
           BTCUSD $17.04 on a 79,351 price = 0.0215% - 3.4x wider, relatively

  swap     XAUUSD points-per-lot-per-night, as MT5 publishes it (mode 1)
           BTCUSD swap_mode 5, percent-annual: -20% a year on the LONG side,
           0 on the short. At 79,351 that is about $44 a night per BTC.

`SwapModel` expresses points per broker lot per night, so the percent-annual
rate is converted at the price observed on 2026-09-09. That is an
APPROXIMATION and it is the weakest number in this file: the real charge tracks
the price, and BTC has not been near one price for long. It is stated rather
than hidden, and it only bites on positions held overnight.

One engine lot = 1 BTC here (`trade_contract_size` is 1.0), against one ounce
for gold, so the net figures are not comparable to the XAUUSD studies. Read the
profit factors and the window signs, not the dollars.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

BTCUSD = CfdId(symbol="BTCUSD")
LOTS = 1
EQUITY = Decimal("100000")
STOP = Decimal("0.5")
ACTIVATION = Decimal("2")
GIVEBACK = Decimal("0.5")
STDEVS = [2.0, 2.5, 3.0]

#: Observed 2026-09-09 on the Vantage demo: spread 1,704 points at 0.01 = $17.04
#: full, so half is $8.52 in price units.
HALF_SPREAD = Decimal("8.52")

#: -20% annual on the long side at a price of 79,351 is 79351*0.20/360 = $44.08
#: a night per BTC = 4,408 points. Short is 0. See the module docstring on why
#: this is the weakest figure here.
BTC_SWAP = SwapModel(
    long_points=Decimal("-4408"),
    short_points=Decimal("0"),
    point_value=Decimal("0.01"),
)

TFS = [
    ("M5", 5, "TIMEFRAME_M5"),
    ("M15", 15, "TIMEFRAME_M15"),
    ("H1", 60, "TIMEFRAME_H1"),
    ("H4", 240, "TIMEFRAME_H4"),
]


def costs() -> CfdCosts:
    return CfdCosts(
        half_spread=HALF_SPREAD,
        swap=BTC_SWAP,
        commission=CfdChargeModel(),
    )


def fetch(const: str, tf: Timeframe) -> list[Bar]:
    if not mt5.initialize():
        raise SystemExit("no MT5")
    mt5.symbol_select("BTCUSD", True)
    off = measure_server_offset(mt5, "BTCUSD")
    raw = mt5.copy_rates_from_pos("BTCUSD", getattr(mt5, const), 1, 50000)
    mt5.shutdown()
    if raw is None or not len(raw):
        raise SystemExit(f"no BTCUSD {const} bars")
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
        bars, instrument=BTCUSD, timeframe=tf,
        strategy_factory=lambda: EmaBollinger(
            instrument=BTCUSD, mode=mode, bb_stdev=stdev,
            stop_loss_pct=STOP, trail_activation_pct=ACTIVATION,
            trail_pct=Decimal("0"), giveback_frac=GIVEBACK),
        stop_loss_pct=STOP, trail_activation_pct=ACTIVATION,
        trail_pct=Decimal("0"), giveback_frac=GIVEBACK,
        lots=LOTS, starting_equity=EQUITY, costs=costs())


def pf(trades):
    w = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal("0"))
    losses = -sum((t.net_pnl for t in trades if t.net_pnl <= 0), Decimal("0"))
    return (w / losses) if losses > 0 else None


def main() -> int:
    print("GoldEmaBollinger on BTCUSD, BTCUSD costs (spread $17.04, long swap ~ -20%/yr)")
    print(f"stop {STOP}% | activation {ACTIVATION}% | giveback {GIVEBACK} | {LOTS} BTC\n")
    rows = []
    for name, mins, const in TFS:
        tf = Timeframe(minutes=mins)
        bars = fetch(const, tf)
        warm = EmaBollinger(instrument=BTCUSD).warmup_bars() + 5
        n = len(bars)
        cuts = [(n * k // 3, n * (k + 1) // 3) for k in range(3)]
        print(f"=== {name}  {bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d} ({n} bars) ===")
        print(f"{'mode':<9}{'sd':>5}{'win1':>12}{'win2':>12}{'win3':>12}"
              f"{'trades':>8}{'PF':>8}{'+w':>6}")
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
                      + (f"{float(p):>8.2f}" if p else f"{'n/a':>8}") + f"{pos:>4}/3")
                rows.append((name, mode, sd, sum(nets), pos, len(alltr), p))
        print()

    print("=== positive in all three windows ===")
    good = [r for r in rows if r[4] == 3 and r[3] > 0]
    print("  NONE." if not good else "")
    for r in sorted(good, key=lambda r: -r[3]):
        print(f"  {r[0]:<4} {r[1]:<9} sd {r[2]}  {float(r[3]):>12,.0f}  "
              f"trades {r[5]}  PF {float(r[6]):.2f}")
    print("\n=== least-bad per timeframe ===")
    for name, _, _ in TFS:
        sel = [r for r in rows if r[0] == name]
        top = max(sel, key=lambda r: r[3])
        print(f"  {name:<4} {top[1]:<9} sd {top[2]}  {float(top[3]):>12,.0f}  "
              f"+w {top[4]}/3  trades {top[5]}  "
              + (f"PF {float(top[6]):.2f}" if top[6] else "PF n/a"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
