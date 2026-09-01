# MT5 expert advisors

Three MetaTrader 5 experts. Two are ports of the XAUUSD strategies in `algo/strategy/`;
the third is not a port and has no measured backtest behind it.

| Expert | Ports | `strategy_for` name | Default magic |
| --- | --- | --- | --- |
| [GoldMacdCrossover.mq5](Experts/AlgoGold/GoldMacdCrossover.mq5) | [macd_crossover.py](../algo/strategy/macd_crossover.py) | `macd` | 20260901 |
| [GoldTrendlineBreakout.mq5](Experts/AlgoGold/GoldTrendlineBreakout.mq5) | [trendline_breakout.py](../algo/strategy/trendline_breakout.py) | `breakout` | 20260902 |
| [GoldIntradayScalper.mq5](Experts/AlgoGold/GoldIntradayScalper.mq5) | **nothing — terminal-side only** | — | 20260903 |

The two ports share [ProtectiveExits.mqh](Include/AlgoGold/ProtectiveExits.mqh) — a port
of `price_stop.py` + `trailing_profit_stop.py` + the sequencing in `protective_exits.py`.
One shared exit module, for the same reason the Python has one: "the shared, tested piece
that adds it identically to both rather than two copies that could quietly drift apart."

All three share [Trader.mqh](Include/AlgoGold/Trader.mqh), the execution plumbing. The
scalper additionally uses [ScalpFilters.mqh](Include/AlgoGold/ScalpFilters.mqh) — session
window, daily governors and the ATR bracket with its cost gate.

All three compile clean (0 errors, 0 warnings) against the standard library shipped with
the Vantage Markets MT5 terminal, build `X64 Regular`.

> **The scalper is unmeasured.** Everything in the "what has actually been measured"
> section below is about the two ports. `GoldIntradayScalper` has never been backtested,
> because there is no Python counterpart to backtest it against — and the measured M15
> column is precisely the regime it operates in. Read
> [Why the scalper exists, and what it is up against](#why-the-scalper-exists-and-what-it-is-up-against)
> before you attach it to anything.

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
`20260903` scalper. Two experts sharing a magic is the same failure as sharing the
Python's: each would net the other's tickets into its own position and manage them.

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

### Protective exits — percentages of price, **not points**

`InpStopLossPct` 0.5 · `InpTrailActivationPct` 2.0 · `InpTrailPct` 0.0 (off)

On XAUUSD near 4,600 a 0.5% stop is about $23, i.e. about 2,300 points. Do not read these
as pips.

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

One SL slot has to express two levels, so it carries whichever is nearer to price. Once
armed, the trail is always the nearer one — the cost-to-cost clamp puts it at or above
entry for a long, while the flat stop is always below — which is the same ordering
`ProtectiveExitsCheck` enforces.

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

### Suggested starting point, and what to do with it

M5 on XAUUSD, defaults as shipped, `InpDailyLossLimit` set to something you would actually
be willing to lose in a day. Then **Strategy Tester on real ticks** — "Every tick based on
real ticks", not "1 minute OHLC", because a modelling mode that interpolates inside the bar
cannot tell you whether a bracket a few ATR-tenths wide was hit stop-first or target-first,
which is the entire question. Demo after that. `algo significance` and the walk-forward
exist for the same reason they do for the ports: D-131's finding was that parameter
optimisation on this data fits noise, and a scalper has more parameters, not fewer.

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
