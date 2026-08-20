# Milestone 0 — Plan (no code)

Status: **revised after your answers. Awaiting review.** Nothing below is implemented.

**Target system**

| | |
|---|---|
| Instrument | **GOLDM (Gold Mini, 100 g) options on MCX** — options *on futures* |
| Timeframe | 30-minute bars |
| Strategy | Short strangle: sell OTM call + OTM put, each nearest 0.25 delta |
| Broker | Live: **Kotak Neo** (orders, chain feed, live master) · History: **Angel One SmartAPI** (candle API, JSON master) |
| Holding | NRML, carried overnight |
| Sizing | Fixed lots |
| Data | **Recorded forward from now** — no usable history exists |

---

## 0. Corrections to my previous draft

Your answer "GOLDM MCX" invalidated part of what I wrote before. Recorded here rather than
quietly edited away.

1. **I was wrong to dismiss London/NY sessions.** I wrote that §2.6's kill zones "do not
   apply — single IST session". For MCX gold that is backwards. Gold's price discovery is
   COMEX/LBMA; MCX gold is close to a currency-adjusted derivative of international gold. The
   large moves land in the **MCX evening session**, which exists precisely to overlap the US
   session. Session filters are not merely applicable here, they are probably the most
   informative filter available. **§2.6 stands as written.**
2. **I was wrong about the session.** MCX non-agri commodities trade **09:00–23:30 or
   23:55 IST**, not 09:15–15:30. That is a 14.5-hour session, not 6.25.
3. **The DST trap in §2.6 is live, and it is US DST, not Indian DST.** MCX shortens the
   evening session to **23:30 IST during US daylight saving** (2nd Sunday of March → 1st
   Sunday of November) and runs to **23:55 IST** outside it. The 2026 change took effect
   9 March 2026. India has no DST, so a session calendar written only in `Asia/Kolkata`
   would be wrong for roughly eight months of every year. The calendar must key its close
   time off **`America/New_York` DST transitions** while expressing the session in
   `Asia/Kolkata`. This is exactly the failure your brief predicted.
4. **Charges are CTT, not STT.** Commodities Transaction Tax, different rates, different
   bases, different exchange. The NSE charge stack I drafted does not apply.
5. **Settlement is not cash.** See §2 below — this is the single biggest risk change.
6. **I over-stated the trade-count problem.** I flagged a Feb/Apr/Jun/Aug/Oct/Dec cycle
   giving ~6 cycles a year. That is the **GOLD (1 kg)** cycle. **GOLDM futures are monthly**,
   expiring on the **5th of each contract month**. So there are ~12 option cycles a year, not
   6. The concern is halved, not eliminated — see §1.4.
7. **I disputed the "last Friday" expiry rule and was wrong.** The terminal shows GOLDM
   options expiring **28 Aug 2026 — a Friday, and the last Friday of August**. My derived
   tender-period chain was wrong; the stated rule is right. See §1.3 and C-004.

---

## 1. What GOLDM options actually are, and why it changes the build

### 1.1 Options on futures — this *simplifies* the pricing layer
An MCX GOLDM option's underlying is the **GOLDM futures contract**, not spot gold. So
Black-76 applies directly with `F` = the underlying futures price. No synthetic forward, no
put-call-parity reconstruction, no dividend or cost-of-carry term, no risk-free rate needed
for the forward (only for discounting). `pricing/forward.py` collapses to a small
"resolve the option's underlying futures contract" mapping. One of the few things that got
easier.

### 1.2 Devolvement — the single biggest risk change
At option expiry, ITM options outside the Close-To-Money band **devolve automatically into a
futures position at the strike price**. CTM options (typically ATM ± 2 strikes) do not
auto-devolve and require explicit instruction.

For a short strangle this means: **a tested leg left open at option expiry hands you a GOLDM
futures position.** And GOLDM futures are **compulsory physical delivery** — 100 g of gold,
with delivery obligations and a tender period. This is a categorically different failure mode
from a cash-settled index option, where the worst case is a debit.

Consequences, all mandatory, not configurable off:

- A hard rule in `risk/devolvement.py`: **no short option may be carried into its expiry
  session.** Enforced by the risk layer, not by strategy discipline.
- A second hard rule: **no futures position (however acquired) may be carried into the tender
  period.**
- A devolvement-risk monitor that escalates as expiry approaches and any leg moves toward ITM.
- Both rules are tested, and both fire in backtest, paper and live identically.

### 1.3 Expiry: last Friday of the month — confirmed from the terminal

**GOLDM option chain, expiry 28 Aug 2026.** That date is a Friday and the last Friday of
August 2026. The stated rule holds; my derived tender-period chain was wrong (C-004).

`exchange/expiries.py` therefore uses:

```
option expiry (cross-check heuristic) = last Friday of the contract month
option expiry (SOURCE OF TRUTH)       = the Angel One instrument master
```

**D-023 stands, and this episode is the argument for it.** A derived rule was wrong here.
The system reads expiry from the instrument master — the same record that determines what
actually trades — and uses "last Friday" only to raise an alarm on mismatch. One confirmed
data point is one month; the recorder will validate the heuristic across many.

