# MT5 expert advisors

Five MetaTrader 5 experts. Three are ports of the XAUUSD strategies in `algo/strategy/`;
the other two are not ports. **Every one that has been measured lost money** — see the
scalper callout below, D-151 for the fair value gap expert, and D-152/D-153 for EMA/BB.

| Expert | Ports | `strategy_for` name | Default magic |
| --- | --- | --- | --- |
| [GoldMacdCrossover.mq5](Experts/AlgoGold/GoldMacdCrossover.mq5) | [macd_crossover.py](../algo/strategy/macd_crossover.py) | `macd` | 20260901 |
| [GoldTrendlineBreakout.mq5](Experts/AlgoGold/GoldTrendlineBreakout.mq5) | [trendline_breakout.py](../algo/strategy/trendline_breakout.py) | `breakout` | 20260902 |
| [GoldIntradayScalper.mq5](Experts/AlgoGold/GoldIntradayScalper.mq5) | **nothing — terminal-side only** | — | 20260903 |
| [GoldFairValueGap.mq5](Experts/AlgoGold/GoldFairValueGap.mq5) | **nothing — terminal-side only** | — | 20260904 |
| [GoldEmaBollinger.mq5](Experts/AlgoGold/GoldEmaBollinger.mq5) | [ema_bb.py](../algo/strategy/ema_bb.py) | **not registered — D-153** | 20260906 |

> **`GoldEmaBollinger` has no measured edge either, and it is a port of a strategy that
> is deliberately absent from `strategy_for`.** D-152: `pullback` was below break-even in
> every window on M30 (PF 0.60 / 0.44 / 0.92); `breakout` cleared PF 1.0 in seven of nine
> cells but lost to doing nothing — buy-and-hold made $116,459 against its $69,458.
> D-153 pre-registered the one promising slice, long-only, and **rejected it** on seven
> years of unseen data: PF 1.04 on H1, 0.99 on M30, against buy-and-hold's +$195,893 at a
> third of the drawdown. It was built because it was asked for. Demo only.
>
> **20260905 is NOT free.** `GoldCamarillaBreakout` claims it and is not in this repo —
> its source lives on `claude/gold-trendline-breakout-es-21c6bc`. That is why this expert
> is 20260906 rather than the next number in sequence.

> **`GoldFairValueGap` has no edge — measured, not suspected.** Over D-140's three
> windows it armed 192 setups, entered 10, and closed 7 trades, **all losers, with not
> one reaching its target**. Replacing its liquidity target with a flat 2R triples the
> trade count and returns +$20 net across eighteen months — average R of +0.10 / -0.00 /
> -0.03. **Do not trade it, and do not tune it.** D-151 has the full result, the adverse
> selection in rule 6 that causes the 0% hit rate, and the two bugs the measurement
> found. `scripts/measure_fvg_xauusd.py` reproduces it.

The two ports share [ProtectiveExits.mqh](Include/AlgoGold/ProtectiveExits.mqh) — a port
of `price_stop.py` + `trailing_profit_stop.py` + the sequencing in `protective_exits.py`.
One shared exit module, for the same reason the Python has one: "the shared, tested piece
that adds it identically to both rather than two copies that could quietly drift apart."

All five share [Trader.mqh](Include/AlgoGold/Trader.mqh), the execution plumbing. The
scalper and the FVG expert additionally use
[ScalpFilters.mqh](Include/AlgoGold/ScalpFilters.mqh) — session window, daily governors,
the gate telemetry, and (scalper only) the ATR bracket with its cost gate.

**Include `ProtectiveExits.mqh` before `Trader.mqh`.** `Trader.mqh`'s `RebuildTrail`
takes a `TrailState`, which `ProtectiveExits.mqh` declares, so `Trader.mqh` does not
compile on its own. Getting the order wrong reports 35 errors *inside `Trader.mqh`* and
none in the expert being compiled, which is a confusing place to start looking. This
applies even to an expert that never touches the trail.

All five compile clean (0 errors, 0 warnings) against the standard library shipped with
the Vantage Markets MT5 terminal, build `X64 Regular`.

> **The scalper does not work — measured, not suspected.** Across three windows and
> ~5,900 trades (Jun–Aug 2026, Jan–May 2026, Jun–Dec 2025) it returns profit factor
> **0.91 / 0.87 / 0.73**, with drawdowns reaching **88.5%**. It is below break-even
> everywhere and worse out-of-sample. **Do not trade it.** D-140 has the full result and
> the reasoning for stopping rather than tuning. The surrounding machinery —
> `ScalpFilters`, the attached bracket, the daily governors, the gate telemetry — is
> sound and reusable; the entry rule is what fails.

