# Backtest history

Every strategy this project has measured, what it returned, and what the record
says when read as a whole. Compiled 2026-09-04, covering D-105 through D-147.

`decisions.md` is the primary source and the place to go for the reasoning
behind any single result; each row here names the D-entry it came from. This
file exists because the entries are chronological and the *pattern* across them
is not visible from any one of them. Nothing here is new evidence.

**How to keep it current:** a new measurement gets its D-entry as usual, and a
row here. If a result contradicts one of the four patterns below, that is the
most interesting thing that could happen and it should be said in the pattern's
own section rather than quietly added as a row.

---

## The record

### Phase 1 — MCX options (D-105 to D-108)

No result. The supplied bhavcopy could not measure the short-strangle strategy
at all: the exit lag exceeded the target, so the data could not resolve the
thing being tested. Recorded as a measurement failure rather than a number,
which is the precedent the rest of this file follows.

### Phase 2 — XAUUSD in Python, against real measured costs

Run by `scripts/measure_macd_xauusd.py` over MT5 history, with the Vantage cost
stack from D-121: $0.29 round-trip spread, zero commission (verified against 54
real deals), financing charged nightly. One MT5 lot fixed, no risk scaling.

| # | what was measured | window | result |
|---|---|---|---|
| D-123 | `MacdCrossover`, M5, no stop | 50,000 bars, 2025-12-12 → 2026-08-28 | 1,952 trades, 32.3% wins. Gross **+$12,271**, spread **-$56,608**, swap +$4,541 credit → net **-$39,796** |
| D-124 | `MacdCrossover`, no stop | 2.11 yr common window | M15 **-$230,052** · M30 +$97,653 · H1 +$190,186 |
| D-124 | `TrendlineBreakout(20)`, no stop | same | M15 +$50,983 · M30 +$102,207 · H1 +$162,298 |
| D-125 | both, 0.5% price stop | same | MACD -$77,668 / +$77,836 / +$132,919 · BO -$14,779 / +$52,467 / +$136,477 |
| D-126 | both, 1.0% price stop | same | MACD -$246,375 / +$44,001 / +$77,916 · BO +$131,882 / +$71,711 / +$79,593 |
| D-127 | both, 2%-armed 0.5% trail, no flat stop | same | **all six cells negative**, -$48,684 to -$406,480 |
| D-131 | walk-forward, Breakout H1, optimising channel length | 13 windows, 89 out-of-sample trades | in-sample $471,556 · **out-of-sample $31,288** · *never touching the parameter* **$91,075** |
| D-132 | parameter sweep tooling | — | built with a robustness verdict above the grid, because a heatmap invites reading off the greenest square, which is the D-131 mistake |
| D-148 | flat stop **and** trail together — the cell D-127 named and never ran | 2024-07-24 → 2026-08-28 | **all twelve trail-bearing cells negative.** The stop bounds losers as predicted (10 of 12 beat trail-only) and every cell the stop alone left positive turns negative |

Buy-and-hold over the D-124 window: **≈ +$199,700**. Over D-123's M5 window:
+$15,682.

### Phase 3 — MT5 experts, in the strategy tester

| # | what was measured | window | result |
|---|---|---|---|
| D-138 | `GoldIntradayScalper` built, M5 | 3 months | 2 trades. A four-way conjunction resting on a single-bar coincidence |
| D-139 | same, generalised, M1 | 2026.06.01–08.31 | 1,261 trades, net **-$2,775**; with breakeven+trail off, 1,223 trades, **-$2,641** |
| D-140 | all 76 installed experts screened | — | ours is dead; the one survivor rests on an assumption this broker cannot test |
| D-141 | the gold EAs on FixedVol100 and BTCUSD | — | no transfer, and **two of them were never reading the chart symbol** |
| D-142 | `ExpertMAPSAR` given a real bracket | 2026.06–08 | stock unbracketed: 51 trades, PF 1.69, +$118. Bracketed with pattern 0 on: 4,936 trades, PF 0.88, -$1,174. Pattern 0 off: 1,405 trades, PF 1.06, +$142 |
| D-142 | that same configuration, out-of-sample | 2026.01–05 and 2025.06–12 | PF **0.77** (-$296) and **0.81** (-$157) |
| D-143 | scale-in grid added to `GoldTrendlineBreakout` | 2026.06.01–08.31 | single position: 1,166 trades, PF 0.83, -$1,385, DD 14.6%, 39.5% wins → **with grid: 3,246 trades, PF 0.50, -$7,191, DD 73.7%, 55.7% wins** |
| D-144 | market EA `Gold Sniping`, five timeframes | 2026.07–08 | identical to the cent on all five (it never reads the chart period): 4,424 trades, +$3,458, PF 1.07, **87.7% wins**, equity DD 43.3% |
| D-145 | the same EA, long window | 2024.01 → 2026.08 requested | **account dead 2024-02-26**: 340 trades, -$10,145 on a $10,000 deposit, PF 0.49, balance DD 101.35% |
| D-146 | Asia value-area fade | 74 sessions, 2026-05-26 → 2026-09-04 | 55 trades, 45.5% wins, net **-$3,971**, PF 0.65. |t| = 1.22, so not distinguishable from zero |
| D-147 | the same, six stop rules × both bias directions | same | **twelve cells, twelve losses**, gross and net |

---

## What the record says

Four patterns repeat often enough to be treated as findings in their own right.

### 1. Cost is the dominant term at speed

