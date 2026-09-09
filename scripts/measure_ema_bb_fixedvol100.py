"""GoldEmaBollinger on FixedVol100 - measured with its own costs.

Third symbol, third cost model. The runner defaults to Vantage XAUUSD terms and
this strategy's whole finding is that cost dominates gross, so each symbol has
to be priced on its own numbers:

  spread as % of price
    XAUUSD 0.0064%   FixedVol100 0.0180%   BTCUSD 0.0215%

FixedVol100 uses swap_mode 5 (percent-annual) at -12% a year on BOTH sides -
unlike gold, where the short side earns. At 5,078.9 that is about $1.69 a night
per unit, converted to points at the price observed 2026-09-09. Same
approximation, same caveat as the BTCUSD study: the real charge tracks a price
that moves.

One engine lot = 1 unit (trade_contract_size 1.0), so the net figures are not
comparable to the XAUUSD studies. Read profit factors and window signs.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

SYM = "FixedVol100"
FV = CfdId(symbol=SYM)
LOTS = 1
EQUITY = Decimal("100000")
STOP = Decimal("0.5")
ACTIVATION = Decimal("2")
GIVEBACK = Decimal("0.5")
STDEVS = [2.0, 2.5, 3.0]

#: 915 points at 0.001 = 0.915 full spread, so half is 0.4575 in price units.
HALF_SPREAD = Decimal("0.4575")

#: -12% annual BOTH sides at 5,078.9 is 5078.9*0.12/360 = 1.693 a night per
#: unit = 1,693 points. point_value 0.001 because tick_value/tick_size is 1.0
#: money per price per broker lot, and one point is 0.001 of price.
FV_SWAP = SwapModel(
    long_points=Decimal("-1693"),
    short_points=Decimal("-1693"),
    point_value=Decimal("0.001"),
)

TFS = [("M1", 1, "TIMEFRAME_M1"), ("M5", 5, "TIMEFRAME_M5"), ("M15", 15, "TIMEFRAME_M15")]


def fetch(const: str, tf: Timeframe) -> list[Bar]:
    if not mt5.initialize():
        raise SystemExit("no MT5")
    mt5.symbol_select(SYM, True)
    off = measure_server_offset(mt5, SYM)
    raw = mt5.copy_rates_from_pos(SYM, getattr(mt5, const), 1, 50000)
    mt5.shutdown()
    if raw is None or not len(raw):
        raise SystemExit(f"no {SYM} {const} bars")
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
        bars, instrument=FV, timeframe=tf,
        strategy_factory=lambda m=mode, w=stdev: EmaBollinger(
            instrument=FV, mode=m, bb_stdev=w,
            stop_loss_pct=STOP, trail_activation_pct=ACTIVATION,
            trail_pct=Decimal("0"), giveback_frac=GIVEBACK),
        stop_loss_pct=STOP, trail_activation_pct=ACTIVATION,
        trail_pct=Decimal("0"), giveback_frac=GIVEBACK,
        lots=LOTS, starting_equity=EQUITY,
        costs=CfdCosts(half_spread=HALF_SPREAD, swap=FV_SWAP,
                       commission=CfdChargeModel()))


def pf(trades):
    w = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal("0"))
    losses = -sum((t.net_pnl for t in trades if t.net_pnl <= 0), Decimal("0"))
    return (w / losses) if losses > 0 else None


def main() -> int:
    print(f"GoldEmaBollinger on {SYM}, its own costs "
          f"(spread 0.915, swap -12%/yr both sides)")
    print(f"stop {STOP}% | activation {ACTIVATION}% | giveback {GIVEBACK} | {LOTS} unit\n")
    rows = []
    for name, mins, const in TFS:
        tf = Timeframe(minutes=mins)
        bars = fetch(const, tf)
        warm = EmaBollinger(instrument=FV).warmup_bars() + 5
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
                print(f"{mode:<9}{sd:>5.1f}"
                      + "".join(f"{float(v):>12,.0f}" for v in nets)
                      + f"{len(alltr):>8}"
                      + (f"{float(p):>8.2f}" if p else f"{'n/a':>8}")
                      + f"{pos:>4}/3")
                rows.append((name, mode, sd, sum(nets), pos, len(alltr), p))
        print()

    print("=== positive in all three windows ===")
    good = [r for r in rows if r[4] == 3 and r[3] > 0]
    if not good:
        print("  NONE.")
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
    raise SystemExit(main())
