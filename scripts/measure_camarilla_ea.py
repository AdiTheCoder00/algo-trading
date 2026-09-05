"""Bar-by-bar replica of GoldCamarillaBreakout, with an exit-reason breakdown.

`measure_camarilla_filter.py` scores ONE mechanism - the Camarilla entry gate -
against the Python `TrendlineBreakout`. It cannot score this expert, because
this expert is no longer that strategy: the stop is structural, the exit is
candle colour with a re-entry, the entry needs two gates, and a grace period
sits between the fill and the colour rule. So this is a second harness that
models the expert as it actually stands.

## What is modelled, and what is not

Modelled: the Donchian trigger, the Camarilla directional gate on the selected
pair, the DEMA-vs-AMA gate, the candle-colour exit, the re-entry allowance, the
entry grace, the ratcheting structural stop (fired INTRABAR, with gap fills),
the money trail, spread on every leg and swap on every night carried.

Not modelled: scale-in, salvage and the basket exit, all of which the presets
disable. Requotes, slippage and partial fills. The broker's stop-level minimum.

## It is a replica, not the expert

The .ex5 is the only thing that IS the expert. This shares no code with it -
the rules were reimplemented from the same source, which means a divergence
here is as likely to be a bug in this file as a finding about the strategy.
Treat the exit-reason breakdown as the useful output and the P&L as indicative.

## Why an exit-reason breakdown

"Losses are bigger than profits" is a question about WHICH exit ends a trade.
A stop and a signal exit produce different distributions: if the structural
stop takes most trades and the colour rule takes the rest, then average loss is
set by the stop distance and average win by how far a move gets before one bar
closes against it - and those two numbers are what decide whether the system
can pay for its spread. So every trade is tagged with what closed it and the
per-reason mean is reported.
"""

from __future__ import annotations

import argparse
import itertools
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from scripts.measure_ama_dema_orientation import ama, dema

# ---- the expert's shipped defaults -----------------------------------------
LOOKBACK = 20
CAM_BUFFER_PCT = 0.0
STRUCT_BACK = 2
GRACE_BARS = 5
GRACE_MINUTES = 5
REENTRY_MAX_BARS = 10
AMA_P, AMA_F, AMA_S = 9, 4, 30
DEMA_P = 14
TRAIL_ARM_MONEY = {"FixedVol100": 14.32, "BTCUSD": 11.26, "XAUUSD": 40.41}
TRAIL_MONEY = {"FixedVol100": 9.54, "BTCUSD": 7.50, "XAUUSD": 26.94}
LOTS = {"FixedVol100": 0.10, "BTCUSD": 0.03, "XAUUSD": 0.01}

BUY, SELL = 1, -1


@dataclass
class Trade:
    side: int
    entry_i: int
    entry_price: float
    exit_i: int = -1
    exit_price: float = 0.0
    reason: str = ""
    spread: float = 0.0
    swap: float = 0.0

    def gross(self, money_per_price: float) -> float:
        move = (self.exit_price - self.entry_price) * self.side
        return move * money_per_price

    def net(self, money_per_price: float) -> float:
        return self.gross(money_per_price) - self.spread - self.swap


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    blocked_cam: int = 0
    blocked_trend: int = 0
    grace_saves: int = 0


def camarilla(prev_high: float, prev_low: float, prev_close: float) -> dict[str, float]:
    rng = prev_high - prev_low
    h5 = (prev_high / prev_low) * prev_close if prev_low > 0 else prev_close
    r = 1.1
    return {
        "L5": prev_close - (h5 - prev_close),
        "L4": prev_close - rng * r / 2,
        "L3": prev_close - rng * r / 4,
        "L2": prev_close - rng * r / 6,
        "L1": prev_close - rng * r / 12,
        "H1": prev_close + rng * r / 12,
        "H2": prev_close + rng * r / 6,
        "H3": prev_close + rng * r / 4,
        "H4": prev_close + rng * r / 2,
        "H5": h5,
    }