**Still open (Q1c): which futures contract underlies this option series, and when does *it*
expire?** The devolvement rules need it. An option expiring 28 Aug devolves into a GOLDM
futures position — in which contract, and how long until that contract's own tender period
begins? That window is how much time a devolved position would sit before delivery
obligations start. `exchange/expiries.py` models all three dates (option expiry, futures
expiry, tender start) precisely because the risk layer needs the gap between them.

### 1.4 Trade count — better than I first said, still thin
GOLDM futures are **monthly** (5th of each contract month), so there are **~12 option expiry
cycles a year**, not the ~6 I flagged earlier from the GOLD (1 kg) cycle.

One strangle per cycle is ~12 trades a year — two years of recording gives ~24 trades. Better,
but still too few to separate skill from luck with any confidence. Every metric in §10 carries
its sample size, and M5 walk-forward may still lack enough cycles to be meaningful. With your
TP/SL levels (§1.7) trades will close well before expiry, so **re-entry within a cycle (Q5) is
now the main lever on n** — probably worth turning on, deliberately.

### 1.7 Your TP/SL levels

**RESOLVED: "investment" = margin blocked.** TP = 2% of margin blocked at entry, SL = 1% of
margin blocked at entry, both combo-level, both resolved to an absolute ₹ figure once at entry
and frozen (D-025). `ComboExit.kind = PCT_OF_MARGIN_AT_ENTRY`.

I raised the cost concern below before you chose; you chose this basis with that in front of
you, so it is what gets built. The remaining analysis stays on the page because the
stop-viability check (D-024) is built on it, and because the recorder will replace my
placeholder spread estimate with a measured one within weeks — at which point we will know
the real ratio instead of arguing about my arithmetic.

**(a) The readings differed by 10×.** A short strangle receives
credit; you do not invest anything. The candidates, with illustrative figures (gold ~₹1,15,000
per 10 g ⇒ GOLDM contract value ≈ ₹11.5 lakh/lot; strangle margin ≈ ₹1 lakh/lot — **all
placeholders until the recorder gives real ones**):

| "Investment" means | TP (2%) | SL (1%) |
|---|---|---|
| Margin blocked for the position | ₹2,000 | **₹1,000** |
| Account equity (₹10 lakh) | ₹20,000 | ₹10,000 |
| Notional contract value | ₹23,000 | ₹11,500 |
| Premium received (~₹15,000) | ₹300 | ₹150 |

**Q4a — which one?** This is blocking: it changes the strategy by an order of magnitude.

**(b) The check that matters: is the stop bigger than the cost of trading?** Round-trip
friction on one strangle is 4 spread crossings (2 legs × in and out) plus 4 brokerage charges
plus CTT, exchange, stamp and GST. On a thin GOLDM option book the **spread dominates
everything else** — the taxes here are small change by comparison. If the spread is 10–30
ticks per leg (tick = ₹1/10 g = ₹10/lot), round-trip friction lands somewhere around
**₹500–1,500 per lot**.

Compare that to the stop:

- On the **margin** reading, SL ≈ ₹1,000 — **the same size as the cost of entering and
  exiting**. The position would sit at or near its stop the moment it filled. That is not a
  strategy, it is a cost machine.
- On the **equity** reading, SL ≈ ₹10,000 — roughly 10× friction, which is workable.

So the ambiguity in (a) is not cosmetic; one reading is untradeable by construction.

**Design consequence (D-024): a stop-viability check in the risk layer.** At startup and at
every entry it compares the configured stop against modelled round-trip cost and **refuses the
configuration** if the stop is under a configurable multiple (default 3×), logging the two
numbers. Better to fail loudly at startup than to discover it in the equity curve.

**(c) A note on the shape, which is your call, not mine.** TP 2% with SL 1% is a 2:1 payoff,
so it needs a win rate above ~33% to break even before costs. A short strangle's natural
distribution is the opposite — frequent small wins, rare large losses — because the credit is
earned slowly by theta while an adverse move hits immediately through gamma and vega. Setting
the target at twice the stop asks a premium-selling position to behave like a trend trade.
It may still work; the backtest will show the realised win rate and R-distribution, and that is
the number to judge it on. I am implementing exactly what you specified.

### 1.8 What the live chain tells us — first real numbers in this project

Read off the GOLDM chain dated 28 Aug 2026, captured 19 Aug 2026. **These are observations,
and the derived figures are estimates from LTP only — there is no bid/ask in the screenshot.**

**Directly observed**

| Fact | Value |
|---|---|
| Underlying (futures) | **₹1,56,640** per 10 g |
| Contract multiplier | 10 (100 g quoted per 10 g) ⇒ **notional ≈ ₹15.66 lakh/lot** |
| Strike interval | **₹500** |
| Tick size | **₹0.50** per 10 g ⇒ **₹5 per lot** (all LTPs land on .00/.50) |
| Days to expiry | **9** |
| Visible strike range | 155000 – 159000 (≈ ±1.3% around spot) |
| Missing quotes | 157500 PE and 159000 PE show `–`; **158500 absent entirely** — with "Tradeable only" ON |
| Today's move | calls +182% to +312%, puts −43% to −52% ⇒ a large up day in gold |

**Derived — ATM implied volatility**

