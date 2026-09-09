"""Does a stop that actually binds reduce the failed-breakout loss on XAUUSD?

The live 0.5% stop sits about $22/oz from entry on gold near 4,400, while the
middle-band exit fires within a band half-width - so on M1 the stop never binds
and every failed breakout costs the full upper-to-middle distance. This sweeps
the stop down until it does bind, and reports the average LOSING trade as well
as net, because "minimise this type of loss" is a question about the left tail,
not only about the total.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, EmaBollinger

X = CfdId(symbol="XAUUSD")
STOPS = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2"), Decimal("0.15"), Decimal("0.1")]

def fetch(const, tf):
    mt5.initialize()
    mt5.symbol_select("XAUUSD", True)
    off = measure_server_offset(mt5, "XAUUSD")
    raw = mt5.copy_rates_from_pos("XAUUSD", getattr(mt5, const), 1, 50000)
    mt5.shutdown()
    b = [Bar(ts=datetime.fromtimestamp(int(r["time"]), UTC) - off, timeframe=tf,
             open=Decimal(str(r["open"])), high=Decimal(str(r["high"])),
             low=Decimal(str(r["low"])), close=Decimal(str(r["close"])),
             volume=int(r["tick_volume"])) for r in raw]
    b.sort(key=lambda x: x.ts)
    return b

for name, mins, const, sd in (("M1", 1, "TIMEFRAME_M1", 2.0), ("M5", 5, "TIMEFRAME_M5", 2.0),
                              ("M5", 5, "TIMEFRAME_M5", 3.0)):
    tf = Timeframe(minutes=mins)
    bars = fetch(const, tf)
    print(f"=== {name} breakout sd{sd}  {bars[0].ts:%Y-%m-%d}..{bars[-1].ts:%Y-%m-%d} ===")
    print(f"{'stop%':>7}{'net':>12}{'trades':>8}{'PF':>7}{'win%':>7}"
          f"{'avg loss':>10}{'worst':>10}{'stop-outs':>10}")
    for stop in STOPS:
        res = run_cfd_backtest(
            bars, instrument=X, timeframe=tf,
            strategy_factory=lambda s=stop, w=sd: EmaBollinger(
                instrument=X, mode=BREAKOUT, bb_stdev=w, stop_loss_pct=s,
                trail_activation_pct=Decimal("2"), trail_pct=Decimal("0"),
                giveback_frac=Decimal("0.5")),
            stop_loss_pct=stop, trail_activation_pct=Decimal("2"),
            trail_pct=Decimal("0"), giveback_frac=Decimal("0.5"),
            lots=100, starting_equity=Decimal("100000"), costs=CfdCosts())
        t = res.trades
        if not t:
            continue
        losers = [x for x in t if x.net_pnl <= 0]
        wins = sum((x.net_pnl for x in t if x.net_pnl > 0), Decimal("0"))
        loss = -sum((x.net_pnl for x in losers), Decimal("0"))
        pf = (wins / loss) if loss > 0 else None
        avg_l = (loss / len(losers)) if losers else Decimal("0")
        worst = min((x.net_pnl for x in t), default=Decimal("0"))
        so = sum(1 for x in t if (x.exit_reason or "").startswith("stop loss"))
        net = sum((x.net_pnl for x in t), Decimal("0"))
        print(f"{float(stop):>7.2f}{float(net):>12,.0f}{len(t):>8}"
              + (f"{float(pf):>7.2f}" if pf else f"{'n/a':>7}")
              + f"{100*(len(t)-len(losers))/len(t):>7.1f}"
              + f"{float(avg_l):>10,.0f}{float(worst):>10,.0f}{so:>10}")
    print()
