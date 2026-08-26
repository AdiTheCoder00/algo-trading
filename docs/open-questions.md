# Open questions

Revised after your answers. Each states **my default** so you can reply by exception.
`[BLOCKING Mn]` means I cannot start that milestone without it.

## Answered

| | Your answer | Consequence |
|---|---|---|
| Instrument | **GOLDM options, MCX** | Options *on futures*; devolvement into physically-settled futures; MCX charge stack (CTT); 09:00–23:30/23:55 IST session driven by **US** DST |
| Data | **Record forward** | Recorder is on the critical path → new Milestone 1.5; M3–M5 run on synthetic fixtures until real data matures |
| Sizing | **Fixed lots** | `sizing.mode = fixed_lots`; implied risk still reported per trade |
| Holding | **NRML, carry overnight** | Full margin; overnight gap exposure; devolvement rules become mandatory |
| Expiry | **"Last Friday of every month"** | **Contested — see Q1.** Sources point to a derived date, not a weekday. Monthly cadence confirmed (~12 cycles/yr, not the ~6 I first claimed) |
| Take profit | **2% of investment** | Combo-level exit, resolved to absolute ₹ at entry |
| Stop loss | **1% of investment** | Combo-level, frozen at entry |
| TP/SL basis | **Margin blocked** | `PCT_OF_MARGIN_AT_ENTRY`. Chosen with the cost arithmetic in front of it, so D-024's viability check drops to **warn**, not refuse — see Q4d |
| Entry | **09:30 bar** | Close of the first 30-min bar (09:00–09:30 IST), time-based, no filter |
| Cadence | **Once per expiry cycle** | One strangle per monthly cycle ⇒ **~12 trades/year**. Sample size stays the hard limit on what any metric can show |
| Expiry | **Last Friday of the month — CONFIRMED** | Terminal shows GOLDM 28 Aug 2026, a Friday and the last Friday of August. I disputed this and was wrong (C-004). Instrument master remains source of truth (D-023); "last Friday" is the cross-check |
| Live broker | **Kotak Neo trades; SmartAPI reads history** | Live orders, chain feed and the live scrip master come from Kotak Neo (`KotakBroker`, `KotakChainFeed`, Kotak CSV master). SmartAPI survives only for historical/closed bars (candle API) and the Angel JSON master. `SmartApiBroker` removed. No websocket bar feed this pass |

---

## A. Contract and strategy

### Q1 `[BLOCKING M1]` The expiry date — please check one live contract in Angel One

You said **last Friday of every month**. The monthly cadence checks out — that part corrects
my earlier ~6-cycles-a-year claim, it is ~12. But the *day* does not match anything I can
find. Sources describe a chain of derived dates:

```
GOLDM futures expiry    = 5th of the contract month  (monthly: 5 Jan, 5 Feb, 5 Mar, 5 Aug…)
Tender period first day = ~3 business days before futures expiry (staggered delivery)
GOLDM option expiry     = 3 business days before the first tender day
                          => lands in the LAST WEEK OF THE PRECEDING MONTH
```

Worked example from those sources: **GOLDM May 2026 calls expire Wednesday 27 May 2026.**
The last Friday of May 2026 is the **29th**.

**Why this is the most dangerous item in the whole plan:** a "last Friday" rule would have the
engine exiting on the 29th a contract that expired on the 27th. Those two days *are* the
devolvement window — an ITM short leg has already become a GOLDM futures position heading for
physical delivery of 100 g of gold, and the system would not know.

Three possibilities and I cannot tell which: you may be describing the futures roll or your
own habitual exit day; your terminal may show something my sources do not; or the rule may
have changed. **Please open one live GOLDM option contract and tell me the exact expiry date
it shows.**

**Design answer regardless (D-023):** the system will not compute expiry from a rule at all.
It reads expiry from the Angel One instrument master — what the exchange actually acts on —
and uses the derived rule only as a cross-check that alarms on mismatch. If the two disagree,
the engine halts new entries rather than picking one.

### Q1e `[BLOCKING M4]` **What is the ₹1,56,640 your terminal displayed?** It is not what the options are priced off.