156500 CE = ₹2,200.50. By put-call parity the 156500 PE ≈ 2200.50 − (156640 − 156500) ≈
₹2,060.50, so the ATM straddle ≈ **₹4,261**.

```
σ ≈ straddle / (0.8 × F × √T)
  = 4261 / (0.8 × 156640 × √(9/365))
  = 4261 / 19,679
  ≈ 21.6%
```

**Derived — where 0.25 delta actually sits** (Black-76, σ = 21.6%, T = 9/365, σ√T = 0.0340)

```
0.25Δ call:  ln(F/K) = −0.6745(0.0340) − ½(0.0340²) = −0.02351  ⇒  K ≈ 160,370  → 160,500
0.25Δ put:   ln(F/K) = +0.6745(0.0340) − ½(0.0340²) = +0.02236  ⇒  K ≈ 153,180  → 153,000
```

**Both 0.25-delta strikes fall outside the visible window.** For reference the 159000 CE
(₹1,239.50) prices at roughly **0.34 delta**, not 0.25 — so even the furthest strike on screen
is not far enough out.

**Why this matters**

1. **The strategy trades at the edge of the listed range.** Whether 153000 / 160500 are listed
   *and quoted* is now the central question (Q1d). The chain already shows `–` at two strikes
   and a missing 158500 **inside** a ±1.3% band, under a "Tradeable only" filter. That is
   empirical support for the liquidity concern, from the live book rather than my speculation.
2. **Longer DTE makes this worse, not better.** A fixed delta sits further from spot as T
   grows: at 30 DTE the 0.25Δ call moves out to roughly **163,700**. So entering early in a
   cycle pushes the strategy further into the illiquid tail.
3. **Today demonstrates the gamma exposure against a 1%-of-margin stop.** Calls rose ~200–310%
   in one session. A short call leg opened yesterday near ₹300/10 g and now at ₹1,240/10 g is
   **≈ ₹9,400/lot underwater** — against a stop of roughly ₹800–1,200/lot (1% of a
   ~₹80k–1.2L strangle margin). The stop is not a brake on a day like today; it is a level
   that gets jumped straight over. That is a gap-risk statement, not a criticism of the level.
4. **Sanity check on the exit levels.** At 0.25Δ the combined credit is roughly ₹1,700–1,800
   per 10 g ⇒ **~₹17,500/lot**. Theta over 9 days averages ~₹1,900/lot/day. So TP ≈ ₹2,000 is
   **about one day of decay**, and SL ≈ ₹1,000 about half a day. The trade is a short
   decay-capture, which is internally consistent — the open question remains whether the
   round-trip spread fits inside it (D-024).

**What is still unknown and only the recorder can answer:** the bid/ask spread at those
strikes. Every friction estimate in this document is a placeholder until then.

### 1.5 A risk no volatility model captures
MCX gold ≈ international gold × USDINR × unit conversion + **import duty**. A short strangle
on GOLDM is therefore short volatility on gold *and* implicitly exposed to:

- **USDINR** moves, which are not in the option's own implied vol in any usable way;
- **import duty changes**, which are policy events that produce a step change in MCX gold with
  no international counterpart, land without warning, and can move price further in a day than
  the option market prices for the whole cycle.

A duty change is precisely the event that destroys a short strangle. It is unmodellable from
price history, so it will not appear in any backtest unless one happened to fall inside the
sample. It gets stated in `docs/backtest-assumptions.md` §8 and in every report footer.

### 1.6 Session and bar arithmetic
| Period | Session (IST) | Minutes | 30-min bars |
|---|---|---|---|
| US DST (2nd Sun Mar → 1st Sun Nov) | 09:00 – 23:30 | 870 | **29 exactly** |
| Rest of year | 09:00 – 23:55 | 895 | 29 + a **25-minute stub** |

So the partial-bar problem exists for only part of the year, and the number of bars per day
changes twice a year. Both are handled by the calendar, and `SessionInfo.is_partial_bar` is
true only for the winter stub. Agri commodities close at 17:00 and are out of scope.

---

## 2. Brief reconciliation — §6 is an MT5 forex/CFD model; this is MCX commodity options

| §6 item as written | What it assumes | MCX / Angel One reality | Replacement |
|---|---|---|---|
| Spread: fixed / time-of-day / bid-ask replay; "rollover widening around broker midnight" | Dealer-quoted CFD spread | Exchange order book. **GOLDM option liquidity is thin** — spread is the dominant cost and cannot be inferred from OHLC. There is no broker midnight, but there is a real session boundary at 23:30/23:55 IST. | Spread taken from **recorded depth** (see §3). Widening modelled at the 09:00 open, around the US session open, and into the close. |
| Commission per lot per side | FX broker | Angel One charges per executed order. On top: **CTT**, MCX transaction charges, SEBI fee, stamp duty, GST. | `McxChargeModel`, itemised. **CTT is charged on the sell side of option premium and the writer pays it — that is us, on every single entry.** |
| Swap / financing, triple-swap Wednesday | Overnight FX financing | Does not exist. | Margin model: SPAN + exposure blocked, daily MTM settled in cash. |
| Contract specs, "stops level", "freeze level" | Static MT5 table | Lot 100 g, quoted ₹/10 g, so contract value = price × 10. Tick, strike interval, max order size and DPR (daily price range) circuit limits all set by MCX and revised over time. | `ContractSpecStore`, effective-dated, every entry with a `source`. **DPR circuits are modelled** — a locked circuit means no fill at any price. |
| Gaps fill at the open | Weekend/news gaps | Overnight gap is 09:30 (winter 09:05) hours every day, plus weekends. Gold gaps on Asian-session news while MCX is shut. | Gap is a first-class modelled event. **No stop is active across it.** |
| London / NY kill zones | FX sessions | **Directly applicable.** COMEX open and US data releases are where GOLDM moves. | Session filters via named zones, US-DST aware. My earlier dismissal of this was wrong. |
| §8 sizing formula | Instrument with a stop distance | You chose **fixed lots**. | `sizing.mode = fixed_lots`. Implied risk still reported per trade so you can see what you are carrying. |
| Cash settlement implied throughout | — | **Devolvement into physically-settled futures.** | §1.2. Hard pre-expiry exit rules. |

