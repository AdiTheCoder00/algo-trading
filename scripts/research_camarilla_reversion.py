"""Does fading a Camarilla extreme beat taking the breakout?

## Where the hypothesis came from

Not from folklore. `measure_camarilla_ea.py` scored the textbook Camarilla
breakout rule - long only above H4, short only below L4 - and it lost to a
random control on every symbol tried, 12 times out of 12. The exit that rule
produced had a **0.0% win rate** and a mean of -4.00 on XAUUSD.

A rule that loses that consistently is not noise; it is a signal read backwards.
If breaks beyond the extreme are exhaustion rather than continuation, the trade
is the other one: sell the break above, buy the break below, and target the
level behind it. That is also what Camarilla's own literature says to do, which
is worth noting but is not why it is being tested.

## What is measured

Entry: the bar CLOSES beyond the entry level. Short above H(n), long below L(n).
Exit, whichever comes first:
  - target: back to the level one step nearer the middle
  - stop:   the level one step further out
  - time:   `--max-bars` bars, so a trade cannot be held for ever

Costs: half the current spread on each leg. Swap is ignored, which flatters a
strategy holding overnight - `--max-bars` keeps holds short enough that this is
small, and the trade count is reported so it can be judged.

## The two things that stop this being curve-fitting

**A placebo.** Every arm is rerun with entries at random instants, the same
number of them, same side mix. An arm that cannot beat its own control has shown
nothing, whatever its P&L looks like.

**A split.** The window is cut in two. Parameters are chosen on the first half
and the second half is reported separately, untouched. An edge that exists only
in the half it was chosen on is a fit, not an edge - and that is the usual
outcome, so the split is printed even when it disagrees.
"""

from __future__ import annotations

import argparse
import itertools
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

LOTS = {"FixedVol100": 0.10, "BTCUSD": 0.03, "XAUUSD": 0.01}
TF = {5: mt5.TIMEFRAME_M5, 15: mt5.TIMEFRAME_M15,
      30: mt5.TIMEFRAME_M30, 60: mt5.TIMEFRAME_H1}

#: entry level -> (target level, stop level). Nearer the middle is the target.
LADDER = {
    "3": ("2", "4"),
    "4": ("3", "5"),
}


@dataclass
class Trade:
    side: int          # +1 long, -1 short
    entry_i: int
    entry: float
    exit_i: int = -1
    exit: float = 0.0
    why: str = ""
    tgt: float = 0.0
    stop: float = 0.0

    def net(self, mpp: float, half_spread: float) -> float:
        return (self.exit - self.entry) * self.side * mpp - 2 * half_spread * mpp


def camarilla(ph: float, pl: float, pc: float) -> dict[str, float]:
    rng = ph - pl
    h5 = (ph / pl) * pc if pl > 0 else pc
    r = 1.1
    return {
        "L5": pc - (h5 - pc), "L4": pc - rng * r / 2, "L3": pc - rng * r / 4,
        "L2": pc - rng * r / 6, "L1": pc - rng * r / 12,
        "H1": pc + rng * r / 12, "H2": pc + rng * r / 6,
        "H3": pc + rng * r / 4, "H4": pc + rng * r / 2, "H5": h5,
    }


def load(symbol: str, tf: int, bars: int):
    mt5.symbol_select(symbol, True)
    raw = mt5.copy_rates_from_pos(symbol, TF[tf], 1, bars)
    d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 400)
    if raw is None or len(raw) < 500 or d1 is None or len(d1) < 3:
        raise SystemExit(f"{symbol}: not enough history")
    levels: dict[object, dict[str, float]] = {}
    for prev, cur in itertools.pairwise(d1):
        if float(prev["low"]) <= 0 or float(prev["high"]) <= float(prev["low"]):
            continue
        key = datetime.fromtimestamp(int(cur["time"]), UTC).date()
        levels[key] = camarilla(float(prev["high"]), float(prev["low"]),
                                float(prev["close"]))
    return raw, levels