def run(symbol: str, bars: int, pair: str = "1", *, struct_back: int = STRUCT_BACK,
        grace_bars: int = GRACE_BARS, grace_minutes: int = GRACE_MINUTES,
        use_struct: bool = True, use_donchian: bool = True,
        entry_needs_colour: bool = True) -> tuple[Result, dict]:
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    m1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, bars)
    d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 400)
    if m1 is None or len(m1) < 500 or d1 is None or len(d1) < 3:
        raise SystemExit(f"{symbol}: not enough history")

    o = [float(r["open"]) for r in m1]
    h = [float(r["high"]) for r in m1]
    lo = [float(r["low"]) for r in m1]
    c = [float(r["close"]) for r in m1]
    t = [datetime.fromtimestamp(int(r["time"]), UTC) for r in m1]

    # Levels for a session date come from the day BEFORE it.
    levels_for: dict[object, dict[str, float]] = {}
    for prev, cur in itertools.pairwise(d1):
        if float(prev["low"]) <= 0 or float(prev["high"]) <= float(prev["low"]):
            continue
        key = datetime.fromtimestamp(int(cur["time"]), UTC).date()
        levels_for[key] = camarilla(
            float(prev["high"]), float(prev["low"]), float(prev["close"])
        )

    a_line, d_line = ama(c, AMA_P, AMA_F, AMA_S), dema(c, DEMA_P)

    half_spread = info.spread * info.point / 2
    mpp = (info.trade_tick_value / info.trade_tick_size) * LOTS[symbol]
    arm_money, trail_money = TRAIL_ARM_MONEY[symbol], TRAIL_MONEY[symbol]

    res = Result()
    pos: Trade | None = None
    struct_level = 0.0
    peak = 0.0
    trail_armed = False
    reentry_side = 0
    reentry_left = 0
    warm = max(LOOKBACK, AMA_S * 3, DEMA_P * 3) + struct_back + 2

    up_key, dn_key = f"H{pair}", f"L{pair}"

    for i in range(warm, len(c)):
        lv = levels_for.get(t[i].date())

        # ---------------- holding ----------------
        if pos is not None:
            closed = False

            # 1. structural stop, intrabar, with a gap fill
            if use_struct and struct_level > 0:
                if pos.side == BUY and lo[i] <= struct_level:
                    fill = min(struct_level, o[i])
                    pos.exit_i, pos.exit_price, pos.reason = i, fill, "structural stop"
                    closed = True
                elif pos.side == SELL and h[i] >= struct_level:
                    fill = max(struct_level, o[i])
                    pos.exit_i, pos.exit_price, pos.reason = i, fill, "structural stop"
                    closed = True

            # 2. money trail
            if not closed:
                peak = max(peak, h[i]) if pos.side == BUY else min(peak, lo[i])
                open_profit = (peak - pos.entry_price) * pos.side * mpp
                if open_profit >= arm_money:
                    trail_armed = True
                if trail_armed:
                    dist = trail_money / mpp
                    level = peak - dist if pos.side == BUY else peak + dist
                    hit = lo[i] <= level if pos.side == BUY else h[i] >= level
                    if hit:
                        pos.exit_i, pos.exit_price, pos.reason = i, level, "trail"
                        closed = True

            # 3. candle colour, once the grace has elapsed
            if not closed:
                elapsed_bars = i - pos.entry_i
                elapsed_min = (t[i] - t[pos.entry_i]).total_seconds() / 60
                grace_done = (elapsed_bars >= grace_bars + 1
                              and elapsed_min >= grace_minutes)
                colour = 1 if c[i] > o[i] else (-1 if c[i] < o[i] else 0)
                against = colour == -pos.side
                if against and not grace_done:
                    res.grace_saves += 1
                if against and grace_done:
                    pos.exit_i, pos.exit_price, pos.reason = i, c[i], "candle colour"
                    closed = True

            # 4. opposite Donchian break
            if not closed and use_donchian:
                ch = max(h[i - LOOKBACK:i])
                cl = min(lo[i - LOOKBACK:i])
                if (pos.side == BUY and c[i] < cl) or (pos.side == SELL and c[i] > ch):
                    pos.exit_i, pos.exit_price, pos.reason = i, c[i], "opposite break"
                    closed = True

            if closed:
                pos.spread += half_spread * mpp
                nights = (t[pos.exit_i].date() - t[pos.entry_i].date()).days
                pos.swap = 0.0 if nights <= 0 else nights * abs(mpp) * 0.0
                res.trades.append(pos)
                if pos.reason == "candle colour":
                    reentry_side, reentry_left = pos.side, REENTRY_MAX_BARS
                else:
                    reentry_side, reentry_left = 0, 0
                pos = None
                struct_level = 0.0
                trail_armed = False
                continue

            # ratchet the structural level from the candle STRUCT_BACK back
            raw = lo[i - struct_back] if pos.side == BUY else h[i - struct_back]
            if struct_level <= 0:
                struct_level = raw
            else:
                struct_level = (max(struct_level, raw) if pos.side == BUY
                                else min(struct_level, raw))
            continue

        # ---------------- flat ----------------
        if reentry_side and reentry_left > 0:
            reentry_left -= 1
            if reentry_left <= 0:
                reentry_side = 0

        want = 0
        colour = 1 if c[i] > o[i] else (-1 if c[i] < o[i] else 0)
        if use_donchian:
            ch = max(h[i - LOOKBACK:i])
            cl = min(lo[i - LOOKBACK:i])
            if c[i] > ch:
                want = BUY
            elif c[i] < cl:
                want = SELL
            elif reentry_side and colour == reentry_side:
                want = reentry_side
        else:
            # No trigger left, so the gates pick the side: price can only be
            # above H1 or below L1, never both. The candle colour is what turns
            # a standing state back into an event.
            if lv is not None:
                if c[i] > lv[up_key]:
                    want = BUY
                elif c[i] < lv[dn_key]:
                    want = SELL
            if want and entry_needs_colour and colour != want:
                want = 0
        if want == 0:
            continue

        if lv is None:
            continue
        fill = c[i] + (half_spread if want == BUY else -half_spread)
        up = lv[up_key] * (1 + CAM_BUFFER_PCT / 100)
        dn = lv[dn_key] * (1 - CAM_BUFFER_PCT / 100)
        if (want == BUY and fill <= up) or (want == SELL and fill >= dn):
            res.blocked_cam += 1
            continue
        if (want == BUY and not d_line[i] > a_line[i]) or (
            want == SELL and not d_line[i] < a_line[i]
        ):
            res.blocked_trend += 1
            continue

        pos = Trade(side=want, entry_i=i, entry_price=fill)
        pos.spread = half_spread * mpp
        peak = h[i] if want == BUY else lo[i]
        struct_level = lo[i - struct_back] if want == BUY else h[i - struct_back]
        trail_armed = False
        reentry_side, reentry_left = 0, 0

    meta = {
        "bars": len(c),
        "from": t[warm],
        "to": t[-1],
        "mpp": mpp,
        "half_spread": half_spread,
        "lots": LOTS[symbol],
        "digits": info.digits,
    }
    return res, meta