---

## 3. The data plan — you chose "record forward", which puts the recorder on the critical path

This is the right call for GOLDM (no vendor has clean 30-minute option depth for a thinly
traded mini contract, and a synthesised chain would be pure fiction for an illiquid book).
But it has a scheduling consequence your §9 build order does not account for:

> **Every day without a running recorder is a day of data permanently lost.**

M3 (backtest engine) and M4 (strategy) have nothing real to run on until the recorder has
been up for months. So I propose one change to the build order:

**Insert Milestone 1.5 — Chain Recorder, immediately after M1.**

- Read-only. Kotak Neo auth, instrument master (live CSV master), MCX option chain
  discovery, snapshot writer → parquet, plus the `data/validate.py` quality gates running
  live so we find out on day one if what we are capturing is unusable. Historical/closed
  bars come from the Angel SmartAPI candle endpoint on the side.
- **No order placement code whatsoever.** This does not weaken §2.1 — the trading path stays
  unbuilt until M7.
- Runs continuously from that point while M2–M5 are built against synthetic fixtures.

**What the recorder must capture, or the months are wasted:**

1. **Full bid/ask depth snapshots, not just LTP or candles.** For an illiquid book the spread
   *is* the strategy's cost. OHLC alone cannot reconstruct it, and no later cleverness
   recovers it.
2. The **underlying GOLDM futures** quote alongside every chain snapshot, timestamped
   together — delta is meaningless without the synchronous futures price.
3. Every listed strike, not just the ones near 0.25 delta today. Strike selection changes as
   price moves, and a truncated strike range makes the backtest unable to answer "what would
   it have chosen".
4. Raw payloads archived verbatim alongside the parsed form, so a parser bug found in month
   four is repairable rather than fatal.
5. Both the exchange timestamp and our receipt timestamp, so we can measure feed latency
   instead of assuming it.

Meanwhile M2–M5 run against **synthetic fixtures** — deliberately constructed data with
known-correct answers, used to prove the engine, never to produce a performance claim.

---

## 4. Module tree

Single top-level package `algo/`, dependencies pointing inward, `core/` importing nothing from
the project. No `utils.py`.