D-123 states it most cleanly: gross P&L was **positive** and spread was **4.6
times larger**. The signal was doing something; trading it 1,952 times paid the
result away. Every fast strategy measured since has died the same way, and
D-124's timeframe ladder is the same fact seen from the other side — trade count
roughly halves per step to a slower interval while the spread is charged per
round trip, so net improves without the signal changing at all.

This is why D-138 recorded the tension *before* the scalper was written: a
scalper trades more than the column that already lost money. D-139 then measured
exactly that.

### 2. Win rate is the most misleading number in the record

Every time win rate rose on its own, the account did worse:

| | win rate | net |
|---|---|---|
| D-127 trailing exit | rose on all six rows | collapsed on all six |
| D-143 grid | 39.5% → **55.7%** | -$1,385 → **-$7,191** |
| D-144 martingale | **87.7%** | PF 1.07, then D-145's dead account |

The mechanism differs each time — winners cut short, losers averaged into,
baskets closed for $1 — but the signature is identical, and it is the one
statistic that can be improved by making the strategy worse.

### 3. Single windows lie

D-131 took the best strategy in the repo and showed that choosing its parameter
per window did **a third** as well as never touching it, with the chosen value
wandering across the whole grid. D-142 produced a positive out-of-sample-looking
cell that turned negative the moment two other windows were run. D-145 took two
profitable months and found the account death that had been sitting just outside
them.

Nothing in this project has ever been rescued by a longer look. Several things
have been destroyed by one.

### 4. Nothing has beaten doing nothing

The best honest result in the record — `TrendlineBreakout` at H1, +$162,298 —
was earned during gold's own bull trend, against a buy-and-hold return of
≈$199,700 on the same bars, and then failed walk-forward in D-131. Every other
positive cell is smaller, in-sample, or both.

Stated plainly: **across roughly twenty-five measurements, no strategy here has
shown an edge that survived costs, a window shift, and walk-forward together.**

---

## Questions the record has raised and never answered

Each of these was named in its own entry as the real next step, and each is
still open. They are listed because a question that has been asked and left is
easy to lose.

1. ~~**A flat stop and a trail together.**~~ **Answered by D-148: no.** The stop
   bounds the loser side exactly as D-127 predicted, and all twelve
   trail-bearing cells are still negative, because the trail's damage is to the
   small number of large winners the edge lives in. Closed, not parked — a
   different activation or trail distance would be a parameter search against
   one window.
2. **Stop distance chosen per strategy and timeframe.** D-125 and D-126 both
   identified this and both correctly declined it, to avoid fitting nine cells of
   one 2.11-year window. Doing it *inside* walk-forward makes the choice
   out-of-sample, which is the only version worth running.
3. **A different cost stack.** D-121's model is one account, one tier: spread-only,
   zero commission. A RAW/ECN account trades that for a tighter spread plus
   commission. Given pattern 1, that changes more than any signal edit would, and
   `CfdCosts` already accepts a commission model.
4. **More history.** Every intraday question is currently capped by data, not by
   ideas. This broker serves M1 back to 2026-05-26 only, which is why D-146 could
   reach 74 sessions and no more, and why it could not distinguish "no edge" from
   "sample too small". Free tick archives go back years.

---

## Where effort goes furthest

Ranked by information gained per unit of work, on the evidence above.

1. **Get more history** (open question 4). It is the binding constraint on
   everything intraday, it is infrastructure rather than strategy, and it turns
   the 74-session studies into something that can answer its own question.
2. ~~Run the untested exit combination.~~ **Done — D-148.** The code already
   existed on `run_cfd_backtest`; it closes the one configuration D-127
   explicitly left open.
3. **Make walk-forward a gate rather than an exhibit.** `cfd_walkforward.py` has
   been run essentially once and immediately falsified the best strategy here.
   D-138 built an expert in a regime the measurements already said loses.
   Requiring a walk-forward pass before any `.mq5` is written has the best track
   record in this repo of preventing wasted work.
4. **Attack the cost term** (open question 3). Pattern 1 says this moves more
   than signal work does, and it is a parameter study rather than new code.
5. **Choose stops inside walk-forward** (open question 2).

The Asia value-area fade is not on this list. D-147 closed the exit as a lever —
the deficit is in the entry — and any further change to it is another parameter
searched against the same 74 sessions. It waits for item 1.

---

## What these numbers do and do not include

Stated once here so no row above has to carry it.

- **Costs are real, not modelled away.** Spread on every leg, financing on every
  night carried, commission as measured. Where a spread profile was available the
  fill is charged what its hour of the week actually costs (D-121, `mt5_spread`).
- **Fills are pessimistic at the bar level.** A stop and a target inside the same
  bar resolve to the stop, because OHLC cannot order two prices within one bar.
  A triggered exit fills at its own level or the bar's open on a gap, never at
  `close ± spread`.
- **Position size is fixed**, one MT5 lot, never risk-scaled. Implied risk is
  reported rather than hidden (D-089).
- **Swap rates are today's, applied to the past.** MT5 publishes no historical
  series, so `SwapModel.is_verified` is False and stays False.
- **Tester history quality varies and is not uniform.** D-145's long window was
  0% real ticks and D-144's two-month window only 8%; both were synthesised from
  M1 bars, which flatters basket strategies specifically.
- **CFD volume is tick volume.** There is no traded volume on this feed, so any
  volume-derived level (D-146's value areas) is a distribution of quote updates
  wearing the same name.