Found while building the pricing layer, from your own chain. Inverting every strike
against 1,56,640 gives put volatilities roughly **0.3 points above call volatilities at
every single strike** — a one-sided error at every strike, which is not noise.

Solving put-call parity for the forward instead — model-free, no volatility assumption —
gives a tight cluster:

| Strike | C − P | Implied forward |
|---|---|---|
| 155000 | 1612.50 | 156,615 |
| 155500 | 1115.50 | 156,617 |
| 156000 | 610.00 | 156,611 |
| 157000 | −395.50 | 156,604 |
| 158000 | −1390.50 | 156,607 |

Median **156,611** against a displayed **156,640**. A thirty-point gap, and the estimates
agree with each other to within thirteen points — so it is a signal, not one bad print.

**Why this matters more than thirty points sounds:** a wrong forward biases every delta in
the chain the same way, which biases strike selection the same way, on every trade,
permanently. It would never surface as an error — only as a strategy that quietly sells
slightly the wrong strikes.

Three candidates, and I cannot tell them apart from a screenshot:

1. **It is spot, not the future.** MCX gold spot and the near future differ by cost of
   carry, and thirty points on 156,640 is about 0.019% — plausible for nine days.
2. **It is a different contract month** than the one these options settle into.
3. **The LTPs are stale relative to each other.** They are last trades, not synchronised
   quotes, so the chain may simply be a mosaic of different moments.

If (3), the recorder solves it by capturing the futures quote in the same snapshot as the
options. If (1) or (2), we need to know which futures series to read. **Please check what
that tooltip figure is labelled as, and whether the chain lets you see the underlying
futures contract explicitly.**

The cross-check is built either way: `implied_forward()` compares the two and reports the
gap rather than absorbing it.

### Q1c `[BLOCKING M4]` Which futures contract underlies the 28 Aug option series?
The devolvement rules need it. An ITM short leg at option expiry becomes a **GOLDM futures
position** — in which contract, and how many days until *that* contract's tender period opens?
That gap is the entire margin for error between "we got assigned" and "we owe someone 100 g of
gold". Please read the underlying contract off the chain, or tell me the GOLDM futures expiry
dates your terminal lists.

**Partly routable now.** `build_snapshots(..., resolve_underlying=...)` in the bhavcopy
loader takes this mapping as an injected function; its default pairs an option expiry with
the earliest futures contract expiring on or after it. That default is a documented
heuristic, and it is the thing this question exists to replace with fact (D-084). The
devolvement rules still need the real answer - a heuristic is fine for grouping a backtest
and is not fine for deciding whether you owe someone 100 g of gold.

### Q1d `[BLOCKING M4]` Scroll the chain to 153000 and 160500 — are they quoted?
From your screenshot (σ ≈ 21.6%, 9 DTE, F = ₹1,56,640) the **0.25-delta strikes are ≈ 160,500
call and ≈ 153,000 put** — outside the 155000–159000 window shown. The furthest visible strike,
159000 CE, prices at ≈ 0.34 delta, not 0.25.

Inside that ±1.3% window your chain already shows `–` at 157500 PE and 159000 PE, and 158500 is
missing entirely — with **"Tradeable only" switched on**. That is the liquidity question
answering itself, from the live book.

**Please scroll to ≈153000 PE and ≈160500 CE and tell me whether they are listed, and whether
they show a two-sided quote.** If they are not quoted, the strategy as specified cannot be
executed at 0.25 delta on GOLDM, and the real choices become: a nearer delta (0.30–0.35, which
the book clearly supports), or **GOLD (1 kg) options** where depth is better at 10× notional
per lot. That is a decision worth making now rather than after the recorder confirms it in
three months.

**Now answerable from evidence rather than a screenshot.** The MCX bhavcopy carries volume
and open interest per strike for every expired cycle back to 2016. `algo bhavcopy <dir>`
reports what share of the ladder actually traded, and a strike that was listed but never
traded is reported as untradeable rather than given a synthetic book (D-083). One
downloaded file plus a few years of archive answers "is 0.25 delta reachable on GOLDM"
across roughly a hundred cycles instead of one afternoon. That does not replace the
question - the bhavcopy has no bid/ask, so it can show a strike traded without showing
what the spread was - but "it never trades at all" and "it trades every day" are both
things it can settle outright.