---

## Install

The terminal only loads code from its own data folder. In MetaEditor use
**File → Open Data Folder**, or find it at
`%APPDATA%\MetaQuotes\Terminal\<instance-id>\MQL5`.

> **`%APPDATA%` only expands in `cmd.exe`.** In PowerShell it is a literal string, so
> `robocopy "...%APPDATA%\MetaQuotes\..."` silently creates a folder *named* `%APPDATA%`
> in the working directory and copies everything into it — the install appears to succeed
> and the terminal never sees the files. The commands below are PowerShell, which is the
> shell this project's other tooling assumes. Check for a stray `%APPDATA%` directory in
> the repo root if an expert fails to show up in MetaEditor.

```powershell
robocopy "D:\algo trading\mt5\Include\AlgoGold" "$env:APPDATA\MetaQuotes\Terminal\725B72F25E46C780EF59F57016D58156\MQL5\Include\AlgoGold" /E
```

```powershell
robocopy "D:\algo trading\mt5\Experts\AlgoGold" "$env:APPDATA\MetaQuotes\Terminal\725B72F25E46C780EF59F57016D58156\MQL5\Experts\AlgoGold" /E
```

Then in MetaEditor press **F7** on each `.mq5`, and in the terminal drag the expert onto
an **XAUUSD** chart with **Algo Trading** enabled.

`input group` needs terminal build 2340 or newer.

---

## Do not run any of these with magic 20260828

`algo/execution/mt5_broker.py` claims magic `20260828`, and that magic is the *only* thing
separating this system's orders from everything else on the account — MT5 overwrites the
comment field, so it cannot be used as a tag (D-122).

An expert sharing that number would be adopted by the Python reconciler as its own
position and managed accordingly. Each expert therefore ships a distinct magic, and
`GoldPreflight` refuses to start on `20260828`. If you run an expert alongside
`algo mt5`, they will correctly ignore each other's positions.

The registry, in full — `20260828` Python adapter, `20260901` MACD, `20260902` breakout,
`20260903` scalper, `20260904` fair value gap, `20260906` EMA/BB. **`20260905` is taken
by `GoldCamarillaBreakout`**, which is installed in the terminal but not committed here —
a magic can be claimed by an expert this README cannot see, so check the terminal as well
as this list. Two experts sharing a magic is the same failure as sharing the Python's:
each would net the other's tickets into its own position and manage them.

---

## Inputs — the two ports