```
algo/
├── core/                     # domain models, zero I/O, zero broker knowledge
│   ├── enums.py              Side, Right, OrderType, TimeInForce, ProductType,
│   │                         OrderState, Mode, Exchange, RejectReason
│   ├── money.py              Decimal money math: quantize_to_tick, round_to_lot_step
│   ├── timeutil.py           tz-aware UTC helpers, IST/NY conversion, session math
│   ├── clock.py              Clock protocol; BacktestClock / SystemClock. CI gate bans
│   │                         datetime.now / utcnow / time.time outside this file.
│   ├── instrument.py         InstrumentId union (FutureId | OptionId), InstrumentSpec
│   ├── bar.py                Bar, BarWindow (immutable view), Timeframe
│   ├── quote.py              Quote, DepthSnapshot (L2), Tick
│   ├── chain.py              ChainRow, OptionChainSnapshot
│   ├── signal.py             Signal, SignalLeg, PriceIntent, ComboExit, Atomicity
│   ├── order.py              ClientOrderId, Order, OrderUpdate, BrokerOrderRef
│   ├── fill.py               Fill
│   ├── position.py           Position, PositionKey
│   ├── trade.py              Trade (round trip), TradeLeg
│   └── errors.py             DomainError, ConfigError, DataError, RiskRejection,
│                             BrokerError -> Retryable / Fatal
│
├── config/
│   ├── schema.py             pydantic-settings models (§6). Frozen after load.
│   ├── loader.py             YAML + .env + CLI precedence; config hash into every run
│   └── modes.py              live requires env TRADING_MODE=live AND
│                             --i-understand-this-is-real-money. No default, no fallback.
│
├── exchange/                 # MCX venue facts, effective-dated, source-cited
│   ├── calendar.py           MCX trading days, holidays, and the US-DST-driven close time
│   │                         (23:30 IST during US DST, 23:55 otherwise). Keys off
│   │                         America/New_York transitions; expresses session in IST.
│   ├── expiries.py           futures expiry, tender period start, AND option expiry
│   │                         (2 working days before first tender day) — all effective-dated
│   ├── specs.py              ContractSpecStore: lot 100g, tick, strike interval,
│   │                         max order size, DPR circuit rules, by (instrument, date)
│   └── data/                 *.yaml — every entry carries effective_from + source
│
├── costs/                    # pure, no I/O
│   ├── charges.py            McxChargeModel: brokerage, CTT (sell side of premium — we pay
│   │                         it on every entry), MCX transaction charge, SEBI fee,
│   │                         stamp duty, GST. Itemised, never a single blended number.
│   ├── slippage.py           SlippageModel + FixedTicks / SpreadFraction / DepthWalk
│   ├── spread.py             SpreadModel + FromDepth / Modelled / Fixed
│   └── margin.py             MarginModel + SpanExposureApprox / BrokerReportedMargin
│
├── pricing/                  # pure option math (float — see D-004)
│   ├── black76.py            price + greeks on a futures price. Direct fit: MCX options
│   │                         ARE options on futures, so F is observed, not reconstructed.
│   ├── iv.py                 deterministic bounded IV solver
│   ├── underlying.py         option -> its underlying futures contract (replaces the
│   │                         synthetic-forward module, which is not needed here)
│   └── chain_greeks.py       vectorised greeks across a chain snapshot
│
├── indicators/               # pure (Series) -> Series, each declaring its lag
│   ├── base.py               Indicator protocol: compute(), lag: int, warmup: int
│   └── ...                   only what the strategy needs (M2)
│
├── data/
│   ├── feed.py               BarFeed / ChainFeed / DepthFeed protocols
│   ├── recorder.py           (M1.5) live chain + depth snapshot recorder -> parquet
│   ├── resample.py           closed-bar resampler; US-DST-aware session bars; stub flagging
│   ├── parquet_feed.py, csv_feed.py
│   ├── chain_builder.py      per-strike snapshots + futures quote -> OptionChainSnapshot
│   ├── synthetic.py          fixture generator for M2–M5 engine proving (never for results)
│   └── validate.py           quality gates: gaps, dupes, non-monotonic ts, crossed quotes,
│                             stale prints, empty book, circuit-locked bars
│
├── strategy/
│   ├── base.py               Strategy ABC
│   ├── context.py            BarContext, ChainView, PositionView — look-ahead firewall
│   ├── selection.py          pure strike selection (nearest-delta within tolerance)
│   ├── buy_and_hold.py       (M3 engine sanity check, on GOLDM futures)
│   ├── coin_flip.py          (M3 engine sanity check, seeded)
│   └── delta_strangle.py     (M4)
│
├── risk/
│   ├── sizer.py              PositionSizer — fixed_lots per your answer, with the implied
│   │                         risk reported alongside every trade
│   ├── limits.py             concurrency, per-underlying, total-margin caps
│   ├── devolvement.py        HARD RULES: no short option into its expiry session; no
│   │                         futures position into the tender period; ITM-proximity monitor
│   ├── exits.py              stop, target, trail, breakeven, time exit — NOT in strategy
│   ├── killswitch.py         KillSwitch, persisted, crash-surviving
│   └── engine.py             RiskEngine: Signal -> Accepted(orders) | Rejected(reason)
│
├── portfolio/                # shared verbatim by backtest, paper and live
│   ├── book.py               position book, weighted-average cost
│   ├── accounting.py         cash, realised/unrealised, equity, daily MTM
│   └── ledger.py             append-only event ledger
│
├── execution/
│   ├── broker.py             Broker protocol — the only outward-facing boundary
│   ├── idempotency.py        deterministic ClientOrderId + write-ahead journal
│   ├── router.py             journal -> place -> confirm; order-size slicing; combo
│   │                         atomicity; retry classification
│   ├── reconcile.py          startup / reconnect reconciliation
│   ├── fills.py              shared fill simulator used by BOTH backtest and paper
│   ├── sim.py                SimBroker (backtest, M3)
│   ├── paper.py              PaperBroker (live data, simulated fills, M6)
│   └── kotak/
│       ├── kotak.py            (M7) KotakBroker: TOTP login, place/cancel,
│       │                            book/trade reads, positions, funds, ledger
│       ├── kotak_feed.py       (M1.5+) KotakChainFeed: chain snapshots via quotes
│       └── smartapi_feed.py    (M1.5+) SmartApiBarFeed: closed bars via candle API
│
├── backtest/
│   ├── engine.py             bar-by-bar event loop
│   ├── runner.py             single run -> artefacts + run hash
│   └── walkforward.py        (M5)
│
├── reporting/
│   ├── metrics.py            §10 metrics + mandatory sample-size caveat
│   ├── tearsheet.py          equity curve, underwater plot, R-multiple distribution
│   └── export.py             trade log, golden-file writer
│
├── persistence/
│   ├── store.py              SQLite (WAL): live state + order journal
│   ├── analytics.py          DuckDB over parquet: research and recorded chain data
│   ├── schema.sql            explicit DDL, versioned migrations, no ORM
│   └── journal.py            write-ahead order journal (crash recovery)
│
├── api/                      (M8) FastAPI read-only + kill switch
└── cli/
    └── main.py               typer: record | backtest | walkforward | paper | live |
                              reconcile | killswitch | report | data-validate

dashboard/                    (M8) Next.js
tests/                        unit / property / integration / golden
docs/                         plan, decisions, backtest-assumptions, open-questions
data/                         gitignored (recorded chain data lives here)
runs/                         gitignored run artefacts
```