### Q2 `[BLOCKING M4]` Which expiry do you sell, and at what DTE?
Nearest option expiry, or skip to the next? What DTE window is acceptable at entry?

**Default:** nearest option expiry, entry only when DTE ∈ [5, 45], no new entry inside 2 days
of option expiry.

### Q3 Entry trigger — answered: the 09:30 bar
Signals evaluated only at the close of the first 30-minute bar (09:00–09:30 IST). Time-based,
no filter. Recorded as D-026.

### Q3a Cadence — answered: once per expiry cycle
One strangle per monthly cycle, ~12 trades a year. Recorded. The consequence stated below
stands: **~12 trades a year cannot validate a strategy**, and every metric will carry its
sample size. Original framing kept for the record.


"Entry at the 09:30 bar" fixes *which* bar but not *which days*. The two readings produce
completely different systems:

| Cadence | Meaning | Trades/year |
|---|---|---|
| **Once per expiry cycle** | Enter at 09:30 on the first eligible day of each monthly cycle; one strangle per cycle | **~12** |
| **Every day when flat** | Enter at 09:30 on any day the book is flat and DTE is in band | **~250** |

This is not a small dial. With SL at 1% of margin — a tight stop — positions will often close
within a day or two, so under "every day when flat" the strategy becomes a **near-daily
premium-selling trade**, not a monthly position trade. That is a coherent strategy, and it has
one large advantage: ~250 trades a year is a sample size that can actually be evaluated,
whereas ~12 is not. It also multiplies cost drag by roughly 20×, and cost drag is the thing
most likely to decide this strategy's fate.

**Default:** once per expiry cycle, because it is the more conservative reading of what you
said. But if you meant daily, say so — it materially improves how much the backtest can tell
you.

*(This supersedes Q5, which asked the same thing from the other direction.)*

### Q3b `[BLOCKING M4]` **Where in the cycle?** — and it collides with liquidity
"Once per cycle, at the 09:30 bar" still leaves the entry day open, and the choice is not free:

| Entry point | DTE | 0.25Δ call strike (at σ ≈ 21.6%) | Trade-off |
|---|---|---|---|
| Day after previous expiry | ~30 | ≈ **163,700** (≈ +4.5% OTM) | Most theta to harvest, **deepest into the illiquid tail** |
| Mid-cycle | ~15 | ≈ 161,500 (≈ +3.1%) | Middle of both |
| Late cycle | ~9 | ≈ **160,500** (≈ +2.4%) | Closest to the quoted range, least time premium |

A fixed delta sits **further from spot as time to expiry grows**, so entering early in the
cycle — the intuitive choice for a premium seller — pushes strike selection into exactly the
strikes least likely to have a two-sided quote. Q1d determines how binding this is.

**Default:** enter on the first trading day of the cycle (~30 DTE), and let the recorder tell
us whether 0.25 delta is quotable that far out. If it is not, the choice becomes a later entry
or a nearer delta — decided on measured spreads, not guesswork.

### Q4 Exit rules — TP 2% / SL 1% recorded. Time exit still open.
Both are combo-level, resolved to an absolute ₹ level once at entry and frozen (D-025).
Still needed: a **time exit** — beyond the mandatory pre-expiry flat, is there a "close after
N days regardless" rule?

**Default:** no time exit; the position runs until TP, SL, or the mandatory pre-expiry exit.

### Q4a `[BLOCKING M4]` **2% and 1% of *what*?** This changes the strategy by 10×
A short strangle receives credit — you do not invest anything — so "investment" needs
pinning down. Illustrative figures (gold ~₹1,15,000/10 g ⇒ GOLDM ≈ ₹11.5 lakh notional/lot;
strangle margin ≈ ₹1 lakh/lot; **placeholders until the recorder gives real numbers**):

| "Investment" means | TP (2%) | SL (1%) |
|---|---|---|
| Margin blocked for the position | ₹2,000 | **₹1,000** |
| Account equity (₹10 lakh) | ₹20,000 | ₹10,000 |
| Notional contract value | ₹23,000 | ₹11,500 |
| Premium received (~₹15,000) | ₹300 | ₹150 |

