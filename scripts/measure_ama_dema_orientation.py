"""Which side of AMA-vs-DEMA is the uptrend?

`GoldCamarillaBreakout.mq5` gates entries on the two lines: a BUY needs DEMA
above AMA, a SELL needs DEMA below it. It shipped INVERTED - requiring AMA above
DEMA to buy - on the reasoning that Kaufman's AMA speeds up in a directional
move and should therefore lead. This script is what showed that reasoning to be
wrong, and it exists so the correction can be re-derived rather than trusted.

## The measurement that settles it is the concurrent one

The claim under test is about what the two lines MEAN at the same instant, not
about what they predict. So the test is: during bars where price is objectively
rising, which line is on top?

    uptrend = close > close twenty bars earlier

That is a crude definition and deliberately so - anything more elaborate would
be a second hypothesis smuggled in beside the first. On H1 closes it gives:

    symbol         UPTREND bars          DOWNTREND bars
                 DEMA>AMA  AMA>DEMA    DEMA>AMA  AMA>DEMA
    FixedVol100     82.5%     17.5%       20.1%     79.9%
    BTCUSD          79.8%     20.2%       22.7%     77.3%
    XAUUSD          83.2%     16.8%       22.8%     77.2%

Four times out of five, every symbol, both directions.

## The forward-return test was nearly used instead, and would have misled

`--forward` reports mean forward returns after each state. Those differ by
single-digit basis points and point in different directions on different
symbols and horizons - on FixedVol100 the means favour DEMA-above while the
hit-rates favour AMA-above. Reading that table first, it is easy to conclude
"inconclusive, leave it alone", which would have left an inverted gate live.
It is kept here as a record of what a noisy answer to the wrong question looks
like beside a clean answer to the right one.

## Why DEMA is the faster line

DEMA removes most of a conventional EMA's lag, and at period 14 tracks price
closely. AMA only reaches its fast constant when the efficiency ratio
approaches 1, which over a 9-bar window is rare; otherwise it decays toward the
slow leg, which at 30 is far behind. "Adaptive" does not mean "fast".

The indicators are reimplemented here rather than read through the terminal
because the MT5 Python API exposes no indicator handles. Both follow the
formulations MT5's iAMA and iDEMA document, so the comparison is of the same
arithmetic on the same bars.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

#: The expert's own defaults, so the measured lines are the ones that ship.
AMA_PERIOD, AMA_FAST, AMA_SLOW = 9, 4, 30
DEMA_PERIOD = 14
TREND_LOOKBACK = 20
HORIZONS = (1, 5, 10, 20)


def ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def dema(values: list[float], period: int = DEMA_PERIOD) -> list[float]:
    """2*EMA - EMA(EMA), the standard double exponential."""
    e1 = ema(values, period)
    e2 = ema(e1, period)
    return [2 * a - b for a, b in zip(e1, e2, strict=True)]


def ama(
    values: list[float],
    period: int = AMA_PERIOD,
    fast: int = AMA_FAST,
    slow: int = AMA_SLOW,
) -> list[float]:
    """Kaufman's adaptive moving average, as MT5's iAMA documents it."""
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    out = list(values)
    for i in range(1, len(values)):
        if i < period:
            continue
        direction = abs(values[i] - values[i - period])
        volatility = sum(
            abs(values[j] - values[j - 1]) for j in range(i - period + 1, i + 1)
        )
        er = (direction / volatility) if volatility > 0 else 0.0
        ssc = er * (fast_sc - slow_sc) + slow_sc
        out[i] = out[i - 1] + (ssc**2) * (values[i] - out[i - 1])
    return out


def closes(symbol: str, bars: int) -> list[float]:
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, bars)
    if rates is None or len(rates) < AMA_SLOW * 4:
        return []
    return [float(r["close"]) for r in rates]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="FixedVol100,BTCUSD,XAUUSD")
    parser.add_argument("--bars", type=int, default=20000)
    parser.add_argument("--forward", action="store_true",
                        help="Also print the forward-return table (the noisy one)")
    args = parser.parse_args()

    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")

    try:
        print(f"Given price IS objectively rising or falling (close vs "
              f"{TREND_LOOKBACK} bars back), which line is on top?\n")
        print(f"{'symbol':<13}{'UPTREND bars':>24}{'DOWNTREND bars':>24}")
        print(f"{'':<13}{'DEMA>AMA':>12}{'AMA>DEMA':>12}"
              f"{'DEMA>AMA':>12}{'AMA>DEMA':>12}")

        for symbol in args.symbols.split(","):
            close = closes(symbol, args.bars)
            if not close:
                print(f"{symbol:<13} not enough history")
                continue
            a, d = ama(close), dema(close)
            warm = AMA_SLOW * 3

            up_d = up_a = dn_d = dn_a = 0
            for i in range(warm + TREND_LOOKBACK, len(close)):
                on_top = d[i] > a[i]
                if close[i] > close[i - TREND_LOOKBACK]:
                    up_d += on_top
                    up_a += not on_top
                else:
                    dn_d += on_top
                    dn_a += not on_top
            up, dn = up_d + up_a, dn_d + dn_a
            if not up or not dn:
                continue
            print(f"{symbol:<13}{up_d / up * 100:>11.1f}%{up_a / up * 100:>11.1f}%"
                  f"{dn_d / dn * 100:>11.1f}%{dn_a / dn * 100:>11.1f}%")

        if not args.forward:
            return 0

        print("\nForward returns, basis points - the NOISY test. Kept as a record "
              "of\nwhat the wrong question looks like; it does not settle anything.\n")
        for symbol in args.symbols.split(","):
            close = closes(symbol, args.bars)
            if not close:
                continue
            a, d = ama(close), dema(close)
            warm = AMA_SLOW * 3
            cols = "".join(f"{'+' + str(h) + 'b':>10}" for h in HORIZONS)
            print(f"\n{symbol}   {len(close)} H1 bars")
            print(f"{'state':<18}{'bars':>8}{cols}")
            for label, want_dema_above in (("DEMA above AMA", True),
                                           ("AMA above DEMA", False)):
                rows: dict[int, list[float]] = {h: [] for h in HORIZONS}
                for i in range(warm, len(close) - max(HORIZONS)):
                    if (d[i] > a[i]) != want_dema_above:
                        continue
                    for h in HORIZONS:
                        rows[h].append((close[i + h] - close[i]) / close[i] * 10000)
                n = len(rows[HORIZONS[0]])
                if not n:
                    continue
                cells = "".join(f"{statistics.mean(rows[h]):>10.1f}" for h in HORIZONS)
                print(f"{label:<18}{n:>8}{cells}")
    finally:
        mt5.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