Modules added beyond §4, each justified: `exchange/` (venue facts change over time and must
be effective-dated), `pricing/` (Black-76 is pure but not `Series -> Series`),
`persistence/` (keeps `portfolio/` free of I/O), and `risk/devolvement.py` (physical-delivery
risk has no analogue in the brief and is too dangerous to leave implicit).

`core/money.py` and `core/timeutil.py` are not a `utils.py` in disguise: one subject each,
own test file, standing rule that anything unrelated is rejected.

---

## 5. Interfaces (§5) — proposed signatures

Illustrative; the contract for review, not runnable code.

### 5.1 Identity and specs

```python
class Right(StrEnum):
    CE = "CE"
    PE = "PE"

class FutureId(BaseModel, frozen=True):
    underlying: str                  # "GOLDM"
    expiry: date                     # futures expiry
    exchange: Exchange = Exchange.MCX

class OptionId(BaseModel, frozen=True):
    underlying_future: FutureId      # MCX options are options ON FUTURES
    option_expiry: date              # NOT the futures expiry — see §1.3
    strike: Decimal
    right: Right
    exchange: Exchange = Exchange.MCX

# No broker token here. The Kotak scrip master (live) and the Angel One JSON
# master (history) map to InstrumentId in exchange/master.py. core/ never learns
# what a symboltoken is.

class InstrumentSpec(BaseModel, frozen=True):
    lot_size: Decimal                # GOLDM: 100 g
    price_quotation_unit: Decimal    # quoted per 10 g -> contract multiplier = 10
    tick_size: Decimal
    strike_interval: Decimal | None
    max_order_size: int | None       # MCX per-order quantity cap
    dpr_pct: Decimal | None          # daily price range / circuit
    min_lots: int = 1
    max_lots: int | None = None
    effective_from: date
    effective_to: date | None
    source: str                      # mandatory. No provenance, no entry.
```

### 5.2 Signal — intent only; no lots, no money

**Deviation from §5, needs approval (Q7).** A strangle is two legs that only make sense
together; a call leg filling while the put leg rejects is a naked short call, a different
instrument of risk entirely.

```python
class PriceIntent(BaseModel, frozen=True):
    kind: Literal["MARKET", "LIMIT"]
    limit_price: Decimal | None = None

class SignalLeg(BaseModel, frozen=True):
    instrument: InstrumentId
    direction: Side
    entry: PriceIntent
    ratio: int = 1
    stop_price: Decimal | None = None
    take_profits: tuple[TakeProfit, ...] = ()

class ComboExit(BaseModel, frozen=True):
    kind: Literal["PCT_OF_MARGIN_AT_ENTRY",   # your TP/SL, if "investment" = margin
                  "PCT_OF_EQUITY_AT_ENTRY",   # your TP/SL, if "investment" = equity
                  "PCT_OF_CREDIT", "MULTIPLE_OF_CREDIT", "ABS_INR",
                  "DELTA_BREACH", "UNDERLYING_MOVE_PCT"]
    value: Decimal
    # Resolved to an absolute ₹ level ONCE, at entry, and frozen into the Signal's
    # context. A level that floats with equity would make the same trade exit at a
    # different price depending on unrelated P&L elsewhere.

class Signal(BaseModel, frozen=True):
    signal_id: str                   # deterministic; see 5.6
    strategy_id: str
    ts: datetime                     # UTC close ts of the bar that produced it
    action: Literal["OPEN", "CLOSE", "MODIFY"]
    legs: tuple[SignalLeg, ...]
    atomicity: Literal["ALL_OR_NONE", "BEST_EFFORT"] = "ALL_OR_NONE"
    combo_stop: ComboExit | None = None
    combo_take_profit: ComboExit | None = None
    time_exit: datetime | None = None
    confidence: Decimal = Decimal("1")
    reason: str                      # mandatory, non-empty (validator)
    context: Mapping[str, str] = {}  # deltas, IVs, DTE, futures price, credit, strikes
```

### 5.3 Strategy

```python
class Strategy(ABC):
    strategy_id: str

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> list[Signal]: ...

    @abstractmethod
    def warmup_bars(self) -> int: ...

    # Proposed optional no-op hooks (Q7b):
    def on_fill(self, fill: Fill) -> None: ...
    def on_session_start(self, ctx: BarContext) -> None: ...
    def on_session_end(self, ctx: BarContext) -> list[Signal]: ...
```

The strategy never sees lots, cash, margin or the broker, and cannot place an order.

### 5.4 BarContext — the look-ahead firewall

```python
class BarContext(Protocol):
    @property
    def now(self) -> datetime: ...          # UTC close ts of bar i. The only clock.
    @property
    def bar(self) -> Bar: ...               # bar i, closed
    @property
    def session(self) -> SessionInfo: ...   # session_date, minutes_to_close,
                                            # is_partial_bar, is_us_dst,
                                            # is_option_expiry_session, dte

    def history(self, instrument: InstrumentId, timeframe: Timeframe,
                lookback: int) -> BarWindow: ...
    def indicator(self, spec: IndicatorSpec) -> IndicatorValue: ...
    def chain(self, underlying: str, option_expiry: date) -> ChainView: ...
    def option_expiries(self, underlying: str) -> tuple[date, ...]: ...
    def spec(self, instrument: InstrumentId) -> InstrumentSpec: ...
    def positions(self) -> PositionView: ...
```