**And here is why it is not a cosmetic choice.** Round-trip friction on one strangle is four
spread crossings (two legs, in and out) plus brokerage, CTT, exchange, stamp and GST. On a
thin GOLDM option book the **spread dominates everything else** — the taxes are small change
next to it. At 10–30 ticks per leg (tick = ₹1/10 g = ₹10/lot) that is roughly **₹500–1,500
per lot round trip**.

- On the **margin** reading the stop is ~₹1,000 — **the same size as the cost of entering and
  exiting**. The position would sit at or near its stop the instant it filled. That is not a
  strategy, it is a cost machine.
- On the **equity** reading the stop is ~₹10,000 — about 10× friction, which works.

**ANSWERED: margin blocked.** `PCT_OF_MARGIN_AT_ENTRY`, TP 2%, SL 1%, both frozen at entry.
You chose this with the arithmetic above in front of you, so it is what gets built and I am
not raising it again.

### Q4d Stop-viability check — `warn` or `refuse`?
D-024 compares the configured stop against modelled round-trip cost. On the margin basis it
will very likely report a ratio near or below 1× at startup.

**Default: `warn`.** It logs the stop, the modelled round-trip cost and the ratio at startup
and on every entry, records it on each trade, and proceeds. Two reasons: you made the choice
informedly and it is not the engine's job to veto it; and my ₹500–1,500 friction figure is a
placeholder I invented — **the recorder will replace it with a measured spread within weeks**,
at which point the ratio becomes a fact rather than my estimate. `refuse` stays available in
config if you would rather the system hard-stop.

### Q4b The payoff shape — your call, flagging it once
TP 2% with SL 1% is a 2:1 payoff needing a >33% win rate before costs. A short strangle's
natural distribution is the opposite: frequent small wins, rare large losses, because credit
is earned slowly by theta while adverse moves arrive immediately through gamma and vega.
Setting the target at twice the stop asks a premium-selling position to behave like a trend
trade. It may still work — the backtest will report realised win rate and the R-distribution,
which is the number to judge it on. I am implementing exactly what you specified.

### Q4c The mandatory pre-expiry exit — please confirm you accept it

**Not negotiable, and I want you to say you understand it:** an ITM short leg left at option
expiry **devolves into a GOLDM futures position**, and GOLDM futures go to **compulsory
physical delivery of 100 g of gold**. The risk layer will force-exit before option expiry and
will refuse to carry a futures position into the tender period. These are hard rules in
`risk/devolvement.py`, not config toggles. Tell me if you disagree — but I am not shipping a
system that can accidentally take delivery of gold.

### Q5 `[BLOCKING M4]` Re-entry within a cycle?
After a take-profit, do you re-enter the same expiry cycle at fresh 0.25-delta strikes?

**Default:** no. But see the trade-count problem in the plan §1.4 — re-entry is the only
lever that meaningfully raises n, so this is worth deciding on purpose.

---

## B. Costs

### Q6 `[BLOCKING M3]` A real contract note, please
Updated 2026-08-26 (D-098): `charges_mcx.yaml` no longer runs on an unsourced guess. Checked
against real, dated sources — Kotak Neo's own published rate card for brokerage (flat Rs 10
per order, commodity & currency, on the Trade Free plan), MCX's own 2024-10-01 fee circular
for the options exchange transaction charge (Rs 41.80/lakh premium turnover), and a broker's
regulatory-charges breakdown for CTT, stamp duty, SEBI fee and GST. One real correction came
out of it: the futures exchange transaction charge was off (0.0026% vs a sourced 0.0021%);
everything else the original secondary-source guess had already landed on the right number,
just without a citation attached.

That is real progress, and it is not the same thing this question originally asked for. A
rate card is a published, general figure; a contract note is what actually happened on one
trade on one account, with that account's plan and any account-level adjustment folded in.
`verified` stays false for exactly that reason, and the dashboard's warning now says so
precisely — "sourced, not contract-note verified" — instead of the old "placeholder", which
overstated how little was known and understated what still is.

