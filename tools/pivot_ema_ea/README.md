# PivotEmaCascade.mq5 — the cascade as an MT5 Expert Advisor

The same rule as `algo/strategy/pivot_ema_cascade.py`, written in MQL5 so it can
run inside MetaTrader 5 — on a chart, in the Strategy Tester, or on an account.

* **SELL** — a candle closes down through **R3, R2, R1 or the pivot**, then down
  through the **10, 20, 50, 100** and finally the **200 EMA** (after, or on the
  same candle). The close beyond the 200 EMA is the entry.
* **BUY** — the mirror, arming on **S3, S2, S1 or the pivot**.
* **EXIT** — the first candle that closes back beyond **both** the 10 and 20 EMA.

Pivots are *Pivot Points Standard* with the type set to **Fibonacci**, from the
previous daily bar: `P = (H+L+C)/3`, then `P ± ratio × (H−L)` for `0.382`,
`0.618`, `1.000`.

## Read this before you attach it to a live account

**This rule has been measured twice and showed no edge either time.**

| | | |
|---|---|---|
| XAUUSD, 3 months M5 | D-157 | 55 trades, **−3,399** per 1.00 lot |
| BTCUSD, D-140's three windows | D-158 | 339 M5 trades, **+4,538** per 1 BTC — which became **−7,078** when the trading *day* was re-cut from 17:00 New York to plain UTC |

That second line is the one this EA cannot engineer around. The pivots come from
the previous **daily bar**, so they depend on when your broker ends its day —
Vantage's server runs GMT+2/+3, and it moves with DST. A different broker, or the
same broker after a clock change, draws different R and S lines from the same
market. On both instruments tested, moving that boundary flipped the result's
sign. That is a property of the rule, not a bug in the code.

So **`InpEnableTrading` defaults to `false`** *on a live or demo chart*. The EA
runs, prints and alerts exactly as it would trade, and places nothing until you
turn it on.

**The Strategy Tester always trades**, whatever that input says. The switch
exists to keep an unmeasured rule off a real account, and the tester is not one
— applying it there produced a run with zero trades that looked like a broken
strategy rather than a switch left off.

## Install

1. In MT5: **File → Open Data Folder**, then `MQL5/Experts/`.
2. Copy `PivotEmaCascade.mq5` in.
3. In MetaEditor (F4), open it and press **F7** to compile.
4. Back in MT5, refresh the Navigator, then drag it onto an **M5** chart.
5. Allow algo trading in the toolbar *and* tick it in the EA's Common tab.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `InpEnableTrading` | `false` | Place real orders on a live/demo chart. Off = alerts and log only. **Ignored in the Strategy Tester**, which always trades. |
| `InpLots` | `0.01` | Volume per trade. |
| `InpStopLossPct` | `0.5` | Protective stop as a % of entry price. `0` = none. |
| `InpMagic` | `20260913` | So it only ever manages its own positions. |
| `InpSlippagePoints` | `30` | Max deviation. |
| `InpAlerts` | `true` | Popup / push on entry and exit. |
| `InpDrawPivots` | `true` | Draw the seven lines on the chart. |

The 0.5% stop is the project's usual default, and in testing it was close to
inert: 303 of 339 BTCUSD trades and 53 of 55 XAUUSD trades exited on the 10/20
EMA rule instead. On M5 the 10 EMA sits far closer to price than a 0.5% stop.

## If a run produces no trades

Every run prints a funnel to the journal when it stops — how many cascades
reached each step, how many completed, and how many orders were actually placed:

```
PivotEmaCascade ---- where the cascades got to ----
  armed on a pivot line : 217
  through the  10 EMA   : 131
  through the  20 EMA   : 88
  through the  50 EMA   : 46
  through the 100 EMA   : 27
  through the 200 EMA   : 17
  signals               : 17
  orders placed         : 17
```

Read it top down:

* **`armed on a pivot line` is 0** — nothing ever started. Either price never
  closed through a pivot line in that range, or the previous **daily** bar was
  missing so no lines were drawn at all. Look for a `pivots from ...` line in the
  journal; if there isn't one, download more history (**Tools → Options →
  Charts → Max bars**, then open a D1 chart of the symbol) and re-run.
* **It armed but `signals` is 0** — the column where the numbers collapse is the
  step the market did not deliver. That is the rule being strict, not a bug.
* **`signals` > 0 but `orders placed` is 0** — on a live chart that is
  `InpEnableTrading=false`; otherwise search the journal for `REJECTED` (usually
  volume below the symbol's minimum, or a stop too close to price).

## Test it before you trust it

Use the Strategy Tester on **M5**, "Every tick based on real ticks", over a few
months. Compare the trade count and dates against what the Python study reports
for the same window — they should be close, and where they differ the reason
will be the broker's day boundary rather than the rule.

**Note:** MetaEditor is Windows-only, so this file has **not been compiled** —
it was written against the MQL5 reference and the Python strategy, not built and
run. If F7 reports anything, send me the compiler output and I'll fix it.

## What matches the backtest, exactly

These are the details that are easy to get subtly different, and each is
commented at the line where it happens:

* A crossing is **close-based**, never wick-based: `prev_close > level >= close`
  going down, mirrored going up.
* The EMA compared against is **this** bar's EMA, not the previous bar's.
* The exit tests "closed beyond both", not "crossed both on this bar" — a
  two-bar recovery is still an exit.
* **No cascade is counted while a position is open.** The next entry is a fresh
  pattern, not the tail of the one already on.
* A close back through the last level crossed **resets** that cascade, and it may
  re-arm on a pivot break on the same bar.
* Everything is decided on **closed** bars.

## This is not the Telegram monitor

`tools/pivot_ema_telegram_alert/` watches the same rule across XAUUSD, BTCUSD,
the broker's volatility index and the NIFTY 50, and sends messages without
touching a trading endpoint. It runs the *real* `PivotEmaCascade` through
`algo/backtest/signal_replay.py`, so it cannot drift from the study.

This EA is a second implementation, in a language that cannot import the first.
That is a real risk and the reason the list above exists — if the two ever
disagree on a signal, the Python one is the one the measurements describe.
