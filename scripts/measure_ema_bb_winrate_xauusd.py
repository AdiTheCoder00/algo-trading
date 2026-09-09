"""Can a >60% win rate be built, and is it worth anything?

Win rate has not appeared in any table in this repo, which is deliberate - but
it is the number most people ask for first, so it is worth measuring rather than
asserting. The question has two halves and they have different answers:

  Is there an existing configuration above 60%?
  Can one be MADE above 60%?

The second is trivially yes, and the lever is the stop. Widening the stop moves
losers off the books - they get more room to come back - so the win rate rises
mechanically. Whether the account grows is a separate question, and the whole
point of the table is that the two columns move independently.

Single full-span run per cell, not the three-window discipline used elsewhere:
win rate is far more stable than net, and the net column here is indicative
rather than validated. Read `win%` against `net`, which is what this exists for.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

X = CfdId(symbol="XAUUSD")
STOPS = [Decimal("0.5"), Decimal("1"), Decimal("2"), Decimal("5")]
TFS = [("M5", 5, "TIMEFRAME_M5"), ("M15", 15, "TIMEFRAME_M15"), ("H1", 60, "TIMEFRAME_H1")]


def fetch(const: str, tf: Timeframe) -> list[Bar]:
    mt5.initialize()
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


def run(bars, tf, mode, sd, stop):
    return run_cfd_backtest(
        bars, instrument=X, timeframe=tf,
        strategy_factory=lambda m=mode, w=sd, s=stop: EmaBollinger(
            instrument=X, mode=m, bb_stdev=w, stop_loss_pct=s,
            trail_activation_pct=Decimal("2"), trail_pct=Decimal("0"),
            giveback_frac=Decimal("0")),
        stop_loss_pct=stop, trail_activation_pct=Decimal("2"),
        trail_pct=Decimal("0"), giveback_frac=Decimal("0"),
        lots=100, starting_equity=Decimal("100000"), costs=CfdCosts())


def main() -> int:
    print("XAUUSD, D-121 costs, 100 oz. Widening the stop is the win-rate lever.\n")
    hits = []
    for name, mins, const in TFS:
        tf = Timeframe(minutes=mins)
        bars = fetch(const, tf)
        print(f"=== {name}  {bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d} ===")
        print(f"{'mode':<9}{'sd':>4}{'stop%':>7}{'trades':>8}{'win%':>7}"
              f"{'net':>12}{'PF':>7}{'avg win':>10}{'avg loss':>10}")
        for mode in (BREAKOUT, PULLBACK):
            for sd in (2.0, 3.0):
                for stop in STOPS:
                    t = run(bars, tf, mode, sd, stop).trades
                    if not t:
                        continue
                    wins = [x for x in t if x.net_pnl > 0]
                    losses = [x for x in t if x.net_pnl <= 0]
                    gw = sum((x.net_pnl for x in wins), Decimal("0"))
                    gl = -sum((x.net_pnl for x in losses), Decimal("0"))
                    wr = 100 * len(wins) / len(t)
                    pf = (gw / gl) if gl > 0 else None
                    net = gw - gl
                    aw = (gw / len(wins)) if wins else Decimal("0")
                    al = (gl / len(losses)) if losses else Decimal("0")
                    flag = "  <-- >60%" if wr > 60 else ""
                    print(f"{mode:<9}{sd:>4.1f}{float(stop):>7.2f}{len(t):>8}{wr:>7.1f}"
                          f"{float(net):>12,.0f}"
                          + (f"{float(pf):>7.2f}" if pf else f"{'n/a':>7}")
                          + f"{float(aw):>10,.0f}{float(al):>10,.0f}{flag}")
                    if wr > 60:
                        hits.append((name, mode, sd, stop, wr, net, pf, len(t)))
        print()

    print("=== configurations with win rate above 60% ===")
    if not hits:
        print("  NONE in this sweep.")
    for h in hits:
        pf = f"PF {float(h[6]):.2f}" if h[6] else "PF n/a"
        print(f"  {h[0]:<4} {h[1]:<9} sd {h[2]} stop {float(h[3]):.2f}%  "
              f"win {h[4]:.1f}%  net {float(h[5]):>12,.0f}  {pf}  trades {h[7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