Deliberately absent: `dataframe`, `feed`, `broker`, `full_history`, `clock`. `BarWindow`
wraps a copy of the sliced array and is index-bounded, so `window[i+1]` raises `IndexError` —
there is nothing past `i` in memory to read.

`ChainView` exposes `rows()` (sorted by strike, deterministic), `atm()`,
`by_delta(target, right, tolerance)`, `by_strike()`, `futures_price`, `dte`, and
`is_tradeable(row)` — the last one false for an empty book, a crossed quote, a stale print
or a circuit-locked strike.

### 5.5 Risk

```python
class RiskEngine(Protocol):
    def evaluate(self, signal: Signal, snap: RiskSnapshot) -> RiskDecision: ...

# RiskDecision = Accepted(orders: tuple[Order, ...], sizing: SizingTrace)
#              | Rejected(reason: RejectReason, detail: str)
```

`SizingTrace` persists every input and intermediate, so any lot count is reconstructible.
Every rejection is logged with its reason; a skipped trade is never silent.
`RejectReason` includes `DEVOLVEMENT_WINDOW` and `TENDER_WINDOW` as first-class values.

### 5.6 Order and idempotency

```python
class ClientOrderId(BaseModel, frozen=True):
    value: str   # f"{strategy_id}.{signal_id}.{leg_ix}.{slice_ix}"

# signal_id = blake2b(canonical_json({
#     strategy_id, strategy_params_hash, bar_close_ts, action,
#     leg instruments + directions, config_hash
# }))[:16]
# Same bar + same config => same id, so a replay after a crash cannot create a
# second order for the same intent.

class Order(BaseModel, frozen=True):
    client_order_id: ClientOrderId
    signal_id: str
    instrument: InstrumentId
    side: Side
    lots: int
    qty: int                       # validated against max_order_size
    order_type: OrderType
    limit_price: Decimal | None
    trigger_price: Decimal | None
    product: ProductType           # NRML
    tif: TimeInForce
    created_at: datetime
    slice_of: ClientOrderId | None = None
```

### 5.7 Broker boundary

```python
class Broker(Protocol):
    def connect(self) -> None: ...
    def place(self, order: Order) -> BrokerOrderRef: ...
    def modify(self, ref: BrokerOrderRef, changes: OrderModification) -> BrokerOrderRef: ...
    def cancel(self, ref: BrokerOrderRef) -> None: ...
    def open_orders(self) -> list[BrokerOrderSnapshot]: ...
    def positions(self) -> list[BrokerPositionSnapshot]: ...
    def executions(self, since: datetime) -> list[BrokerFillSnapshot]: ...
    def funds(self) -> Funds: ...
    def health(self) -> BrokerHealth: ...
```

Synchronous — see D-003 and Q8. All broker timestamps convert to tz-aware UTC inside the
adapter; no naive datetime crosses this boundary.

---

## 6. Config schema (shape, for review)