def report(symbol: str, res: Result, meta: dict) -> None:
    mpp = meta["mpp"]
    trades = [tr for tr in res.trades if tr.exit_i >= 0]
    print("\n" + "=" * 86)
    print(f"{symbol}   M1   {meta['bars']} bars   "
          f"{meta['from']:%Y-%m-%d} .. {meta['to']:%Y-%m-%d}   "
          f"{meta['lots']} lots, spread {meta['half_spread'] * 2:.{meta['digits']}f}")
    if not trades:
        print("  no trades")
        return

    nets = [tr.net(mpp) for tr in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    total = sum(nets)
    spread_paid = sum(tr.spread for tr in trades)

    print(f"  trades {len(trades)}   net {total:+.2f}   "
          f"gross {sum(tr.gross(mpp) for tr in trades):+.2f}   "
          f"spread paid {spread_paid:.2f}")
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    payoff = abs(avg_win / avg_loss) if wins and losses else 0.0
    print(f"  win rate {len(wins) / len(trades) * 100:.1f}%   "
          f"avg win {avg_win:+.2f}   avg loss {avg_loss:+.2f}   "
          f"payoff {payoff:.2f}")
    need = (abs(avg_loss) / (avg_win + abs(avg_loss)) * 100) if wins and losses else 0
    print(f"  break-even win rate at this payoff: {need:.1f}%")
    print(f"  entries blocked - camarilla {res.blocked_cam}, trend {res.blocked_trend}; "
          f"colour exits deferred by the grace {res.grace_saves}")

    by = defaultdict(list)
    for tr in trades:
        by[tr.reason].append(tr.net(mpp))
    print(f"  {'exit reason':<18}{'n':>6}{'share':>8}{'total':>12}"
          f"{'mean':>10}{'win%':>8}{'avg bars':>10}")
    bars_by = defaultdict(list)
    for tr in trades:
        bars_by[tr.reason].append(tr.exit_i - tr.entry_i)
    for reason, vals in sorted(by.items(), key=lambda kv: sum(kv[1])):
        w = sum(1 for v in vals if v > 0)
        print(f"  {reason:<18}{len(vals):>6}{len(vals) / len(trades) * 100:>7.1f}%"
              f"{sum(vals):>12.2f}{statistics.mean(vals):>10.2f}"
              f"{w / len(vals) * 100:>7.1f}%{statistics.mean(bars_by[reason]):>10.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="XAUUSD,FixedVol100,BTCUSD")
    ap.add_argument("--bars", type=int, default=50000)
    ap.add_argument("--pair", default="1", help="Camarilla pair: 1..4")
    ap.add_argument("--struct-back", type=int, default=STRUCT_BACK)
    ap.add_argument("--grace-minutes", type=int, default=GRACE_MINUTES)
    ap.add_argument("--no-struct", action="store_true",
                    help="Disable the structural stop entirely")
    ap.add_argument("--no-donchian", action="store_true",
                    help="Remove the Donchian trigger and the opposite-break exit")
    ap.add_argument("--no-entry-colour", action="store_true",
                    help="With --no-donchian, do not require the candle to agree")
    ap.add_argument("--grace-bars", type=int, default=GRACE_BARS)
    args = ap.parse_args()

    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    try:
        for symbol in args.symbols.split(","):
            res, meta = run(symbol, args.bars, args.pair,
                            struct_back=args.struct_back, grace_bars=args.grace_bars,
                            grace_minutes=args.grace_minutes,
                            use_struct=not args.no_struct,
                            use_donchian=not args.no_donchian,
                            entry_needs_colour=not args.no_entry_colour)
            report(symbol, res, meta)
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