**Still open, for the same original reason: paste one real Angel One or Kotak Neo contract
note for an MCX options trade.** It is the only thing that pins every component *and* its
rounding to the paisa, and the only thing that can flip `verified` to true per D-011.

---

## C. Design sign-offs

### Q7 `[BLOCKING M1]` Multi-leg `Signal` — approve the deviation from §5?
Your §5 gives `Signal` a single `direction`. A strangle is two legs that only make sense
together: a call filling while the put rejects leaves a naked short call, which is a
different instrument of risk. I propose `legs: tuple[SignalLeg, ...]` plus `atomicity` and
combo-level exits (plan §5.2).

**Q7b:** approve optional no-op `on_fill` / `on_session_start` / `on_session_end` hooks?
`on_session_end` is what expresses square-off cleanly instead of faking it inside `on_bar`.

### Q8 `[BLOCKING M1]` Sync or async engine?
**Default: synchronous** single-threaded loop, broker and websocket I/O isolated in adapter
threads behind queues. Deterministic, testable, and backtest/live share one code path —
your §4 invariant. Async buys throughput this system does not need and costs determinism it
cannot afford.

### Q9 `[BLOCKING M1]` Partial bar and bar count across DST
In winter the session is 09:00–23:55, which is 29 bars plus a 25-minute stub. In US-DST
months it is 09:00–23:30, exactly 29 bars.

**Default:** emit the stub flagged `is_partial`; strategy may not act on it, risk layer may.
Say the word if you would rather use 15-minute bars, where the stub disappears entirely.

### Q17 `[ANSWERED]` `Quote.status()` has no spread-width gate — and real data exploits it

> **Answered: "use yours."** Implemented as `DEFAULT_MAX_SPREAD_PCT = 10` (percent
> of mid) plus a reject on a *reported* open interest of exactly zero — see
> D-101. Open interest of `None` (feed never said) is explicitly not treated as
> zero. The original question is kept below because the evidence in it is the
> justification for the threshold.

Found while wiring the scraped live chain (D-099), on your own 26 Aug scrape.
`Quote.status()` refuses an empty book, a non-positive price, a crossed book and
a stale one — but says nothing about **how wide** the book is. A quote of
bid 76.5 / ask 884.5 is uncrossed, positive and fresh, so it passes as `OK`.

That is not hypothetical. In that one scrape, **24 of 204 "tradeable" rows have a
spread wider than 50% of the bid**, the worst being bid 1 / ask 1833. Every one
of them has zero volume. The IV solver then inverts the *mid* of that garbage
book, converges on a plausible-looking number, and produces a delta that sits
squarely in the strategy's selling zone:

```
167500 CE   bid 76.5   ask 884.5   vol 0   ->  mid 480.5  ->  iv 51.1%  ->  delta +0.150
167000 CE   bid 106    ask 107.5   vol 27202                 iv 32.3%      delta +0.063
168000 CE   bid 76     ask 77.5    vol 32546                 iv 33.9%      delta +0.045
```

`algo chain` confirms the consequence: at a 0.15 delta target the strategy picks
**167500 CE** — the fabricated row, not either real neighbour. Live, it would
sell believing it collected ~480 and actually collect 76.5.

**My default: gate on relative spread, and reject rather than widen.** A row
whose spread exceeds some percentage of its mid becomes `QuoteFlag.TOO_WIDE` —
untradeable, unpriced, and *visible* in the chain panel as a gap, the same
treatment an unquoted strike already gets. I have deliberately **not**
implemented this yet, because the threshold is a strategy risk decision, not a
code detail, and picking it silently is exactly the kind of invisible assumption
D-005 exists to prevent.

**What I need from you:** the threshold. On this scrape, the real near-the-money
book runs 0.3–1.5% of mid, and the fabricated rows run 50%+ — so anything in the
5–15% range separates them cleanly. I would pick **10% of mid**, plus a
requirement of non-zero open interest. Say a number, or say "use yours".

Until this is answered, treat any backtest or live signal whose chosen strike had
zero volume as suspect.

---

## D. Operations — these gate the recorder, which gates everything