```yaml
mode: backtest                      # backtest | paper | live  (live gated twice, §2.1)

run:
  name: goldm_strangle_fixture
  seed: 20260819
  out_dir: runs/

market:
  timezone: Asia/Kolkata            # session expressed here
  dst_reference_zone: America/New_York   # but the CLOSE TIME keys off US DST
  calendar: mcx
  session:
    open_ist: "09:00"
    close_ist_us_dst: "23:30"
    close_ist_otherwise: "23:55"
  bar:
    timeframe: 30m
    anchor: session_open            # 09:30, 10:00, ... 23:30 (+ 23:30-23:55 stub in winter)
    partial_last_bar: keep_flagged
    act_on_partial_bar: false

instruments:
  - underlying: GOLDM
    exchange: MCX
    spec_source: exchange/data/goldm.yaml

data:
  chain:  {kind: parquet, path: data/chain/goldm/}
  depth:  {kind: parquet, path: data/depth/goldm/}
  quality:
    reject_crossed_quotes: true
    reject_empty_book: true
    reject_circuit_locked: true
    max_stale_bars: 2
    on_violation: skip_bar

recorder:                           # M1.5
  enabled: false
  snapshot_interval_s: 5
  capture_depth_levels: 5
  strike_range_pct: 15              # capture well beyond 0.25 delta
  archive_raw_payloads: true
  out_path: data/raw/

costs:
  charges:
    provider: mcx_v1
    rates_file: exchange/data/charges_mcx.yaml    # effective-dated + source-cited
  spread:
    model: from_depth
    fallback: {pct_of_premium: "2.0", min_ticks: 2}
  slippage:
    entry: {model: depth_walk}
    stop:  {model: depth_walk, extra_ticks: 2}
  margin:
    model: span_approx

execution:
  entry_order_type: LIMIT
  limit_offset_ticks: 2
  combo_atomicity: ALL_OR_NONE
  unwind_on_partial_fill: true
  slice_on_max_order_size: true
  retry: {max_attempts: 3, backoff_ms: 250, jitter: false}
  reconcile_on_start: true

risk:
  starting_equity: "1000000.00"
  sizing:
    mode: fixed_lots                # your answer
    fixed_lots: 1
  caps:
    max_concurrent_strangles: 1
    max_lots_per_underlying: 5
    max_total_margin_pct: "50.0"
  devolvement:                      # HARD RULES — not optional
    force_exit_before_option_expiry_sessions: 1
    block_new_entries_within_dte: 2
    forbid_futures_into_tender: true
  kill_switch:
    daily_loss_limit_pct: "2.0"
    max_consecutive_losses: 3
    max_drawdown_pct: "10.0"
    flatten_on_trip: false

strategy:
  id: goldm_delta_strangle_v1
  params:
    target_delta: "0.25"
    delta_tolerance: "0.05"
    option_expiry:
      select: nearest
      source: instrument_master     # D-023: read expiry, never compute it from a
      crosscheck_rule: mcx_goldm_v1 #        weekday rule. Rule only alarms on mismatch.
    min_dte: 5
    max_dte: 45
    entry:
      bars_ist: ["09:30"]           # close of the first 30-min bar (09:00-09:30)
      cadence: per_expiry_cycle     # CONFIRMED: one strangle per monthly cycle, ~12/yr
    exit:                           # confirmed: 2% / 1% of MARGIN BLOCKED at entry
      take_profit: {kind: PCT_OF_MARGIN_AT_ENTRY, value: "2.0"}
      stop_loss:   {kind: PCT_OF_MARGIN_AT_ENTRY, value: "1.0"}
      time_exit: null               # Q4 — none beyond the mandatory pre-expiry flat
      evaluate_on: bar_close        # backtest; live evaluates on ticks (Q15)
      min_stop_to_cost_ratio: "3.0"
      on_stop_viability_breach: warn   # warn | refuse   (Q4d — your call)
                                       # D-024: logs stop vs modelled round-trip cost

persistence:
  live:     {backend: sqlite, path: state/live.db, wal: true}
  research: {backend: duckdb, path: state/research.duckdb}

logging: {level: INFO, format: json, file: runs/{run_name}/log.jsonl}
api: {enabled: false, host: 127.0.0.1, port: 8000, token_env: ALGO_API_TOKEN}
```

---

## 7. How §7 (look-ahead prevention) is enforced structurally

1. **Physical slicing.** `BarContext` gets a *copy* of `[0..i]`. Future rows are not in the
   object graph, so no accessor can reach them.
2. **Future-poisoning canary.** Full backtest run twice — once on real data, once on data
   whose rows after each decision point are randomised garbage. Trade logs must be
   byte-identical.
3. **Cheating-strategy canary.** A strategy attempting `ctx.bars[i+1]`, `ctx._df` or
   `ctx.feed` must raise. Asserted, per §7.3.
4. **Indicator causality property test.** Value at `i` over `[0..i]` must equal value at `i`
   over `[0..n]` once the declared `lag` is applied.
5. **Declared lag** is mandatory; the engine shifts centred indicators accordingly.
6. **Higher-TF resampling** emits only completed bars.
7. **Determinism.** Same data + config ⇒ byte-identical trade log and identical config hash.
   Seeds fixed, retry jitter off, sorted iteration, IV solver iteration-bounded.
8. **Calendar canary (new).** A test asserting that bar counts and close times are correct
   across both 2026 US DST transitions, and that no bar is emitted after the session close in
   either regime. The DST bug this brief predicts would otherwise surface as a silent
   one-bar-per-day error for eight months of the year.

---

## 8. Milestone 3 engine falsification

- Buy-and-hold on GOLDM futures tracks the instrument within costs.
- A seeded coin-flip loses approximately `sum(spread crossed) + sum(charges)`; predicted vs
  realised cost drag is reported and the test fails on divergence. If they diverge, the
  engine is wrong and work stops there.
- Zero-cost, zero-slippage config reproduces hand-computed P&L on a synthetic fixture
  exactly, in `Decimal`.
- **Devolvement test:** a fixture where a short leg is ITM at option expiry must show the
  risk layer force-exiting beforehand, and a deliberately disabled rule must show the
  futures position appearing — proving the rule is what prevents it, not luck.

---

## 9. Proposed build order (revised)

| M | Content | Blocked on |
|---|---|---|
| 0 | This plan | your review |
| 1 | Core domain, config, MCX calendar (US-DST aware), expiry calendar, spec store, resampler, look-ahead canaries. Synthetic fixtures. | Q1, Q7, Q8, Q10, Q11 |
| **1.5** | **Chain recorder — read-only. Starts the data clock.** | Angel One API credentials |
| 2 | Indicators the strategy needs, with lag declarations | Q3 |
| 3 | Backtest engine + MCX cost model + fill simulator. Buy-and-hold and coin-flip falsification on fixtures. | Q6 |
| 4 | Strategy + risk + devolvement rules. Fixtures only until recorded data matures. | Q2–Q5 |
| 5 | Walk-forward — **may be infeasible at ~6 cycles/year; will report why if so** | recorded data |
| 6 | Paper adapter, reconciliation, crash recovery | — |
| 7 | Angel One trading adapter | Q9 |
| 8 | FastAPI + Next.js dashboard | — |