def placebo(raw, real: list[Trade], *, max_bars: int, lo: int, hi: int,
            seed: int) -> list[Trade]:
    """The same trades, at random times.

    Two earlier attempts at a control were wrong in instructive ways, so what
    this preserves is worth stating.

    Entering at random bars while keeping the LEVELS as the target and stop puts
    them on the wrong side of the entry - a random long is handed a target below
    where it bought, and a short is handed a "stop" below it too, so hitting the
    stop books a profit. That control produced enormous positive numbers and
    meant nothing.

    Borrowing another session's levels fails the same way for the same reason.

    What must be held constant is the GEOMETRY: side, distance to target,
    distance to stop, and the number of trades. Only the instant is randomised.
    Then the question the control answers is the right one - does entering at
    these levels beat entering anywhere at all, with the identical trade shape?
    """
    h = [float(x["high"]) for x in raw]
    lo_ = [float(x["low"]) for x in raw]
    c = [float(x["close"]) for x in raw]
    o = [float(x["open"]) for x in raw]

    rng = random.Random(seed)
    out: list[Trade] = []
    busy_until = -1
    spots = sorted(rng.sample(range(lo, hi - max_bars - 2), min(len(real),
                                                                hi - lo - max_bars - 3)))
    for k, i in enumerate(spots):
        if i <= busy_until:
            continue
        src = real[k % len(real)]
        side = src.side
        d_tgt = abs(src.entry - _tgt_of(src))
        d_stop = abs(src.entry - _stop_of(src))
        entry = o[i + 1]
        target = entry + side * d_tgt
        stop = entry - side * d_stop
        tr = Trade(side=side, entry_i=i + 1, entry=entry)
        for j in range(i + 1, min(i + 1 + max_bars, hi)):
            hit_stop = (lo_[j] <= stop) if side > 0 else (h[j] >= stop)
            hit_tgt = (h[j] >= target) if side > 0 else (lo_[j] <= target)
            if hit_stop:
                tr.exit_i, tr.exit, tr.why = j, stop, "stop"
                break
            if hit_tgt:
                tr.exit_i, tr.exit, tr.why = j, target, "target"
                break
        if tr.exit_i < 0:
            last = min(i + max_bars, hi - 1)
            tr.exit_i, tr.exit, tr.why = last, c[last], "time"
        out.append(tr)
        busy_until = tr.exit_i
    return out


def _tgt_of(t: Trade) -> float:
    return t.tgt


def _stop_of(t: Trade) -> float:
    return t.stop


def run(raw, levels, *, entry_level: str, max_bars: int,
        lo: int, hi: int) -> list[Trade]:
    """Bars `lo`..`hi`."""
    o = [float(x["open"]) for x in raw]
    h = [float(x["high"]) for x in raw]
    lo_ = [float(x["low"]) for x in raw]
    c = [float(x["close"]) for x in raw]
    t = [datetime.fromtimestamp(int(x["time"]), UTC) for x in raw]
    tgt_name, stop_name = LADDER[entry_level]

    signals: list[tuple[int, int]] = []      # (bar, side)
    for i in range(lo, hi):
        lv = levels.get(t[i].date())
        if lv is None:
            continue
        # Fade: a close beyond the extreme is taken as exhaustion.
        if c[i] > lv["H" + entry_level]:
            signals.append((i, -1))
        elif c[i] < lv["L" + entry_level]:
            signals.append((i, +1))


    trades: list[Trade] = []
    busy_until = -1
    for i, side in signals:
        if i <= busy_until or i + 1 >= hi:
            continue
        lv = levels.get(t[i].date())
        if lv is None:
            continue
        pre = "L" if side > 0 else "H"
        target = lv[pre + tgt_name]
        stop = lv[pre + stop_name]
        entry = o[i + 1]                      # fill at the next bar's open
        # The entry must sit BETWEEN the two, or they are not a target and a
        # stop at all. A bar that closed beyond H4 leaves the "stop" below a
        # short's entry, so price reaching it books a gain - which is how this
        # test first reported 86% of its exits as stops and an 82% win rate at
        # the same time. Signals that far gone are skipped, not repriced.
        ok = (stop < entry < target) if side > 0 else (target < entry < stop)
        if not ok:
            continue
        tr = Trade(side=side, entry_i=i + 1, entry=entry, tgt=target, stop=stop)
        for j in range(i + 1, min(i + 1 + max_bars, hi)):
            # Against the bar's RANGE, not its close. A bar that spikes through
            # the stop and closes back inside has stopped the trade out; testing
            # the close instead is how a backtest invents a win rate.
            hit_stop = (lo_[j] <= stop) if side > 0 else (h[j] >= stop)
            hit_tgt = (h[j] >= target) if side > 0 else (lo_[j] <= target)
            # Both inside one bar: assume the stop came first. The bar cannot say
            # which did, and guessing in the trade's favour is the other way to
            # manufacture a result.
            if hit_stop:
                tr.exit_i, tr.exit, tr.why = j, stop, "stop"
                break
            if hit_tgt:
                tr.exit_i, tr.exit, tr.why = j, target, "target"
                break
        if tr.exit_i < 0:
            last = min(i + max_bars, hi - 1)
            tr.exit_i, tr.exit, tr.why = last, c[last], "time"
        trades.append(tr)
        busy_until = tr.exit_i
    return trades