### Q10 `[BLOCKING M1.5]` Angel One API access
Confirm: MCX segment enabled on the account; SmartAPI access approved; API key issued; TOTP
set up for API login. And confirm GOLDM **option** contracts appear in the instrument master
with tradable tokens (futures certainly do; options I want you to eyeball).

Credentials go in `.env`, gitignored, never in the repo, never in a log line.

### Q11 `[BLOCKING M1.5]` Where does the recorder run?
This is the question that decides whether "record forward" actually works. The recorder must
be up 09:00–23:30 IST, every trading day, for months. **Every hour it is down is data you
cannot get back.**

- **This Windows machine** — free, but sleeps, reboots, gets closed. Realistically it will
  lose days without you noticing.
- **A small VPS** (recommended) — a few hundred rupees a month, runs unattended, survives
  your laptop.

**Default:** build it to run either way, with a **daily coverage report** that tells you
exactly how many snapshots were captured versus expected — so a gap is discovered the next
morning rather than in month four.

### Q13 `[BLOCKING live drill]` Kotak Neo credentials
The live drill (`algo live`) needs the five `ALGO_KOTAK_*` variables in `.env`
(`CONSUMER_KEY`, `MOBILE_NUMBER`, `UCC`, `TOTP_SEED`, `MPIN`) alongside the four
`ALGO_SMARTAPI_*` ones. The Kotak session is established fresh on every connect
(TOTP login + MPIN — the SDK cannot restore a trade session from an access token
alone), and a wrong seed/MPIN is reported as a hard failure, never retried.

### Q12 `[BLOCKING M1]` Repository
`D:\algo trading` is not a git repository. Shall I `git init` and add a `.gitignore` covering
`.env`, `data/`, `runs/`, `state/`? A system that moves real money without version control is
itself a hazard. Confirm the package name — default `algo/`.

### Q13 `[BLOCKING M1]` Persistence
**Default:** SQLite (WAL) for live state and the order journal; DuckDB over parquet for
recorded chain data and research. Two stores, each good at its job, no ORM. Approve?

### Q14 Capital and kill-switch thresholds
**Default:** ₹10,00,000 starting equity; 1 lot; max 5 lots per underlying; 50% max margin;
daily loss 2% of start-of-day equity; 3 consecutive losses; 10% max drawdown.
`flatten_on_trip: false` — a trip halts new orders and alerts, because market-closing a short
strangle during the move that tripped it can cost more than the breach.

### Q15 `[BLOCKING M7]` Stop handling in live
Engine-managed stops evaluated on ticks, or broker-side stop orders? Broker-side survives our
process dying; engine-managed is more flexible but dies with the process. I will verify what
Angel One accepts for MCX options at M7 rather than assume now.

**Default:** engine-managed on ticks, broker-side protective orders added as a second net once
verified, and an explicit note that the **backtest evaluates stops only at bar granularity**
and is therefore optimistic relative to live on fast moves.

### Q16 Dashboard scope (M8)
**Default:** read-only monitoring plus the kill switch. Parameter changes go through config
and a restart, so every live parameter traces to a committed file.

---

## E. Things I want on the record before you fund anything

Not questions. Say if you disagree with any of them.

1. **~6 trades a year cannot validate a strategy.** Whatever the metrics say at n=6 or n=30,
   they will not distinguish skill from luck. Every report will carry the sample size next to
   the Sharpe ratio. Walk-forward (M5) may simply not be feasible; if so I will say so rather
   than produce a chart that implies otherwise.
2. **Import duty changes are an unhedgeable jump risk with no international counterpart.**
   MCX gold ≈ international gold × USDINR + import duty. A duty change moves MCX gold in a
   way no volatility model anticipates, and it is exactly the event that destroys a short
   strangle. It will not appear in a backtest unless one happened to fall in the sample.
3. **Fixed lots means no risk-based sizing.** That is a legitimate choice and I have
   implemented what you asked, but the loss on a short strangle is unbounded and fixed lots
   does not scale exposure down as equity falls. The implied risk per trade will be reported
   so the number is at least visible.
4. **A recorded dataset is months away from being useful.** Between now and then, every
   number this system produces comes from synthetic fixtures and proves only that the engine
   is arithmetically correct — never that the strategy works.