Every default is the default of the same-named argument in the Python strategy class.
The scalper's inputs have no Python counterpart and are documented
[in its own section](#why-the-scalper-exists-and-what-it-is-up-against).

### Signal

| MACD expert | Breakout expert | Python |
| --- | --- | --- |
| `InpFast` 12, `InpSlow` 26, `InpSignal` 9 | — | `fast` / `slow` / `signal_period` |
| — | `InpLookback` 20 | `lookback` |
| `InpSeedBars` 1000 | — | no counterpart — see below |

### The on-chart panel

`GoldEmaBollinger` draws the same four blocks as `GoldCamarillaBreakout`, in the same
order, so running both does not mean relearning a layout per chart: **SIGNAL** (mode, the
EMA, the three bands, warmup), **POSITION** (side, floating, peak, trail, give-back),
**MARKET** (status, symbol/TF, spread) and **P&L** last — `NET TODAY` set large, split into
floating and realised beneath it, then 7 days and account equity.

Three things about it are deliberate:

- **It repaints on the tick, throttled to once a second**, not on the bar close. P&L,
  spread and floating are live numbers; on H1 a bar-close-only panel shows figures up to an
  hour stale while looking current, which is worse than showing nothing.
- **Every block above P&L writes a fixed number of rows** — the position block fills
  placeholders when flat rather than omitting rows — so the P&L figures never shift up or
  down. A number that moves around is a number that gets misread. `ClearFrom()` deletes
  anything below the rows actually written, so a shrinking layout cannot leave stale rows
  on screen looking live.
- **`InpPanelY` defaults to 112**, which clears MT5's one-click trading widget. At the old
  default the widget covered the first two rows.

`NET TODAY` is realised **plus** floating, from deal history filtered by symbol *and*
magic — profit, swap and commission all summed, since a "profit" that ignores the
commission it cost to earn is not what anyone means by the day's P&L.

> The panel needs `DASH_MAX_ROWS` of at least 24. It was 16, at which the whole P&L block
> was silently dropped off the bottom — the worst way for a panel to fail, because it still
> looked complete.

### Targets, alternative stop units, and the daily governors

`GoldEmaBollinger` also carries the optional exits the other experts have, resolved
**money > points > percent** — so setting `InpStopLossMoney` makes `InpStopLossPoints` and
`InpStopLossPct` inert. Money comes first because it is the only unit that still means the
same thing after `InpLots` changes; percent is last because it is what the Python
counterpart uses, and so what every measured study ran on.

- `InpTakeProfitPct` / `Points` / `Money` — a fixed target. **No Python counterpart, so no
  measured study in this repo covers it.** Off by default for that reason, not because it
  is known to be bad. Turning it on makes the expert a configuration nobody has measured.
- `InpStopLossPoints` / `InpStopLossMoney` — the same stop distance in other units.
- `InpBreakEvenPct` / `InpBreakEvenLockPts` — move the stop to entry plus a lock once a
  profit threshold is reached. It never pulls an armed trail backwards: the expert takes
  whichever stop is better, so break-even can only tighten, never give profit back. The
  lock exists so a "break even" does not settle as a scratch *minus* costs.
- `InpSessionStartHour` / `EndHour` / `InpCloseAtSessionEnd` — **server** hours, printed on
  init because guessing that is how a session filter ends up three hours out with nobody
  noticing. Equal values mean all day; a window may wrap midnight.
- `InpDailyLossLimit` / `InpDailyProfitTarget` / `InpMaxTradesPerDay` — these block
  **entries only**. An open position is never closed by them: a daily loss limit that also
  flattened would be a stop nobody chose, firing at a level set by the calendar rather than
  by the trade.

Every `.set` in this folder now writes all of these out explicitly, including the ones that
are off. **A `.set` that omits an input is not neutral** — MT5 falls back to the compiled
default, so a file predating a feature quietly enables whatever that feature ships with.

> The panel gained a GOVERNORS block, so "why has it stopped trading" is answered on the
> chart instead of in the log: session open/shut, entries allowed or HALTED, and trades
> today against the cap. That took it to **29 rows**, one past the 28-row ceiling — which
> would have dropped the `EQUITY` row and nothing else, the same silent failure as before.
> `DASH_MAX_ROWS` is now 34.

### Protective exits — percentages of price, **not points**

`InpStopLossPct` 0.5 · `InpTrailActivationPct` 2.0 · `InpTrailPct` 0.0 (off)

On XAUUSD near 4,600 a 0.5% stop is about $23, i.e. about 2,300 points. Do not read these
as pips.

`InpGivebackFrac` is the exception: it is a **fraction of the banked move**, not a percent
of anything. At 0.5 the position closes once it has handed back half of the best
unrealised profit it ever showed — the level sits midway between entry and peak and rises
with the peak. `InpTrailPct` gives back a slice of the *price*, so it scales with gold;
this gives back a slice of the *profit*, so it scales with how well the trade went. They
are separate rules and either, both, or neither may be on. Entering `50` meaning "50%" is
rejected at init rather than clamped.

**It is ON at 0.5 on `GoldEmaBollinger`, by request and against D-154**, with
`InpTrailActivationPct` raised from 0.25 to 2.0 in the same change — the difference between
the gate that measured as destructive and the one that measured as inert. At a 2% gate the
trail fired three times in fifty-three trades in the sharpest cell and finished $556 from
baseline; on an M1 chart it will essentially never arm, since 2% of gold near 4,400 is about
$88 and those round trips last minutes. It stays **0 — off — on every other expert.**
D-154 below is not withdrawn; the default simply no longer follows it.
`GoldFairValueGap` and `GoldIntradayScalper` have no such input at all: neither calls
`ProtectiveExitsCheck` (the scalper runs its own R-multiple ATR trail), so one there would
be dead.

`scripts/measure_ema_bb_giveback_xauusd.py` ran `EmaBollinger` in both modes across three
timeframes, three windows and four activation gates, each cell against its own
giveback-off baseline. The trail beat that baseline in **3 of 72 cells**, and all three are
degenerate — two are +$556 and +$806 where it fired three or four times out of 245 trades,
and the third merely loses less (−$17,309 against −$21,912) where both readings are heavy
losses. Everywhere else it is worse, often badly: H1 breakout over 2026.06-08 goes from
**+$37,725 at PF 1.54** to **−$70,636 at PF 0.10**.

The cause is arithmetic rather than fit. At `frac` 0.5 a trail armed at `a` first fires at
`a/2` of profit while the flat stop still lets a loser run to `InpStopLossPct` — at a 0.25%
gate against a 0.5% stop, a 1:4 reward-to-risk floor on every trade it touches. The results
are monotone in the gate for that reason: the wider it is set the closer to baseline it
lands, because it fires less. **Its best measured behaviour is not firing at all.**

The input is kept rather than removed so the falsification stays attached to the fix. A
give-back trail is only coherent when it arms well *above* the stop distance — and this
data says even then the middle band was already the better exit.

### Execution

`InpLots` is in **MT5 lots**, which is what the terminal shows. The Python engine sizes in
troy ounces (1 engine lot = 1 oz = 0.01 MT5 lots), so its default of 100 engine lots is
`InpLots = 1.00`. Both units are printed on init, because reading "100" as MT5 lots
instead of ounces is a hundredfold position error.

`InpMaxSpreadPoints` (default 0, off) blocks **new entries** when the book is abnormally
wide. It has no counterpart in the backtest, which charges a modelled flat $0.29 round
trip at every instant alike — so switching it on makes live diverge from the measured
numbers in a way the backtest cannot score. It never blocks an exit.

`InpAllowNewEntries = false` manages open positions and takes no new ones — the way to
wind an expert down without abandoning what it is already holding.

---

## What the port preserves exactly

- **The crossover rule** — `<=` then `>`, `>=` then `<`, on the MACD histogram, matching
  `Macd.crossed_up` / `crossed_down` and `tools/macd_telegram_alert`.
- **The channel excludes the bar being tested** — the Donchian range is built from chart
  shifts 2..`lookback`+1, never shift 1. Including today's own high would compare price
  against a range that already contains it.
- **Protective exits run before the warmup gate**, every bar a position is held. A held
  position must never go unprotected because the indicator that would eventually close it
  has not converged yet.
- **Exit order is stop, then trail**, with the trail's peak advanced first. A bar crossing
  both is reported as the stop.
- **The trail's "cost to cost" clamp** — an armed trail's level can never sit worse than
  entry, so its worst outcome is a scratch, never a loser.
- **Neither strategy reverses in one step.** Closing consumes the crossing/breakout event;
  re-entry waits for the next one. The Python calls this a real design choice costing
  roughly half of every reversal's timeliness, not an oversight, so it is preserved rather
  than quietly improved.
- **Decide on the closed bar, fill at the next price.** Nothing happens intrabar except a
  broker-side stop firing.

## What the port deliberately changes, and why

**It does not use `iMACD()`.** The built-in seeds its EMAs with an SMA of the first
`period` values; `algo/pricing/indicators.py` seeds with the *first value*
(pandas `adjust=False`, which is what the alert tool and TradingView use). Those differ,
and a signal here disagreeing with an alert there about what a crossover is would defeat
the point. The three EMAs are computed in the expert, recursively, in the same order and
the same 64-bit floating point.

**Indicator state is seeded from history, not persisted.** The Python persists its EMAs
because reseeding from zero would spend `warmup_bars()` bars re-converging — worst exactly
during a restart with a position open. An expert is reloaded far more often (recompile,
chart change, terminal restart), so it replays `InpSeedBars` closed bars forward on every
init instead: deterministic, no state file, and the seeding error decays geometrically —
for the 26-period EMA `alpha` is 0.074, so after 1,000 bars the residue is of order
`e^-77`, many orders of magnitude below a $0.01 tick.

**The trail's peak is replayed, not persisted.** `advance_trail` applied to every closed
bar from the one the position opened in through the last closed one — identical arithmetic
to the Python's persisted peak, and a reload cannot silently drop a trail that was already
armed mid-trade.

**The stop is a real broker-side order.** `price_stop.py` checks the bar's low/high rather
than its close *precisely because* it is standing in for a broker-side stop firing
intrabar. Live, we can place that order, so model and reality agree by construction rather
than by approximation. The bar-close check is kept as a backstop for the bar where the
stop could not be placed (freeze band, rejected modify, a position adopted at init); it
closes at market instead.

One SL slot has to express up to three levels, so it carries whichever is nearest to
price. Once armed, a trail is always nearer than the flat stop — the cost-to-cost clamp
puts it at or above entry for a long, while the flat stop is always below — which is the
same ordering `ProtectiveExitsCheck` enforces.

Between the two trails the ordering is by level and not by precedence. The flat stop wins
ties by fiat, because a bar's OHLC does not say whether its high or its low printed first
and pessimism is the safe tie-break. The trails have no such ambiguity: both sit at or
above entry once armed, and price reaches either only by retreating from the peak, so it
crosses the nearer one first. That is the one reported — and the reported kind is what
`algo/backtest/cfd_runner.py` prices the exit from, so naming the wrong one would be a
wrong P&L figure rather than a wrong label.

**Hedging accounts are netted.** `positions_get()` returns an independent ticket per
trade while both strategies reason about one signed net position, so tickets are
aggregated into a signed volume and a volume-weighted entry — the arithmetic
`Position.average_price` does. Two opposing tickets netting to zero still pay financing
and still hold spread, so that case is logged rather than hidden.

---

## One known divergence, stated rather than hidden

In `macd_crossover.py`, when a protective exit fires, `on_bar` returns **before**
`self._prev_histogram` is assigned. The next bar therefore compares against the histogram
from *two* bars ago, not one. `TrendlineBreakout` has no equivalent, since its channel is
recomputed from scratch each bar.

`GoldMacdCrossover.mq5` reproduces this exactly, because matching the measured backtest
matters more than tidying it in the port. It is controlled by
`ALGOGOLD_MATCH_PY_STOP_PREV_HISTOGRAM` at the top of the file. **Decide it in the Python
first** — changing it only here would make the expert and the backtest disagree about what
a crossover is, which is the one thing this port exists to prevent.

---

## Why the scalper exists, and what it is up against

`GoldIntradayScalper` is not a port. Nothing in `algo/strategy/` corresponds to it, so
there is no backtest it has to agree with — and equally, none standing behind it.

**It operates in the regime the measurements below say lost money.** The M15 column is
−$230,052 for MACD and −$14,779 for breakout, and the stated cause is not the signal:
trade count roughly halves per step to a slower interval while the $0.29 round-trip
spread is charged *per round trip*. A scalper trades more often than the column that
lost. That is the honest framing, and it is why the expert's design puts essentially all
of its engineering into the cost side rather than the signal side.

Three commitments follow from it, and each is a deliberate departure from what the two
ports do:

1. **The cost gate is mandatory and on by default.** `InpMinTpSpread` (default 4.0)
   refuses any trade whose target does not clear the *current* spread by that multiple.
   `GoldMacdCrossover` has `InpMaxSpreadPoints` default **off**, reasoned as "enabling it
   makes live diverge from the backtest in a way the backtest cannot score." There is no
   backtest here to diverge from, and the measured numbers say this guard is the whole
   game. Setting it to 0 turns it off and logs a warning saying so.
2. **The chop filter is the signal's main job.** `InpMinSepAtr` (default 0.25) refuses to
   trade while the two EMAs are tangled. Scalpers rarely die on one bad trade; they die on
   forty round trips through a flat market, each paying the spread. That is the state this
   input exists to refuse.
3. **The day is bounded, not just the trade.** A realised loss limit, a profit target and
   a trade cap. All three are recomputed from deal history every bar rather than held in
   memory, so a recompile cannot hand back a budget already spent — the same reasoning
   that makes the trail replayed rather than persisted.

### The signal

All reads are from closed bars. Long:

| | |
| --- | --- |
| regime | `emaFast > emaSlow`, separation at least `InpMinSepAtr × ATR` |
| pullback | RSI at or below `InpRsiPullback` on the previous bar |
| resume | RSI above it on this bar |
| confirm | the bar closed above `emaFast` |

Short is the mirror, with the band at `100 − InpRsiPullback`. It buys a dip inside an
established uptrend at the moment the dip stops — not a breakout and not a reversal. The
confirm clause is what keeps it off a falling knife: RSI can turn up on a bar that still
closed below the fast EMA, and that bar is a pause in a decline, not the end of a pullback.

`InpRsiPullback` is rejected at or above 50, because the short band is derived as
`100 − it` and overlapping bands would fire both sides on one bar.

### The bracket goes out with the order

`OpenBracket()` attaches SL and TP to the *same* request rather than opening and then
calling `ApplyStop()`. Between those two calls a position exists with no stop on it, for
the span of a server round trip — which is exactly when the fast move that motivated the
entry is still moving. A swing stop of $23 tolerates that; a scalp stop of a few ATR-tenths
does not. The broker takes the whole bracket or rejects the whole order.

The consequence is that **SL/TP is the primary exit**, not the bar-close logic. The
bar-close pass mostly manages a position the broker may already have closed intrabar.

### Distances are ATR multiples, not percentages

`ProtectiveExits.mqh` is anchored in percent of price because the Python it ports is. Its
0.5% default is about $23 on XAUUSD near 4,600 — a swing stop. A scalp cannot express its
risk in that unit, so `ScalpFilters.mqh` is ATR-relative throughout. The two modules are
not interchangeable and are deliberately **not** merged.

`BuildBracket` applies three constraints in a fixed order: the broker's
`SYMBOL_TRADE_STOPS_LEVEL`, then `InpMinStopPoints`, then the cost gate. Order matters —
the first two *widen* the stop and therefore the target, so the gate must test the final
target, not the requested one. The gate **rejects** rather than adjusts: widening a target
until it clears the spread would quietly convert a scalp into a swing trade still carrying
a scalp's stop, which is the worst of both.

### Sizing

`InpUseRiskSizing` (default on) sizes each trade from `InpRiskPercent` of balance against
that trade's ATR stop, converted through `SYMBOL_TRADE_TICK_VALUE` / `SYMBOL_TRADE_TICK_SIZE`
— tick value is already in the *account* currency, whereas going via contract size is
correct only while quote and account currency coincide.

Two behaviours worth knowing. If the computed size falls below `volume_min`, the trade is
taken at `volume_min` and the log states plainly that it risks **more** than asked. If the
symbol reports no usable tick value, sizing returns zero and the entry is **skipped** —
there is deliberately no fallback lot size, because a sizing failure must not become a
position.

### Nothing is persisted

The risk unit `R` is recovered from the TP the bracket actually placed
(`R = TP distance ÷ InpRewardRisk`), and the extreme for the trail is replayed from the
bars since entry. A recompile mid-trade therefore changes nothing about how the open
position is managed, and there is no state file to go stale.

### Out-of-sample: the result that ended it

The M1 numbers below were in-sample. Re-run on two windows it was never developed
against, with everything else identical:

| Window | Trades | PF | Net | Max DD |
| --- | ---: | ---: | ---: | ---: |
| 2026.06–08 *(in-sample)* | 1,261 | 0.91 | −$2,775 | 31.0% |
| 2026.01–05 | 1,898 | **0.87** | −$5,007 | 51.4% |
| 2025.06–12 | 2,739 | **0.73** | −$8,748 | **88.5%** |

Below break-even in all three, and degrading out-of-sample. That is not a tuning
problem — D-131 already found that optimising on this data fits noise. The entry rule
is what fails; the machinery around it is fine.

For context from the 76-EA screen on the same window: only **13 of 48** EAs that traded
cleared PF 1.0, and the 28 stock candlestick robots all landed between 0.61 and 0.85 —
so 0.91 was mid-field, not close to viable.

### What it actually measured on M1

XAUUSD M1, 2026.06.01–08.31, real-tick model, $10,000, 0.5% risk per trade,
shipped defaults. Both columns are the same run with position management toggled:

| | breakeven + trail ON | OFF |
| --- | --- | --- |
| Trades | 1,261 | 1,223 |
| **Net** | **−$2,775.13** | **−$2,641.42** |
| Profit factor | 0.91 | 0.92 |
| Expected payoff | −$2.20 | −$2.16 |
| Win rate | 47.82% | 42.4% |
| Avg win / avg loss | 0.99 | 1.25 |
| Max drawdown | 31.02% | 28.96% |

Gate telemetry, first column: 3,451 signals fired, 2,086 blocked by the session
window, 104 by cooldown, **0 by the cost gate**, **0 by the spread guard**, 1,261 taken.

Three things that says, none of them encouraging:

1. **It loses.** Profit factor 0.91 over 1,261 trades is not noise around break-even.
2. **The position management is roughly neutral, not harmful.** Turning breakeven and
   the trail off restores the reward:risk from 0.99 to 1.25 but drops the win rate from
   47.8% to 42.4%. The effects cancel — profit factor moves 0.91 → 0.92.
3. **Costs are the decisive term, exactly as D-124 said.** With management off the
   realised reward:risk is 1.25 against a bracket *designed* at 1.5. At a 42.4% win rate
   break-even needs 1.358, and the designed 1.5 would give a profit factor near 1.10.
   The gap between 1.5 and 1.25 is the spread, and it is what turns a marginally
   positive system negative.

The cost gate refused nothing, and that is not a gate failure. `InpMinStopPoints = 80`
floors the stop at 0.80, so the target is 1.20 against a ~0.22 spread — over the 3×
threshold every time. The gate exists to catch a target that is *small relative to
spread*; it cannot catch a system whose edge is merely thinner than its costs.

**These numbers have not been tuned and should not be.** D-131's finding was that
parameter optimisation on this data fits noise, and this is one instrument over one
90-day window.

### Beware the Strategy Tester's input cache

The tester reuses the last input set it saved for an expert
(`MQL5\Profiles\Tester\<Expert>.set`) in preference to the compiled defaults. After
changing a default in the `.mq5`, a re-run silently uses the **stale** value for every
pre-existing input while newly added inputs take their compiled value — a hybrid
matching neither the source nor any set file. Delete that `.set` (and the
`<Expert>.<Symbol>.<Period>.*.ini` beside it) before trusting a run. The expert prints
its full configuration on init precisely so this is visible in the log.

### Suggested starting point, and what to do with it

The shipped defaults are M1 defaults, and M1 is where it was measured — badly. Do not put
this on a demo account expecting the numbers to improve. `InpDailyLossLimit` still ships at
0 and needs setting to something you would actually be willing to lose in a day before the
expert runs anywhere.

Use **"Every tick based on real ticks"**, not "1 minute OHLC" — a model that interpolates
inside the bar cannot tell you whether a bracket a few ATR-tenths wide was hit stop-first
or target-first, which is the entire question. Note that this broker retains only about
five days of XAUUSD tick history (`real ticks begin from …` in the tester log); everything
earlier is synthesized from M1 bars even when the real-tick model is selected, so a long
window is not as honest as the model name suggests.

`algo significance` and the walk-forward exist for the same reason they do for the ports:
D-131's finding was that parameter optimisation on this data fits noise, and a scalper has
more parameters, not fewer.

If you want to keep going with this, the productive direction is not sweeping these
inputs. It is finding an entry whose *designed* reward:risk survives the ~0.22 spread —
which on M1 means either a materially higher win rate or a wider target, and a wider
target is a slower timeframe wearing a scalper's name.

`GoldTimeframeNote` will warn every time you attach this below M15. That warning is
correct and is left in deliberately.

---

## What has actually been measured — read this before sizing

**This section is about the two ports.** The scalper has no measured numbers at all.

These are the Python's own numbers on 2.11 years of real XAUUSD bars against real
Vantage costs, common-window so only the bar interval differs (D-124, D-125, D-127).
Net P&L, 1 MT5 lot fixed:

| | M15 | M30 | H1 |
| --- | --- | --- | --- |
| **MACD**, no stop | -$230,052 | $97,653 | $190,186 |
| **MACD**, 0.5% stop | -$77,668 | $77,836 | $132,919 |
| **Breakout(20)**, no stop | $50,983 | $102,207 | $162,298 |
| **Breakout(20)**, 0.5% stop | -$14,779 | $52,467 | $136,477 |
| either, 2%/0.5% trail with **no** flat stop | negative on all six cells |

Four things those rows say, none of them comfortable:

1. **Timeframe is the dominant term, and it is a cost effect.** Trade count roughly halves
   per step to a slower interval while the $0.29 round-trip spread is charged per round
   trip. That is why `GoldTimeframeNote` warns below M15.
2. **The 0.5% stop is not uniformly good.** It rescues the worst case (MACD M15) and makes
   every previously-positive row *worse*, flipping breakout M15 from +$50,983 to
   -$14,779. A stop bounds the worst case; it does not come free on a strategy whose own
   exit was already doing useful work. It stays on by default here because an expert
   running unattended with no downside bound is the worse hazard, but that is a live-risk
   judgement, not a measured improvement.
3. **Do not run the trail with the flat stop off.** Every one of six cells measured
   negative, several worse than any other configuration — winners cut short while losers
   run unbounded. `InpTrailPct` defaults to 0 for that reason. Whether a flat stop *and* a
   trail together beat either alone is an open question the measurements do not answer.
4. **This is one instrument over one window, and it is gold's own trending period.**
   Buy-and-hold returns about $200k on the same window. A trend-following signal doing
   well while the underlying trended is not distinguishable, from a single run, from
   genuine edge. `algo significance` and the walk-forward exist for exactly this, and
   D-131's walk-forward result is that the parameter optimisation is fitting noise.

No expert here has traded a live account. `Mt5Broker` in the Python has never placed an
order either, which is why `algo mt5` runs the paper path. Use the Strategy Tester on real
ticks, then a demo account, before anything else.

---

## `GoldFairValueGap` — where the rules came from, and what they are worth

**This is the fourth expert, and the second one that is not a port.** It has never been
backtested. What follows is provenance and specification, not evidence.

### The source

The rules were transcribed from the published description of *"Best Prop Firm GOLD
Strategy 2026 (High Win Rate XAUUSD Setup)"*, RBI FOREX, 28 Jun 2026,
`youtube.com/watch?v=WokhegaZ5WM`. That channel had 479 subscribers and the video 502
views; it is monetised through Vantage / XM / Exness affiliate links and a Telegram
channel. **Its "high win rate" claim is unverified marketing and is recorded here as a
claim, not a finding.**

What the source is actually good for is that its rules are *mechanical*. They can be
written down, coded, and measured. Only the first two of those have happened.

The six rules, as published:

1. Identify a completed 4-hour candle.
2. Wait for the next 4-hour candle to close above the previous candle's high (buy) or
   below its low (sell).
3. A valid setup requires a Fair Value Gap created **during** the breakout.
4. Wait for price to retrace and tap into the imbalance zone.
5. Use 15-minute displacement as confirmation before entering.
6. Target the previous 4-hour buyside/sellside liquidity; move the stop to break even
   at 1:1.

### What the source left undefined

Three things the rules need in order to be code at all. Each is an input, and each is a
choice **the source did not endorse** — the place to start if the backtest disappoints.

| Gap in the source | What this expert does instead | Input |
| --- | --- | --- |
| "Displacement" is never defined | A closed M15 bar, in the trade direction, with a body ≥ `mult × ATR`, **closing back out of the zone** | `InpDisplaceAtrMult` 0.60 |
| The stop loss is never given at all — only a break-even rule | Beyond the far edge of the imbalance, plus a buffer. Through that edge, the gap is filled and the premise is gone, so "stopped out" and "setup was wrong" become the same event | `InpStopBufferAtrMult` 0.25 |
| "Previous buyside liquidity" is not a formula | Extreme of the `InpLiquidityLookback` structure bars **strictly before** the breakout bar — the same exclusion the Donchian channel makes | `InpLiquidityLookback` 12 |

### What it adds that the source does not mention

A minimum reward:risk (`InpMinRewardRisk` 1.5), a session window, and the same daily
governors the scalper uses. The source has no risk framework beyond the break-even move.
`InpMaxTradesPerDay` defaults to 3 — the setup is meant to be rare, and a rule that fires
often on H4 is a rule that has been mis-implemented.

### Behaviours worth knowing before reading the log

- **One position at a time.** No pyramiding, no reversal in one step.
- **Bar-close decisions only.** Break-even is evaluated on M15 closes, not ticks. A wick
  that touches 1R and retraces inside the same bar does not arm it. This is deliberate:
  the alternative depends on tick density and cannot be reproduced by a backtest.
- **Transient gates leave the setup armed; structural ones clear it.** Session, daily
  governor and spread are transient. A target behind price, or a reward:risk under the
  floor, is structural — waiting does not make those true.
- **A new break supersedes an armed setup** rather than queueing behind it. Two live
  setups would mean two contradictory biases.
- **`GateStats` is reused, not forked.** Two of its rows always read zero here — there is
  no cooldown gate and no spread-multiple cost gate. The "cost gate" row counts
  reward:risk rejections instead.

### What the measurement said

`scripts/measure_fvg_xauusd.py` reimplements these rules against real MT5 bars with
D-121's costs. It does **not** run the compiled `.ex5` — the Strategy Tester needs the
terminal closed, and the terminal is forward-testing on demo. The divergences are listed
in the script's docstring.

| Window | Setups | Confirmed | Entered | Trades | PF | Net | Targets hit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026.06–08 | 47 | 15 | 2 | 1 | 0.00 | -$65 | 0 |
| 2026.01–05 | 56 | 23 | 2 | 1 | 0.00 | -$121 | 0 |
| 2025.06–12 | 89 | 32 | 6 | 5 | 0.00 | -$184 | 0 |

**Rule 6 is what makes it unusable.** Its target is the extreme of the 12 H4 bars before
the breakout, which after a breakout is usually far away — so `InpMinRewardRisk` rejects
the *close* targets and keeps the distant ones. The gate selects for targets least likely
to be reached, which is why the hit rate is zero rather than merely low.

**Removing it reveals a coin flip, not an edge.** Same entries and stops, target set to a
flat 2R: 8 / 10 / 18 trades at PF 1.91 / 0.74 / 0.95, +$20 net across all three windows,
average R of +0.10 / -0.00 / -0.03. The 1.91 is eight trades and means nothing.

Still worth running in the Strategy Tester on **M15** if you want the `.ex5` itself
confirmed (the chart period sets modelling granularity, so an H4 chart tests something
else). But the rules have already answered the question the tester would ask.