def summarise(trades: list[Trade], mpp: float, half: float) -> dict:
    if not trades:
        return {"n": 0, "net": 0.0, "win": 0.0}
    nets = [t.net(mpp, half) for t in trades]
    wins = [x for x in nets if x > 0]
    return {
        "n": len(trades),
        "net": sum(nets),
        "win": len(wins) / len(nets) * 100,
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean([x for x in nets if x <= 0] or [0.0]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="XAUUSD,FixedVol100,BTCUSD")
    ap.add_argument("--tfs", default="15,60")
    ap.add_argument("--levels", default="3,4")
    ap.add_argument("--max-bars", type=int, default=24)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--placebo", type=int, default=10)
    args = ap.parse_args()

    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    try:
        for symbol in args.symbols.split(","):
            info = mt5.symbol_info(symbol)
            mpp = (info.trade_tick_value / info.trade_tick_size) * LOTS[symbol]
            half = info.spread * info.point / 2
            print("\n" + "=" * 92)
            print(f"{symbol}   {LOTS[symbol]} lots   "
                  f"spread {info.spread * info.point:.{info.digits}f}")
            print(f"  {'tf':>4} {'lvl':>4} {'half':>5} {'n':>5} {'win%':>6} "
                  f"{'net':>10} {'placebo mean':>13} {'beat':>6}")
            for tf in (int(x) for x in args.tfs.split(",")):
                raw, levels = load(symbol, tf, args.bars)
                mid = len(raw) // 2
                for lvl in args.levels.split(","):
                    for name, lo, hi in (("1st", 60, mid), ("2nd", mid, len(raw))):
                        tr = run(raw, levels, entry_level=lvl,
                                 max_bars=args.max_bars, lo=lo, hi=hi)
                        s = summarise(tr, mpp, half)
                        if not s["n"]:
                            continue
                        ctrl = []
                        for seed in range(args.placebo):
                            ct = placebo(raw, tr, max_bars=args.max_bars,
                                         lo=lo, hi=hi, seed=seed)
                            ctrl.append(summarise(ct, mpp, half)["net"])
                        beat = sum(1 for x in ctrl if x >= s["net"])
                        by = {w: sum(1 for x in tr if x.why == w)
                              for w in ("target", "stop", "time")}
                        print(f"  {('M' + str(tf)):>4} {lvl:>4} {name:>5} {s['n']:>5} "
                              f"{s['win']:>5.1f}% {s['net']:>10.2f} "
                              f"{statistics.mean(ctrl):>13.2f} "
                              f"{beat:>3}/{len(ctrl)}  "
                              f"tgt/stop/time {by['target']}/{by['stop']}/{by['time']}")
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
