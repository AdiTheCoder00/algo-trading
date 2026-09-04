# Decision log

Every judgement call, with its reason. Append-only; superseded entries are marked, not
deleted. D-001..D-022 were made during Milestone 0 and are **provisional until the open
questions are answered**.

---

## Corrections

### C-001 — I was wrong that London/NY sessions do not apply
**What I wrote:** in the first draft of the plan, that §2.6's London/NY kill zones had no
analogue and were "replaced by a single IST session".
**Why it was wrong:** that was written while assuming NSE index options. For MCX gold it is
backwards. Gold's price discovery is COMEX/LBMA, MCX gold is close to a currency-adjusted
derivative of international gold, and the MCX evening session exists precisely to overlap the
US session. Session-of-day is probably the most informative filter available for this
instrument. **§2.6 stands as written.**

### C-002 — I was wrong about the session and about which DST matters
**What I wrote:** 09:15–15:30 IST, and that IST having no DST made §2.6's DST warning
inapplicable.
**Why it was wrong:** MCX non-agri runs 09:00–23:30/23:55 IST, and the close time keys off
**US** daylight saving, not Indian. A session model written only in `Asia/Kolkata` would be
wrong for roughly eight months of every year — exactly the failure the brief predicted, just
sourced from a different country's clock.

### C-003 — I over-stated the trade-count problem
**What I wrote:** GOLDM follows a Feb/Apr/Jun/Aug/Oct/Dec cycle giving ~6 expiry cycles a
year, and that n≈6/year makes validation impossible.
**Why it was wrong:** that is the **GOLD (1 kg)** cycle. **GOLDM futures are monthly**,
expiring on the 5th of each contract month, so there are ~12 option cycles a year. The concern
is halved, not eliminated — ~24 trades from two years of recording is still thin — but I
stated it more strongly than the facts supported.

---

### D-001 — Single top-level package `algo/`, layered inward
Dependencies point inward; `core/` imports nothing from the project.
**Why:** §4 requires backtest, paper and live to share one `Strategy` and one `Portfolio`. A
flat layout makes it easy to import a broker type into domain code, which is how the
two-systems-that-disagree failure starts.

### D-002 — Modules added beyond §4: `exchange/`, `pricing/`, `persistence/`, `risk/devolvement.py`
**Why:** `exchange/` — MCX venue facts (session times, expiry rules, lot specs, circuits)
change over time; scattering them guarantees a stale constant. `pricing/` — Black-76 is pure
but not `(Series) -> Series`, so `indicators/` would be the wrong home. `persistence/` — keeps
`portfolio/` free of I/O. `risk/devolvement.py` — physical-delivery risk has no analogue in
the brief and is too dangerous to leave implicit in a strategy's exit logic.

### D-003 — Synchronous engine, I/O isolated in adapter threads
**Why:** determinism is a hard requirement (§7.4) and asyncio schedules
non-deterministically. Throughput needs here are trivial. Sync lets backtest and live share
one engine, which async would not without duplicating the loop. **Revisit if** tick volume
ever becomes the bottleneck. Q8.

### D-004 — `Decimal` for money, `float` for greeks, explicit boundary
**Why:** IV comes out of an iterative solver; forcing `Decimal` through Newton iterations
buys no accuracy and costs determinism. Allowed only because **greeks never touch money** —
delta selects a strike, and the strike itself is a `Decimal` read from the chain. Enforced by
annotations plus a CI grep banning `float(` in money paths.
**Risk accepted:** a float delta of 0.2499 vs 0.2501 could select a different strike across
machines. Mitigated by rounding delta to fixed precision before comparison and by D-005.

### D-005 — Deterministic IV solver
Bracketed bisection to fixed tolerance, fixed max iterations, bounded Newton polish, no
random or adaptive initial guess.
**Why:** §7.4 requires byte-identical trade logs. Non-convergence returns an explicit failure
that marks the row untradeable rather than a silent fallback.

### D-006 — Look-ahead prevented by physically slicing history
`BarContext` receives a copy of `[0..i]`; there is no future data in the object graph.
Reinforced by the future-poisoning test.
**Why:** §7 says structurally, not by convention. An accessor guard can be bypassed by a
determined strategy author (including me, six months from now); absent data cannot.
**Cost accepted:** O(n·w) copying. Irrelevant at this scale.

### D-007 — Indicators pure + causality property test, no incremental state in v1
**Why:** the causality test only works if the function can be called on both `[0..i]` and
`[0..n]`. Incremental state would make the test meaningless. A faster path later must pass an
equivalence test against the pure function.

### D-008 — Multi-leg `Signal` (deviation from §5)
**Why:** a strangle is not two independent trades. A call leg that fills while the put leg
rejects is a naked short call — a completely different risk. Atomicity must be expressible in
the intent, or the execution layer has to guess. **Awaiting approval, Q7.**

### D-009 — Broker tokens never enter `core/`
`OptionId` is `(underlying_future, option_expiry, strike, right, exchange)`. Angel One's
`symboltoken` / `tradingsymbol` live only in `execution/angelone/instruments.py`.
**Why:** §4's inward rule. A second broker becomes an adapter rather than a refactor, and a
reissued token cannot corrupt historical records.

### D-010 — Provenance mandatory on every venue constant
Every entry in `exchange/data/*.yaml` carries `effective_from` and `source`.
**Why:** §1. Session close times, lot specs, expiry rules and charge rates all change. A
number without a date and a source is an unfalsifiable claim.

### D-011 — Charge rates come from a real contract note, not from memory or blog posts
**Why:** while planning I found indicative MCX figures in secondary sources. I do not trust
them to the paisa, and CTT on the sell side of premium is a *per-entry* cost for a strategy
whose every entry is a sale. Being 30% wrong there would quietly bias every result. Until a
contract note is supplied, M3 does not report net P&L as authoritative. Q6.

### D-012 — Kill switch halts by default, does not flatten
**Why:** market-closing a short strangle during the fast move that tripped the limit can cost
more than the breach. Flattening is available and explicit, never the default.

### D-013 — Two stores: SQLite (WAL) live, DuckDB research
**Why:** the live path needs durable small writes and crash recovery; the research path needs
columnar scans over millions of recorded depth rows. One store doing both would be worse at
the one that matters more. Q13.

### D-014 — The winter stub bar is kept and flagged
US-DST months give exactly 29 bars; winter gives 29 plus a 25-minute stub.
**Why:** dropping it hides the last 25 minutes from square-off logic; merging distorts the
prior bar's OHLC. Flagging keeps both the data and the honesty. Q9.

### D-015 — `datetime.now()` banned outside `core/clock.py`
CI grep gate fails the build on `datetime.now`, `datetime.utcnow`, `time.time()` elsewhere.
**Why:** one stray wall-clock call makes backtests non-reproducible and makes replay diverge.
Enforcement beats discipline.

---

## Decisions arising from your answers (GOLDM / MCX / record-forward / fixed lots / NRML)

### D-016 — Devolvement and tender rules are hard rules, not configuration
No short option may be carried into its expiry session. No futures position may be carried
into the tender period. Both enforced in `risk/devolvement.py`, identically in backtest,
paper and live.
**Why:** an ITM short leg at MCX option expiry devolves into a GOLDM futures position, and
GOLDM futures go to compulsory physical delivery of 100 g of gold. That is not a P&L event,
it is a delivery obligation. A config toggle that can accidentally take delivery of gold is
not a feature. A test proves that disabling the rule is what produces the obligation — so the
rule is demonstrably what prevents it, not luck.

### D-017 — Session calendar keys off `America/New_York`, expresses in `Asia/Kolkata`
**Why:** the MCX evening close moves between 23:30 and 23:55 IST with **US** daylight saving.
Both zones are named; no offset is hardcoded. A dedicated calendar canary test asserts correct
bar counts and close times across both 2026 transitions, because this bug would otherwise
present as a silent one-bar-per-day error for eight months.

### D-018 — `pricing/forward.py` replaced by `pricing/underlying.py`
MCX options are options **on futures**, so `F` is directly observed.
**Why:** no synthetic forward, no put-call-parity reconstruction, no carry or dividend term.
The module shrinks to resolving an option's underlying futures contract. One of the few
things GOLDM made simpler. Requirement: the futures quote must be captured **synchronously**
with the chain snapshot, since a stale `F` silently corrupts every delta.

### D-019 — Milestone 1.5 inserted: read-only chain recorder
**Why:** you chose record-forward, so every day without a running recorder is data
permanently lost. The §9 build order assumes data exists at M1–M3; it does not. The recorder
is read-only — no order-placement code exists until M7 — so this does not weaken §2.1.
M2–M5 proceed against synthetic fixtures meanwhile.

### D-020 — The recorder captures depth and raw payloads, not just candles
Full bid/ask depth snapshots, the synchronous futures quote, every listed strike well beyond
0.25 delta, raw payloads archived verbatim, and both exchange and receipt timestamps.
**Why:** on a thin book the spread *is* the strategy's cost, and OHLC cannot reconstruct it.
Recording the wrong fields for six months is indistinguishable from not recording at all, and
unlike a code bug it cannot be fixed retroactively. Raw archives make a parser bug repairable.

### D-021 — Circuit (DPR) limits and book depth are modelled in the fill simulator
**Why:** a circuit-locked strike cannot be filled at any price, and a thin book cannot absorb
arbitrary size. A backtest that fills through a locked circuit or an empty book is lying at
precisely the moment the answer matters most.

### D-022 — Sample size is reported next to every metric
**Why:** a Feb/Apr/Jun/Aug/Oct/Dec cycle implies ~6 expiry cycles a year. At n≈6/year no
metric distinguishes skill from luck, and a Sharpe ratio printed alone implies a confidence
the data cannot support. If walk-forward has too few cycles to be meaningful, the report says
so rather than drawing the chart anyway.

### C-004 — The expiry rule is "last Friday of the month". I was wrong to dispute it.
**What I wrote:** that "last Friday" did not match the sources, which described option expiry
as 3 business days before the first tender day of a futures contract expiring on the 5th, and
that a weekday rule risked a two-day devolvement gap.
**What the terminal shows:** GOLDM option chain, expiry **28 Aug 2026** — which is a Friday,
and the last Friday of August 2026. The stated rule is correct.
**Why I got it wrong:** I derived a rule from secondary sources describing the tender-period
chain, and treated the derivation as more authoritative than the user's direct observation of
the instrument. The derivation may describe an older regime, a different contract, or I may
simply have chained it wrong. Either way the terminal is the exchange's own answer.
**What does not change:** D-023 stands and is, if anything, reinforced — a derived rule was
wrong here, which is exactly why the system reads expiry from the instrument master rather
than computing it. "Last Friday" becomes the *cross-check heuristic* (one confirmed data
point, to be validated across more months as the recorder runs), not the source of truth.

### D-023 — Expiry dates are **read**, never computed from a weekday rule
`exchange/expiries.py` sources option and futures expiry dates from the Angel One instrument
master. The derived rule (futures expire 5th of month → tender starts ~3 business days prior →
option expires 3 business days before the first tender day) exists **only as a cross-check
that raises an alarm on mismatch**, never as the source of truth.
**Why:** the stated rule "last Friday of every month" appears to be wrong. For May 2026 the
sources point to option expiry on Wednesday 27 May, while the last Friday is the 29th. A
system exiting on the 29th would be acting on a contract that expired two days earlier — and
those two days are exactly the devolvement window, where an ITM short leg has already become a
GOLDM futures position bound for physical delivery. A weekday rule is precisely how the worst
failure this system can have would occur. The instrument master is what the exchange acts on;
a rule is an opinion about what the exchange will do.
**Follow-on:** if the instrument master and the derived rule ever disagree, the engine halts
new entries and alerts rather than picking one.

### D-024 — Stop-viability check: refuse a stop that is smaller than the cost of trading
At startup and at every entry, the risk layer compares the configured stop against modelled
round-trip cost (4 spread crossings + brokerage + CTT + exchange + stamp + GST) and **rejects
the configuration** if the ratio is below a configurable multiple, default 3×, logging both
numbers.
**Why:** on the tightest reading of "1% of investment" the stop lands near ₹1,000/lot while
round-trip friction on a thin GOLDM option book plausibly runs ₹500–1,500/lot, dominated
entirely by the spread. A position that opens at its stop is not a strategy. This is exactly
the class of error §1 of the brief exists to prevent, and it is cheap to detect at startup and
expensive to discover in an equity curve.

**Amended after Q4a was answered.** The margin basis was chosen explicitly, with the cost
arithmetic in front of the decision. So the check **defaults to `warn`, not `refuse`**: it
logs the stop and the modelled round-trip cost and their ratio at startup and on every entry,
and proceeds. Reasons: (i) the decision was made informedly and it is not the engine's place
to veto it; (ii) my ₹500–1,500 friction figure is a placeholder — the recorder will replace it
with a measured spread within weeks, and blocking a system on an estimate I invented would be
worse than reporting the ratio honestly. `refuse` remains available in config. The ratio is
recorded on every trade so cost drag can be attributed later rather than argued about.

### D-026 — Entry at the 09:30 bar close, one bar, time-based
Entry is evaluated only at the close of the first 30-minute bar (09:00–09:30 IST). No filter.
**Why:** stated requirement. Worth recording alongside it that this is the **quiet** part of
the GOLDM day — international gold price discovery happens in the evening session that
overlaps COMEX (C-001), so a 09:30 entry deliberately sells premium into the Indian morning
rather than into the session where the instrument actually moves. It also means the entry
absorbs the overnight gap before acting, which cuts both ways. Neither observation is an
objection; both belong in the record so the backtest's session-of-day attribution can be read
against them.

### D-025 — Exit levels are resolved to absolute ₹ once, at entry, and frozen
A `ComboExit` expressed as a percentage is converted to an absolute rupee level at entry,
written into the `Signal.context`, and never recomputed.
**Why:** a level that floats with live equity would make the same trade exit at a different
price because of unrelated P&L elsewhere in the account, which breaks reproducibility and
makes the trade log impossible to reason about after the fact.

---

## Decisions made while building Milestone 1

### D-027 — Parquet stores prices as strings; a float column is refused on read
`write_parquet_bars` writes `open/high/low/close` as strings, and `read_parquet_bars`
raises if it finds a float column, naming the remedy.
**Why:** float64 cannot represent every tick-grid price exactly. Reading a float and
converting it to `Decimal` afterwards does not recover the lost bits — it launders them.
The failure would surface several layers away as a tick-grid rejection with no visible
cause. Strings compress well enough that the cost is not worth arguing about.

### D-028 — `dec()` refuses a float argument outright
**Why:** `Decimal(0.1)` is legal Python and returns
`0.1000000000000000055511151231257827`. Nothing in the type system stops it, so the
refusal has to be explicit. If a float reaches money math, the bug is upstream and
silently accepting it here would hide it.

### D-029 — `bars_in_session` comes from the calendar, never from counting the data
Found while writing the look-ahead canaries: the first version of
`contexts_from_bars` counted how many bars each session contained by scanning the whole
series, then handed that count to the strategy.
**Why it was wrong:** that tells a strategy how many bars today will have *before the day
has finished*. It is knowledge unavailable live, so it is look-ahead however innocuous it
looks — a strategy could behave differently on a 29-bar day than a 30-bar one before
knowing which it was in. The count now comes from `calendar.bar_boundaries()`, which is
knowable in advance and is what a live session would use.

### D-030 — `TRADING_MODE` is matched exactly: no `strip()`, no `lower()`
**Why:** §2.1 says "no default, no fallback". Lenient parsing is a fallback. This is the
one check standing between the engine and a real account, so `TRADING_MODE=LIVE ` with a
stray shell space is refused — and the error quotes the value back with `repr()` so the
space is visible rather than mysterious.

### D-031 — The calendar refuses dates beyond its verified holiday range
A `MarketCalendar` with no sourced holiday file, or a query past `verified_through`,
raises rather than answering. Tests use an explicitly named `synthetic_calendar()`.
**Why:** the alternative default is "assume it is a trading day", which silently backtests
trades on holidays. Naming the synthetic constructor explicitly means an unverified
calendar cannot reach a real run by accident.

### D-032 — mypy runs against the ambient interpreter, unpinned
**Why:** numpy ships stubs using 3.12 `type` syntax, so pinning `python_version = "3.11"`
makes mypy fail inside site-packages before reaching our code. The package still targets
3.11+ and uses nothing newer than `StrEnum` and `datetime.UTC`.

---

## Decisions made while building Milestone 2

### D-033 — Milestone 2 is `pricing/`, not `indicators/`
The brief's M2 is "Indicators — only what `<<STRATEGY>>` needs". What this strategy
needs turns out to be no indicators at all: entry is time-based at the 09:30 bar with
no filter (D-026), so there is no moving average, oscillator or swing detector in the
design. What it actually needs is Black-76, an implied-volatility solver and greeks, in
order to find the 0.25-delta strikes.
**Why this is worth recording rather than quietly substituting:** inventing indicators to
fill the milestone slot would have added parameters nothing asked for, and every added
parameter on a ~12-trade-a-year strategy is a curve-fitting opportunity. `indicators/`
still exists with its lag-declaring protocol, empty, for whenever a filter is specified.

### D-034 — `pricing/forward.py` exists after all. D-018 was right in theory, wrong in practice.
**What D-018 said:** the forward module collapses to nothing, because MCX options are
options on futures and `F` is observed rather than reconstructed.
**What building it revealed:** inverting the live 28 Aug 2026 chain against the futures
price shown on the terminal (1,56,640) produced put volatilities roughly 0.3 points
*above* call volatilities at **every single strike**. A one-sided error at every strike is
not noise — it is the signature of a wrong `F`. Solving put-call parity for the forward
instead gives a tight cluster: 156,604 / 156,607 / 156,611 / 156,615 / 156,617 across the
five strikes with two-sided quotes. Median 156,611, against a stated 156,640. **Thirty
points.**
**Why it matters more than thirty points sounds:** a wrong forward biases every delta in
the chain in the same direction, which biases strike selection in the same direction, on
every trade, forever. It would never show up as an error — just as a strategy that
consistently sells slightly the wrong strikes.
**Decision:** `forward_from_parity()` and `implied_forward()` compute the chain's own
implied forward as a model-free cross-check, and the gap is reported rather than absorbed.
The check returns "consistent" when no strike quotes both sides, because absence of
evidence must not halt the engine on a thin day — `pairs_used` distinguishes the two cases.
**Open:** what the 1,56,640 on the terminal actually was. See Q1e.

### D-035 — Each chain row is priced off its own solved volatility, not a flat ATM vol
**Why:** the live chain has a real skew — call volatility rises monotonically from 21.53%
at 155000 to 22.51% at 159000. Pricing the wings off an ATM volatility would misprice
precisely where a 0.25-delta strangle lives. The consequence is visible in the tests: the
159000 call is 0.342 delta on its own volatility and 0.336 on a flat one. Both numbers are
arithmetically right; the market's is the one that decides which strike gets sold.

### D-036 — Bisection, not Newton, for implied volatility
**Why:** bisection is unconditionally convergent here (option price is strictly monotonic
in volatility) and runs a bounded, deterministic number of iterations. Newton is faster and
can overshoot into negative volatility on a deep out-of-the-money quote — a once-a-month
failure that is impossible to reproduce afterwards. Speed is not the constraint on a
strategy that trades twelve times a year; reproducibility is (§7.4).

### D-037 — `math.erf` for the normal CDF, not a polynomial approximation
**Why:** exact to double precision and identical across platforms. The Abramowitz-Stegun
polynomial is accurate to ~7.5 decimal places, which sounds ample until a delta of 0.2499
versus 0.2501 selects a different strike. A backtest that picks different strikes on
different machines is not reproducible, and reproducibility here is a hard requirement.

---

## Decisions made while building Milestone 3

### D-038 — A signal from bar `i` executes at bar `i+1`'s **open**
**Why:** filling at bar `i`'s own close would let a decision taken from a bar
profit from that same bar. It is the subtlest look-ahead there is, because every
number stays plausible. The alternative costs a little realism on fast markets and
buys a structural guarantee, which is the right trade for a system whose §1 is
"this cannot silently produce a wrong number." Asserted directly:
`test_no_order_fills_on_the_bar_that_produced_it`.

### D-039 — Fills round **against** us; limit placement rounds **for** us
Two functions, deliberately opposite. `quantize_to_tick` places a limit
conservatively (a BUY limit rounds down, so it cannot accidentally cross the
market). `worst_tick_for_fill` prices a fill we already have, where conservative
means the worse side of the tick (a BUY fills higher).
**Why:** rounding a fill the friendly way is a free fraction of a tick on every
single trade. On a strategy that pays the spread twice per round trip that is
exactly the sort of quiet flattery a backtest should not contain.

### D-040 — Positions store an exact **cost basis**, not an average price
**How it was found:** `Portfolio.check_identity` — which re-checks equity two
independent ways after every fill and every mark — failed on its first run against
the coin-flip strategy with a discrepancy of **1e-21**. The cause was
`(p1·q1 + p2·q2) / total` in the weighted-average calculation: `2000/3` has no
exact decimal representation, so a position built from three fills carried a
rounding error into every subsequent P&L figure.
**The fix:** store the exact total paid or received and derive an average only for
display. `unrealised_pnl` is computed as `(mark·|qty| − cost_basis) · direction ·
multiplier`, with no division anywhere in the money path. On a partial close the
basis is split proportionally and the **remainder taken by subtraction**, so the
two parts always sum back to the original exactly — the realised/unrealised split
can round by a paisa, but their total cannot drift.
**Worth noting:** the identity check found this on the day it was written. Without
it the drift would have sat in the equity curve indefinitely, far too small to
notice and far too strange to explain.

### D-041 — A strategy reads its position from the context, never from its own memory
**How it was found:** the first coin-flip tracked which side it had asked for. When
the risk layer refused an order — as it did, because the reopen half of a
close-and-reopen pair tripped the concurrency cap — the strategy carried on as
though it had been filled, emitted closes for positions that did not exist, and
quietly accumulated a three-lot position. That accumulation is what tripped D-040.
**The rule, now applying to every strategy:** the portfolio is the source of truth
about what is held; a strategy's memory of its own intentions is not. In live
trading the same divergence arrives through rejections and partial fills, where it
would be considerably harder to see.

### D-042 — A metric that cannot be computed returns `None`, never zero
Sharpe below four observations, Sortino with no losing periods, Calmar with no
drawdown, cost drag on zero gross P&L.
**Why:** a Sharpe ratio of 0.0 reads as "no edge"; `None` reads as "not enough
data", and with ~12 trades a year the second is almost always the truthful one.
`Metrics.summary()` also prints the trade count beside every ratio and appends an
explicit caveat below thirty trades.

### D-043 — Observation: on futures, charges dwarf the spread at GOLDM notional
Running the falsification with the placeholder MCX rates, a 29-bar session cost
**₹140 in spread and ₹4,690 in charges**. The reason is the base: futures CTT and
exchange charges apply to *notional* (~₹15.7 lakh per lot), while option charges
apply to *premium* (~₹15,000 per lot). Same rates, a hundredfold difference in
what they are charged on.
**Consequence:** the option-vs-futures distinction in the charge model is not
cosmetic, and the `is_option` flag must never be defaulted. Also a reminder that
the placeholder rates make these dominate — which is precisely why a real contract
note is a blocking question (Q6) rather than a nicety.

---

## Decisions made while building Milestone 4

### D-044 — Margin is taken for the **combo**, not summed across legs
**How it was found:** the end-to-end test asserted a ₹1,000 stop (1% of a ₹1 lakh
margin) and got ₹2,000. The engine was summing per-leg margin across the two legs.
**Why it matters far more than it sounds:** SPAN nets a strangle's legs against each
other — they cannot both go wrong at once — so summing them roughly doubles the
margin figure. And the configured stop is **1% of margin**, so an overstated margin
silently doubles the stop distance and turns the strategy into a different one. A
margin model is normally a capacity constraint; here it sets the exit.

### D-045 — The strategy's cadence is recorded from **fills**, not from emitted signals
"One strangle per expiry cycle" needs the strategy to remember which cycles it has
traded. That set is populated in `on_fill`, not when the signal is emitted.
**Why:** the risk layer can refuse an entry — the devolvement guard and the kill
switch both do. A cadence counter that ticked on *intent* would mark a cycle as
traded that the account never entered, and skip it. Same failure class as D-041,
caught by the end-to-end test finding six sell fills where it expected two.
**Consequence for live:** the traded-cycle set is genuine strategy state and must be
persisted for a restart to behave correctly. Carried into Milestone 6.

### D-046 — The devolvement exit deadline is the session the exit **happens on**
**How it was found:** the forced-exit tests never fired. `requires_option_exit` used
`on > exit_deadline`, so with a Thursday deadline the position was still open through
Thursday and only demanded an exit on Friday — the expiry session itself.
**The fix:** `on >= exit_deadline`. The deadline is the day the exit is placed, not
the last day of grace. Reading it the other way leaves a short option open through
its own expiry session, which is exactly the state that devolves. Applied to the
tender guard too.
**Worth noting:** the unit tests passed with the wrong semantics, because they
encoded the same misreading. Only the end-to-end run — which actually had to close a
position before a real date — exposed it.

### D-047 — The risk layer is evaluated before the strategy, within each bar
Forced pre-expiry exits and kill-switch checks run at step 2 of the bar; the strategy
is consulted at step 4; and a risk-layer exit discards whatever the strategy asked
for.
**Why:** a forced exit is not a suggestion the strategy may decline, and a kill switch
that only takes effect on the next bar has not stopped the bar it tripped on.

### D-048 — A missing mark is an error, never a zero
`require_mark` raises when an open position has no price at a bar.
**Why:** marking a missing price at zero shows a **short** option as fully profitable
on exactly the bar the feed dropped out — the most dangerous possible direction for
that error. This fired during development when the chain generator stopped quoting
deep out-of-the-money strikes near expiry; the generator was wrong (real exchanges
quote them at the minimum tick), and the check caught it rather than producing a
plausible, wrong equity curve.

### D-049 — One engine, two price sources
The single-instrument futures path and the multi-leg options path differ only in a
`PriceSource`. `BarPriceSource` marks at the close and fills at the open;
`ChainPriceSource` reads both from chain snapshots.
**Why:** brief §4 — "if backtest and live use different code paths for anything
except I/O, you have built two systems that will disagree." A second engine loop for
options would have been easier and would have started drifting immediately. The M3
falsification suite still passes unchanged through the generalised engine, which is
the evidence that the abstraction did not change behaviour.

### D-050 — Strategies leave notes; the engine logs them
`Strategy.note()` and `drain_notes()`, surfaced on the result as `notes`.
**Why:** brief §8 requires a skipped trade to be logged, and a strategy has no I/O.
Without this, "no strike was quoted at 0.25 delta" and "the strategy chose not to
trade" are indistinguishable in the output — and for this strategy on this book, the
first is the case that actually matters.

---

## Decisions made while building Milestone 5

### D-051 — `WalkForwardReport` has no combined metric, and cannot be given one
Brief §10: "Always report in-sample and out-of-sample side by side. Never report a
single blended number." Enforced structurally — there is no `overall` field and no
property that would compute one, and a test asserts the attribute does not exist.
**Why:** averaging a number the parameters were fitted to with a number they were
not is worse than reporting either alone. Leaving the field out is the only version
of that rule that survives someone being in a hurry later.

### D-052 — Out-of-sample windows may never overlap, and `rolling_windows` refuses
A step smaller than the validation window is rejected, not silently allowed.
**Why:** overlapping validation periods count the same trade as evidence twice,
which inflates the apparent sample exactly where the sample is the binding
constraint. A step *larger* than the window is refused too, since it would leave
days validated by nothing.

### D-053 — Instability is measured by flip rate, not by counting distinct values
The brief asks to "flag any parameter whose optimal value jumps around between
windows". `flip_rate` — the fraction of window-to-window transitions where the value
changed — distinguishes a parameter that moved once and settled (a possible regime
change) from one that alternates every window (noise). Counting distinct values
collapses those two into the same verdict and makes the flag useless.
The chosen values are printed as a sequence so the reader sees the wobble rather
than taking a verdict on trust.

### D-054 — The walk-forward compares optimising against **not** optimising
Every window is also evaluated out of sample with a fixed parameter set that was
never fitted to anything. If per-window optimisation does not beat leaving the
parameters alone, the optimisation is fitting noise, and the report says so in
those words.
**Why:** this is the single most informative line in a walk-forward and the easiest
one to omit. Without it, a report showing positive out-of-sample P&L looks like
evidence for the optimisation, when it may be evidence only that the strategy works
and the optimisation is along for the ride.

### D-055 — The report leads with whether it can support a conclusion at all
`Feasibility` grades a run INSUFFICIENT / THIN / ADEQUATE from the out-of-sample
trade count, and `summary()` prints that verdict above any number.
**Why, concretely:** at ~12 trades a year, two years of recorded data over 180/90-day
windows yields **6 windows and about 18 out-of-sample trades**. Reaching 30 would take
roughly 2.5 years of *validated* data, and longer still of recording, because the
first in-sample window is consumed before any validation starts. That is the honest
answer to "will walk-forward tell me anything", and it is better delivered before
months of recording than after. Exposed as `algo walkforward` so the answer can be
checked against different cadences and window sizes.

---

## Decisions made while building Milestone 6

### D-056 — The journal records SENT **before** the network call, never after
This single ordering is the whole crash-safety design.

* Write SENT, then call. A crash in between leaves an ambiguous SENT, and
  reconciliation asks the broker what actually happened.
* Call, then write SENT. A crash in between leaves JOURNALLED while the broker
  holds a live order — and the obvious recovery ("not sent yet, send it") doubles
  the position.

**The safe ambiguity is "might have been sent". The unsafe one is "looks unsent".**
A test asserts the journal reads SENT at the moment the broker is entered, so the
ordering cannot be quietly inverted later.

### D-057 — An unconfirmable order halts trading; it is never resent
When an order is marked SENT but the broker has neither the order nor an execution
for it, the reconciler raises `UNCONFIRMED_ORDER` and the router refuses to trade
until a human resolves it.
**Why:** an order missing from the broker's open-order list may never have arrived
**or** may have filled and been cleared. Those look identical from outside, and
resending on that ambiguity doubles the position — for a short strangle, doubling
an unbounded risk. This is a deliberately inconvenient default; the convenient one
loses money.

### D-058 — A retryable error does not trigger a retry
`RetryableBrokerError` leaves the order in SENT and reports `UNCONFIRMED`. Nothing
retries automatically.
**Why:** retrying a request that may already have been accepted is the same mistake
as resending, dressed up as resilience. The classification is still useful — it
tells the operator the venue is reachable-but-unhappy rather than rejecting — but
the action is to reconcile, not to try again.

### D-059 — Trading is blocked until a reconciliation has run **and come back clean**
`is_safe_to_trade` is false on a fresh router, not just after a detected problem.
**Why:** §2.3 says reconcile before sending anything, and "anything" includes the
first order after a restart — precisely when local state is least trustworthy. A
router that trusted itself until proven wrong would have exactly one unguarded
order, at the worst possible moment.

### D-060 — The paper broker keeps its own books, persisted separately
`PaperBroker.save()` / `restore()` write to their own file, independent of the
journal.
**Why:** a real broker remembers your orders when your process dies. A paper broker
living only in engine memory would make crash recovery untestable, because the
thing recovery has to reconcile *against* would vanish at the same moment. Saving
separately reproduces the asymmetry a real crash creates — broker remembers, engine
forgets — which is the only version of the scenario worth testing.

### D-061 — The paper broker rejects a duplicate client order id
Mirroring what a real venue does, rather than silently accepting a second order.
**Why:** it turns the router's idempotency from an assumption into something the
tests can actually falsify. If the router ever did resend, the broker would say so
rather than quietly opening a second position.

### D-062 — Fill adoption is idempotent on `fill_id`
Every reconnect replays the day's executions; `record_fill` returns False for one
already stored.
**Why:** this is the specific mechanism behind brief §11's "assert no duplicate
fills". A test reconciles three times in a row and asserts the fill count stays at
one.

### D-063 — Position drift blocks trading
The reconciler compares the broker's positions against the positions our recorded
fills imply, and any difference is a blocking drift.
**Why:** an order discrepancy can be resolved by hand at leisure. A position we do
not know about is risk we are not managing, and every downstream calculation —
margin, stop level, kill-switch equity — is wrong while it stands.

---

## Decisions made while building Milestone 8

### D-064 — The API reads a file; it never holds the engine
The engine writes to a SQLite state file, the API only reads it. The API process
has no `Portfolio`, no `OrderRouter` and no broker connection.
**Why:** a web framework holding live trading objects is one bug away from
mutating trading state to serve an HTTP request. A dashboard that can move a
position is not a dashboard. A test enumerates the routes and fails if a second
mutating endpoint ever appears — which is the only form of this rule that survives
someone adding a convenient "close position" button later.

### D-065 — The kill switch is a *request*, not an action
`POST /kill-switch` writes a row saying a halt was asked for and returns **202**.
The engine trips its own switch on its next bar and marks the request acted on.
**Why:** the API never touches the switch, so a dead API cannot leave the engine
half-tripped, and a dead engine cannot swallow a halt — the request is still
sitting in the table when it comes back. The 202 is not decoration either: a UI
that showed "halted" the instant the call returned would be lying for as long as
the engine took to notice, at exactly the moment an operator most needs the truth.
The dashboard therefore reports "halt requested", never "halted".

### D-066 — There is no reset endpoint
Tripping the switch is one click. Clearing it is `algo killswitch --reset` at a
terminal.
**Why:** un-tripping a halt deserves a look at why it tripped and a person who has
done that looking. Q21 settled the same way for parameters: they change through
config and a restart, so every live setting traces to a committed file rather than
to something typed into a browser once.

### D-067 — The API token never reaches the browser
The Next.js **server** holds `ALGO_API_TOKEN` and proxies every call through
`app/api/state/[...path]`. The variable deliberately has no `NEXT_PUBLIC_` prefix,
because that prefix inlines a value into the browser bundle.
**Why:** the token guards the kill switch. Shipping it to the browser would put a
trading halt behind devtools. The proxy also carries an explicit allow-list of
paths — without one, a path parameter is an open proxy from the browser into
whatever else the server can reach.

### D-068 — The API refuses to start without a token, and binds to localhost
`create_app` raises if `ALGO_API_TOKEN` is unset; `algo serve` defaults to
127.0.0.1 and prints a warning when told to bind wider.
**Why:** serving an unauthenticated kill switch to anything that can reach the port
is not a default worth having. Exposing it should be a decision, not an accident.

### D-069 — Money crosses the wire as strings and is never parsed
The API serialises every `Decimal` as a string, and the TypeScript types declare
them `string`. They are converted to `number` in exactly one place: computing SVG
path coordinates for the equity chart.
**Why:** JavaScript numbers are float64, so `Number("1000000.05")` is
1000000.0499999999. Parsing at the last step would undo the whole `Decimal`
discipline at the point the operator actually reads the figure. A pixel of
rounding in a chart is invisible; a paisa in a reported number is not.

### D-070 — No charting library
The equity curve and the underwater plot are hand-drawn inline SVG — roughly thirty
lines of path arithmetic.
**Why:** a chart package would add several hundred transitive dependencies to a
page running on the same machine as a trading engine. The whole dashboard has
four runtime dependencies. Brief §10 asks for the underwater plot specifically,
and depth and duration are different experiences that one equity line shows
neither of clearly.

### D-071 — `allowedDevOrigins` covers both `127.0.0.1` and `localhost`
Found by actually opening the page: Next's dev server treats the two spellings as
different origins and blocks its own JavaScript chunks across them. The result is a
page that serves its shell with no client code — which looks exactly like an engine
that is up and reporting nothing. Both spellings are allowed so the failure cannot
happen either way round.

### D-072 — Next was upgraded off the version with a published CVE
`npm install` flagged a security advisory against `next@15.1.6`. Upgraded to
16.3.1; `npm audit --omit=dev` now reports zero vulnerabilities.
**Why:** shipping a known-vulnerable dependency in the process that fronts a
trading halt is not a trade-off worth making for a pinned version number.

---

## Closing the gaps against the brief

### C-005 — The dashboard pointed at a command that did not exist
The kill-switch panel and the README both told the operator to run
`algo killswitch --reset`. There was no such command — the CLI had `verify`,
`config`, `backtest`, `walkforward` and `serve` and nothing else. So the only
documented way to clear a halt was a dead end, in the one part of the system whose
whole purpose is to work when things have gone wrong. Now implemented with
`--trip`, `--reset` and a default status view.

### D-073 — A strangle is **one** trade, not two
`TradeBuilder` groups every fill between "flat" and "flat again" into a single
`Trade` with multiple legs.
**Why:** reporting the call and the put separately would show one winner and one
loser on a position that was always a single bet, which would corrupt the win
rate, the profit factor and the R-distribution simultaneously. Until this was
built, `core.trade.Trade` was fully defined and entirely unused — the trade log
was permanently empty and every §10 statistic needing a round trip was absent.

### D-074 — Trade P&L is differenced from the portfolio, not recalculated
`gross_pnl` is the portfolio's realised figure at close minus its value at open.
**Why:** exactly one piece of code knows how a round trip closes out
(`Position.apply`). A second implementation in the reporting layer would agree
for a while and then quietly stop agreeing, and the two numbers would both look
plausible.

### D-075 — R is the configured stop; no stop means no R-multiple
Assumption 7.4, now enforced. A trade opened without a stop carries `r_multiple:
None`, and the R statistics report **how many trades they could actually use**.
**Why:** a short strangle's maximum loss is unbounded, so an R measured against it
is meaningless — and averaging a subset without saying so overstates what the
number covers.
**Observed immediately:** the first real strangle run stopped out at **−1.32R** on
a 1R stop. The exit is evaluated at a bar close and fills at the next bar, so the
position kept moving. Stops do not stop at 1R, and the R-distribution now shows
that rather than assuming it away.

### D-076 — A position still open when the data ends is not a trade
`TradeBuilder.abandon()` discards it.
**Why:** counting it would put an unrealised figure into a realised statistic. The
open position is reported separately instead.

### D-077 — Export is byte-stable; formatting lives in the tearsheet
The CSV writer uses sorted columns, `str(Decimal)` rather than a format string, one
fixed UTC timestamp form, and an explicit `lineterminator="\n"` — because the
default on Windows produces `\r\r\n` and a golden file that depends on the
operating system is not a golden file.
**Why:** every "nicety" on the way out — two decimal places, thousands separators,
a rounded R — makes the file prettier and the diff useless.

### D-078 — Regenerating the golden file is a deliberate act
It only happens under `ALGO_UPDATE_GOLDEN=1`, and the test then *skips* with a
message telling you to read the diff before committing.
**Why:** a test that silently rewrites its own expectation whenever the code
changes is not a test. There is also a guard-the-guard assertion that the fixture
actually trades — a golden file of zero trades would pass forever.

### D-079 — The tearsheet is one self-contained HTML file
Inline SVG, no matplotlib, no chart library, nothing fetched when it opens. Asserted
by a test: no `http`, no `https`, no `<script>`.
**Why:** a tearsheet you can email, open on a machine with no Python, and still read
in five years beats a prettier one that needs an environment. The caveats render
*above* the numbers, because placeholder rates change what every figure below them
means and a footnote is where that goes to be ignored.

### D-080 — CI enforces the project's own rules, not just the tooling's
Beyond ruff, mypy and pytest, the workflow greps for wall-clock reads outside
`core/clock.py` (D-015), `float(` in money paths (§2.5), and swallowed exceptions
(§12) — and fails the build on any of them.
**Why:** ruff cannot know these are rules. Written down in a decision log they are
guidance; enforced in CI they are constraints. The dashboard job runs `tsc`,
`next build` and `npm audit` for the same reason.

### D-081 - Bhavcopy is a second historical feed, not a replacement for the recorder
MCX's daily contract-wise file is loaded alongside SmartAPI rather than folded into
it. SmartAPI serves GOLDM *futures* bars; Angel One state plainly that "data of
expired contracts is not stored", so it can never serve an option cycle that has
already settled - which is every cycle worth testing. The bhavcopy covers expired
contracts back to 2016.
**Why:** the two feeds have different granularity and different caveats, and folding
them together would let a daily number be mistaken for a 30-minute one. Keeping
them apart means every result carries the provenance of the data behind it. This
turns "wait two and a half years for the recorder" into "about a hundred monthly
cycles, today" - but a bhavcopy backtest is a **shape test**, not a fill-accurate
one, and is labelled as such wherever it is reported.

### D-082 - The bhavcopy column mapping is declared data, and unverified
`MCX_DEFAULT_COLUMNS` is a `BhavcopyColumns` model, not names hardcoded in the
parser. `parse_rows` validates the header on load and, on a mismatch, raises with
the columns the file actually has beside the ones it wanted.
**Why:** MCX serves the file through a browser flow behind Akamai bot protection.
Three attempts to fetch a sample were refused with 403, so the mapping is a stated
assumption and is documented as one. At M0 the rule was set that "a loader written
against an invented schema is a week of wasted work" - the compromise that honours
it is a loader that cannot silently mis-parse: correcting the mapping against a real
file is a config change, and `algo bhavcopy <file>` exists to make that check the
first thing anyone does.

### D-083 - Assuming a spread on end-of-day data is an explicit, separate function
A bhavcopy chain has no bid or ask, so no row passes the engine's tradeability test -
correct, and it would otherwise mean no backtest could run at all. `assume_spread()`
synthesises a book, and it is a call site you can see rather than a flag. Rows with
zero volume get no book at all.
**Why:** the alternative was loosening `is_tradeable`, and that rule protects the
live path. Inventing data is sometimes necessary; doing it invisibly is not. The
zero-volume rule matters more than the spread itself: a strike that was listed but
never traded is the most valuable thing this dataset says about a thin ladder, and
handing it a synthetic book would erase exactly that signal.

### D-084 - A chain with no futures close for that session is dropped, not patched
Options are priced off the future. If the file has no FUTCOM row for a trade date,
`build_snapshots` skips the whole chain rather than carrying the previous day's
forward or interpolating one.
**Why:** every delta in the snapshot is computed against that number, so a
substituted forward would not produce a slightly wrong 0.25-delta strike - it would
produce a confident one. Missing data should be absent, not approximated. The same
reasoning as the look-ahead firewall: the wrong number is worse than no number.

### D-085 - Bhavcopy snapshots are stamped at the session close
Not midnight. These are closing prices, so the snapshot timestamp is 23:30 IST
(or 23:55 outside US daylight saving) on the trade date.
**Why:** dating a closing price to 00:00 would place the entire chain *before* the
session that produced it, which is a look-ahead the firewall cannot catch because
the timestamps would be internally consistent.

### D-086 - CLI output stays ASCII
Typer prints command docstrings as `--help` text. On this machine the console is
cp1252, which rendered every em dash in the CLI as a replacement character and would
raise `UnicodeEncodeError` on a rupee sign. `algo/cli/main.py` is now ASCII and CI
greps to keep it that way. Prose in docs and non-CLI modules keeps its typography.
**Why:** the alternative - reconfiguring the console code page at startup - leaks
into the user's shell after the process exits and can break other programs in that
window. The failure showed up on the error path, which is the last place that can
afford to be unreadable or to crash.

### D-087 - Directory ignores are anchored to the repository root
`.gitignore` had bare `data/`, `runs/`, `state/` and `logs/`. A bare directory pattern
matches at *every* level, so those four lines were silently excluding real source:
the whole of `algo/data/` (twelve modules, including both broker feeds, the resampler
and the validator), `algo/exchange/data/` (the GOLDM contract spec and the MCX charge
rates), and `dashboard/app/api/state/` (the proxy that keeps the API token out of the
browser). Nothing in those paths had ever been committed. They are now `/data/`,
`/runs/`, `/state/`, `/logs/`, which is what was meant.
**Why:** a clone of this repository would not have imported. The pattern also hid the
data layer from `ruff`, which respects `.gitignore` by default - `ruff check .` had
been passing without ever reading those files, so CI was green over code it could not
see. Fifteen real lint errors surfaced the moment the ignore was corrected. The
lesson generalises past this bug: a check that silently covers less than you think is
worse than no check, because it also removes the suspicion that would find the gap.

### D-088 - The dashboard is fed by the engine, opt-in, through one state file
`algo backtest --state state/dashboard.db` attaches a `StateStore` to the engine.
The engine writes equity, positions, signals, notes, completed trades and health
after each bar, and consumes halt requests recorded in the same file at its next
bar. Without `--state` the engine behaves exactly as before - the wiring is
invisible when unused, and a test proves that attaching a store changes nothing
about the result a run produces.
**Why:** the dashboard had to be able to show the state of a *run*, and the only
honest source of that state is the engine that produces it. Recording it in the
same file the API already reads keeps one source of truth instead of two. The
kill-switch direction (dashboard -> engine) stays narrow: a halt is a request the
engine chooses to honour on the next bar, never a mutation of running state (the
same reasoning as D-064/D-065), and the flatten that accompanies a requested halt
is an explicit part of the request, not something the engine invents.

### D-089 - Sizing is fixed lots until a sizing rule is actually implemented
`SizingConfig` now accepts `mode: fixed_lots` only; the `margin_pct` and `risk_pct`
modes that `config/goldm.yaml` advertised are gone from both schema and file.
**Why:** those modes were never implemented - a config file that names an
unimplemented sizing rule is a promise the engine does not keep, and D-024 shows
what happens to a stop that is a percentage of an *approximate* margin. When a
real sizing rule lands, the schema grows with the implementation, not before it.

### D-090 - Roadmap-only modules stay until their milestone arrives
`algo/data/feed.py`, `csv_feed.py` and `parquet_feed.py` (M1.5/M7 feeds) and
`algo/execution/paper.py` (the paper broker - the path said `algo/strategy/`
until D-115; the module was always in `execution/`) are not dead code: they are
the load bearing future of a repository whose tests replay recordings, and
`synthetic_chain.py` is the strangle suite's fixture. They stay, and keep their
importers in the test suite.
**Why:** deleting them would save a few modules today and re-create them against
a changed interface tomorrow. The cost of keeping code that is tested and imported
is lower than the cost of re-deriving decisions that were already made (D-019).

### D-091 - The real strategy now runs against real history: `backtest-bhavcopy`
`algo/backtest/bhavcopy_runner.py` builds everything `BacktestEngine` needs -
bars, a chain provider, an expiry table - directly from bhavcopy rows, and
`algo backtest-bhavcopy` runs `DeltaStrangle` itself against it. Until this, no
runnable command in the repository had ever executed the actual strategy: the
`backtest` command is hardwired to `coin_flip`/`buy_and_hold` on synthetic bars
(the M3 falsification, by design), and `DeltaStrangle` had only ever run inside
`tests/test_strangle_end_to_end.py`.
**Why:** the strangle's edge, if any, cannot be assessed from a component that
has never seen a real cycle. This is the first command that can - see D-092 for
what it honestly cannot yet tell you.

### D-092 - Bhavcopy backtests use two bars a session: entry (09:30, day's open) and close
Bhavcopy is end-of-day. There is no 09:30 print and no intraday grid, so rather
than fabricate one, each session becomes exactly two ticks: entry, stamped 09:30
IST and priced from the day's open (the closest real proxy to what the fixed
entry gate would have seen), and close, stamped at the real session close (read
from a `MarketCalendar`, DST-correct) and priced from the day's close, high and
low. Every exit check in between - a stop that would have fired and reversed by
the close - is invisible to the run, and the CLI command prints this as a
standing warning on every result, not a footnote.
**Why:** this is what makes `backtest-bhavcopy` a **shape test** (has the
strategy ever come out ahead across many real cycles) rather than a
fill-accurate one (what it would actually have been filled at) - the same
distinction D-081 through D-085 already draw for the loader itself, carried
through to the engine that consumes it.

### D-093 - CLI output ASCII stayed local to `main.py`; the mojibake was not
D-086 made `algo/cli/main.py` ASCII and added a CI grep for it. Building
`backtest-bhavcopy` surfaced that the em dashes, section signs and `±` in
*runtime message strings* elsewhere - warnings, raised errors, note text in
`algo/backtest/engine.py`, `algo/risk/*.py`, `algo/strategy/delta_strangle.py`,
and ten more modules - reach the exact same cp1252 console the CLI guard was
built to protect, because the CLI echoes them. Confirmed live: running the
shipped `backtest` command printed a `?` in place of an em dash in its own
warning line before this fix. Fixed at each of the 25 call sites that are
runtime-visible text (not docstrings or comments, and not `tearsheet.py`, which
writes HTML and is correctly typographic).
**Why:** the CI grep only ever covered the file it was written against. The
actual hazard is any string that reaches `typer.echo` or an uncaught exception's
message, wherever in `algo/` it is written - a boundary the original guard did
not draw.

### D-094 - `ExpiryCalendar.nearest_expiry_on_or_after` now tolerates an unlisted starting month
Found building the bhavcopy bridge: a session in a calendar month with no table
entry (a session predates the file's only listed expiry) raised immediately,
even when the *next* month's contract was in the table and would have satisfied
the query. `BarContext.option_expiries` already tolerates exactly this gap
(`with suppress(CalendarError)` per month, `context.py`); the nearer-expiry
lookup did not, because every prior caller's table happened to cover every month
it queried. Fixed to match: a missing month is skipped, not fatal, within the
same `horizon`.
**Why:** the same gap is real outside a bhavcopy backtest - a live session in a
month whose own contract has already expired and rolled off the master, with
next month's very much listed, is an ordinary calendar position, not an edge
case. Regression test in `tests/test_expiries.py`.

### D-095 - The SmartApi SDK's logger is silenced before it is ever constructed
`SmartConnect.__init__` logs full request bodies - password and TOTP included -
at ERROR level on any failed call, to stderr **and** to a `logs/<date>/app.log`
file it creates itself via `logzero.logfile(...)`, unconditionally, with no
opt-out in its own API. Confirmed by hitting it directly: one failed login
(a local TLS-interception certificate error, unrelated to the credentials
themselves) wrote a real MPIN and a TOTP code to disk in plaintext.
`_silence_smartapi_logger()` (`algo/data/smartapi_feed.py`) neutralises
`logzero`'s default logger - disabled, no handlers, level above CRITICAL - and
replaces `logzero.logfile` with a no-op, both before `SmartConnect(api_key)` is
constructed, so neither sink is ever live.
**Why:** this is a defect in a third-party dependency, not project code, so it
cannot be fixed at the source - only neutralised at every call site that touches
it, which is now exactly one (`SmartConnectTransport.__init__`). The two leaked
log files from this session were never committed (confirmed against
`git log --all`) and have been deleted. Angel One's own TOTP is time-boxed to
thirty seconds, so the specific value that leaked was already unusable by the
time it was found; the MPIN is not, which is the more serious half of this.

### D-096 - The trade log and halt history are now on the dashboard, not just in the API
`lib/api.ts` already had typed client methods for `/trades` and `/kill-switch` -
both real, working backend endpoints - and `page.tsx` never called either. Added
`TradeLog.tsx` and `KillSwitchHistory.tsx` and wired both in: the trade log is
brief §10's "why did this fire six weeks later" question closed out (the
signals panel already answered the entry half), and the halt history makes
"who stopped trading, when, and did the engine ever act on it" answerable from
the page instead of by inference.
**Why:** a client method with no caller and a server endpoint with no consumer
is exactly the kind of half-finished implementation brief §12 rules out, just
distributed across two files where it is easy to miss.

### D-097 - `StateStore` opens its SQLite connection with `check_same_thread=False`
Found live, not in a test: the moment a second dashboard panel started hitting
the API back to back with the first, `/positions` returned 500 -
`sqlite3.ProgrammingError: SQLite objects created in a thread can only be used
in that same thread`. `algo/api/app.py`'s `store()` dependency opens one
`StateStore` per request; FastAPI's threadpool executor is free to run that
request's open and its later close on different worker threads, and sqlite3
refuses by default. Fixed by passing `check_same_thread=False` to
`sqlite3.connect`.
**Why safe:** each `StateStore` instance is still only ever touched by the one
request that created it - never concurrently by two threads at once, which is
the actual hazard `check_same_thread` exists to catch. This relaxes a check
that was stricter than the real access pattern, not the discipline behind it.
`fastapi.testclient.TestClient` could not reproduce this - confirmed by running
30 requests through it against the unpatched code and watching all 30 pass; it
dispatches through its own portal, not the real threadpool. The regression test
(`tests/test_api.py::TestStateStoreCrossesRealThreads`) uses a real
`ThreadPoolExecutor` instead, confirmed to fail against the unpatched code
before trusting it.

### D-098 - Charge rates are now sourced; `verified` still stays false
Replaced the uncited placeholder numbers in `algo/exchange/data/charges_mcx.yaml`
with figures checked against real, dated sources: Kotak Neo's own published
Trade Free plan page for brokerage (Rs 10/order, commodity & currency, flat -
was an uncited "20"), MCX's own 2024-10-01 fee circular for the options
exchange transaction charge (Rs 41.80/lakh premium turnover = 0.0418% - this
one was already numerically right, just uncited), and a broker's
exchange-and-government charges breakdown cross-checking CTT, stamp duty, SEBI
turnover fee and GST (all already correct). The one real numeric correction:
futures exchange transaction charge, 0.0026% to 0.0021%.
**Why `verified` does not flip:** D-011 sets a specific, deliberately strict
bar - rates reproduced from a real Angel One contract note to the paisa. A
broker's public rate card or an exchange circular is real sourcing and a
genuine improvement, but neither is that: a contract note reflects the actual
plan, GST state and any account-level adjustment on the specific account,
which a rate card cannot. The warning text changed to match this precisely -
"CHARGE RATES ARE SOURCED, NOT CONTRACT-NOTE VERIFIED" rather than the old
"ARE PLACEHOLDERS", because they no longer are placeholders in the sense that
word implies (invented), but "sourced" is not "verified" and the message
should not blur the two. Also touched: `algo/costs/margin.py`'s
`SpanApproxMargin` docstring now records a real sanity check for the futures
percentage (a reported GOLDM margin of Rs 60,000-90,000 on an Rs 11.5 lakh
lot brackets the existing 6%) - the short-option percentage has no equivalent
source (SPAN for a short option is scenario-driven, not a stable percentage
any source gives), and is left as an acknowledged guess rather than nudged
without evidence.

### D-099 - The scraped live chain is a first-class source, beside bhavcopy not instead of it
`algo/data/mcx_chain_excel.py` reads the exchange's own option-chain page as
scraped to Excel. It is the only source in the project carrying a **real bid
and ask**: bhavcopy is end-of-day with no book at all (so every tradeability
call against it rests on `assume_spread`'s invention), and SmartAPI cannot
serve a contract that has already expired. What it cannot do is history - it
is one instant, not a series. The two sources answer different questions and
the project keeps both: the scrape for "what is actually quotable now", the
bhavcopy archive for "has this shape ever worked".

The reader maps columns by **position**, not by name, because the sheet mirrors
a rendered ladder - calls left, puts right - so the header text repeats itself
("LTP" appears twice, once per side) and only position disambiguates it. That
makes a silent layout shift the obvious failure mode, so `_verify_header`
refuses a file whose header row does not match exactly and prints wanted-vs-found,
the same contract `bhavcopy.parse_rows` already offers. Blank bid/ask cells stay
`None` and are never coerced to zero: "nobody is quoting this" and "the quote is
zero" are different facts and the tradeability gate depends on the difference.

Greeks are deliberately *not* solved in the loader - it returns unpriced rows and
the caller runs `pricing.chain_greeks.enrich`, so this module never has to pick a
risk-free rate. Correctness evidence is put-call parity: on a real 160000 row the
solved call and put deltas differ by 1.000, which only comes out right if price,
strike and right were all wired together correctly.

### D-100 - The chain panel windows to +/-15 strikes, but never hides a held leg
A real GOLDM ladder lists ~140 strikes at a 500 gap; the ones 30,000 points out
are noise on every question the panel exists to answer, and the strategy sells
inside roughly 0.15 delta which is well within the window. The count of what was
clipped is printed alongside, so a windowed ladder never reads as a short one.
The one exception is a **held** strike that has drifted outside the window -
that is precisely the position worth looking at, so it is always shown.

### D-101 - A book wider than 10% of its own mid is not a book (answers Q17)
`Quote.status()` gained two checks: `TOO_WIDE` when the spread exceeds
`DEFAULT_MAX_SPREAD_PCT` (10) percent of mid, and `NO_OPEN_INTEREST` when open
interest is a *reported* zero. Every previous check asked whether a quote
existed; none asked how wide it was, so a real scraped row at bid 76.5 /
ask 884.5 passed as tradeable, and the IV solver inverted its 480.5 mid into a
delta of +0.150 - landing on the strategy's own selling target ahead of both
real neighbours (which sat at 0.063 and 0.045, on volumes of 27k and 32k against
its zero). Live, that trade collects 76.5 while the backtest records ~480.

**The threshold is measured, not chosen by feel.** On a real GOLDM scrape the
genuine near-the-money book runs 0.3-1.5% of mid and the fabricated rows run
50%+ (worst observed: bid 1 / ask 1833), so the 5-15% band separates them
cleanly. Signed off by the operator. Applying it moved the 0.15-delta call from
167500 at zero volume to 164000 at 47,843.

Three details that are deliberate:

* **Relative to mid, not absolute.** Five points is tight on a 2000-rupee option
  and nonsense on a 20-rupee one; an absolute bound would need re-tuning per
  strike.
* **Reject, never widen.** An untradeable row is the truthful outcome. Widening
  the quote to fit would be exactly the substitution D-005 forbids.
* **Open interest of `None` is not zero.** The feed not reporting it is not
  evidence that nobody holds the contract - and treating absence as zero would
  have made every synthetic fixture untradeable.

The default is applied in `status()` itself rather than left opt-in like
`stale_after_s`, because staleness needs a policy number the model cannot know,
whereas a book wider than its own mid is not a book under any policy. Callers
wanting the raw classification pass `max_spread_pct=None`.

The whole suite passed both before and after the gate went in, which meant
nothing covered it; `tests/test_quote_tradeability.py` was added and confirmed
to fail (4 tests) against the ungated code before being trusted. The reason a
row was rejected now travels to the dashboard as `flag`, so the chain panel can
say *why* a strike is dimmed instead of blurring "nobody quoted it" together
with "quoted, but fictitiously".

### D-102 - The stop loss is disabled, and the target moved to 4% (operator's call)
`no_stop_loss: true` and `take_profit_value: "4"` in `config/goldm.yaml`. Requested
directly by the operator. What it means mechanically, stated here because the
code can no longer state it at the exit level:

* A short strangle's **call side has no bounded loss**. The put side is bounded
  only by gold going to zero.
* The kill switch does **not** rescue this. It halts *new entries*; with
  `flatten_on_trip: false` it leaves an open position alone. So a run can trip
  the 2% daily limit and still be carrying the losing strangle.
* The remaining exits are take profit, the devolvement forced pre-expiry exit,
  and the end of the run. The devolvement guard is therefore now the **only**
  hard backstop on an open position, which raises its importance considerably.

Three things keep the change from being silent:

* `ExitLevels.stop_loss` is `None`, never `Decimal("0")` - a zero would exit the
  moment the position was down a rupee, the opposite of what was asked for.
* Disabling it takes the explicit `no_stop_loss` flag. Passing `stop_loss=None`,
  or omitting the setting, still gets the 1% default. A safety level removable by
  omission is removable by accident.
* The engine raises a standing run warning, alongside the uncalibrated-model
  ones, and `algo config` prints `NO STOP LOSS`. `stop_loss_value: "1"` is
  deliberately left in the file so flipping the flag back restores the old
  behaviour exactly.

The D-024 stop-viability check is skipped rather than reworked - there is no stop
to compare against the round-trip cost.

### D-103 - Only strikes that are multiples of 1000 are considered
`strike_multiple: "1000"`. GOLDM lists every 500, but the round thousands carry
the book. Measured on a real scraped chain rather than assumed:

| | multiples of 1000 | the 500s between |
|---|---|---|
| tradeable rows | 78 of 142 (55%) | 55 of 140 (39%) |
| median volume | 4,434 | 1,167 |
| median volume, 0.15-0.30 delta band | 165,340 | 49,853 |

Applying it moved the 0.15-delta call from 167500 at **zero volume** to 164000 at
**47,843**.

It is a **filter, not a rounding**. A strike is either on the grid or is not
considered; nothing snaps 160500 to 160000. Snapping would report a delta the
position does not have - the same substitution D-005 forbids - and if no strike
on the grid falls within the delta tolerance, the strategy emits nothing and says
why, exactly as it already did for unquoted strikes.

### D-104 - Enter on the front cycle's expiry day, selling the next cycle
`roll_at_front_dte: 0` with `cycle_offset: 1`. The monthly roll: on the day the
current month's options expire, sell the next listed cycle (~28-30 DTE).

Two implementation choices worth recording:

**The gate is a DTE threshold, not a date equality.** `<= 0` on the front cycle
is its expiry day; but if that date is a holiday there is no session on it, and an
equality test would skip the roll for that month entirely. The threshold form
lets the last session before it satisfy the gate. It can therefore be true on
several consecutive sessions - the cadence check, keyed on the cycle actually
sold, is what limits it to one entry, not the gate.

**The next cycle is resolved by date, not by adding a month.** Gold does not list
every calendar month, so "the next contract" and "the same day next month" are
different facts and only the first can be traded. `BarContext.expiry_after` asks
the calendar for the first cycle expiring after the front one. When nothing later
is listed - a real state near the end of the master's horizon - the strategy
records a note and does not trade, rather than raising.

The devolvement entry block does not interfere: `_pre_trade_block` resolves the
cycle from the **signal's own legs**, so it judges the ~30-DTE cycle being sold,
not the front one expiring that day.

### D-105 - The bhavcopy column mapping is verified, for one layout, and the file is HTML
Real MCX "commodity wise" exports arrived on 2026-08-27 (GOLDM, Jan-Jul 2026,
82,020 option rows over 140 sessions). Three findings:

**The blind mapping was almost right.** `MCX_DEFAULT_COLUMNS` guessed 12 headers
and got 10 exactly right. Only two were wrong, both carrying a unit suffix in the
real file: `Volume` is `Volume(Lots)`, `Open Interest` is `Open Interest(Lots)`.
`MCX_COMMODITY_WISE_COLUMNS` records the checked layout; the blind one stays as a
fallback and stays labelled unverified, because the plain CSV bhavcopy has still
never been seen.

**The file is HTML with an `.xls` extension.** Not Excel, not CSV - a bare
`<table>`. A CSV reader yields one meaningless column and raises nothing. So
`parse_rows` now sniffs content rather than trusting the name, and
`load_directory` globs both extensions: keying the default off `.csv` alone found
zero files in a directory entirely full of usable data, which is precisely the
silent-nothing failure this codebase is meant not to have.

**Untraded rows carry only a settlement close.** About 80% of the ladder has an
empty open/high/low and a close. That is not missing data - it is "the only price
this contract had all day was its settlement" - so the empty fields fall back to
the close. `volume` stays 0, so `traded` and `assume_spread` still treat the row
as untradeable and nothing becomes falsely fillable.

### D-106 - The supplied bhavcopy cannot back-test this strategy, for two reasons
Recorded because both are properties of the *data*, not of the engine, and both
have to be fixed by downloading differently rather than by changing code.

**No futures rows.** All 82,020 rows are `OPTFUT`. `build_snapshots` needs a
futures close for the forward, and without a forward every delta in the chain
would be invented - so it skips the session. The run reports
`every day with option rows was missing a matching futures row (140 skipped)`
and produces nothing, which is the correct outcome: `DeltaStrangle` selects
strikes *by delta*, so an invented forward would silently choose different
strikes and the entire result would be fiction.

Put-call parity was checked as a fallback and is viable but not adopted by
default: on 15 Jul 2026, 35 strikes had both legs traded and the implied forward
clustered inside ~0.3% (141,461-142,332). That is a derivation with real error
bars, and delta is sensitive to it. The exchange's own futures close is one more
download away and is not worth substituting.

**Only the front expiry is present.** No session in any of the seven files lists
more than one expiry. D-104's roll needs the *next* cycle quoted on the front
cycle's expiry day, and those rows do not exist in this export.

This is a limitation of the download, not of the instrument: the Angel One master
lists three GOLDM option expiries concurrently (2026-08-28, 2026-09-25,
2026-10-29). The roll is therefore tradeable live and merely unbacktestable
against this particular file set.

### D-107 - MCX trades some weekends, and the calendar now knows it
The first real backtest crashed on `2026-02-01 is not an MCX trading day`. The
calendar was wrong, not the data: **Sunday 1 February 2026 carries 285,223 lots
of GOLDM option volume across 149 traded strikes**. India's Union Budget is
presented on 1 February and the exchanges hold a live session for it whatever the
weekday - as in 2020 and 2025, both Saturdays.

`MarketCalendar` gained `special_sessions`, a set of dates that override the
weekend rule. It is a list of observed facts evidenced by traded volume, not a
rule to be extrapolated; a holiday still beats a special session, since a date in
both is a session scheduled and then cancelled. Exactly one of the 149 session
dates in the supplied data needed it.

### D-108 - The bhavcopy backtest cannot measure this strategy: the exit lag exceeds the target
Six real cycles ran (Jan-Jun 2026, 238 bars, 6 round trips, +22,957 net). **The
P&L is not a usable estimate of edge**, and the run's own output shows why: all
six trades exited `TAKE_PROFIT`, including two that lost money. A take-profit
exit with a negative P&L is a contradiction, and chasing it found the reason.

Exit orders join `pending` and fill at the **start of the next bar**
(`engine.py`). The bhavcopy runner builds **two bars per session** - a 09:30 entry
priced from the day's open and a close bar - so an exit decided at the close fills
at the next session's open. Measured on the 128 real futures sessions:

| | |
|---|---|
| take-profit target (4% of ~113,335 margin) | **₹4,533** |
| median close→next-open gap, 0.42% of 141,669 | **₹5,950 per lot** |

The lag through which every exit fills is **larger than the target it is trying
to capture**. Median intraday range is 1.72% and reaches 13.64%, so the noise
dominates the signal completely. This is not a bug to fix in the engine - the
fill is honest given two ticks a day - it is bhavcopy being the wrong instrument
for measuring a 4%-of-margin target.

Three things were checked first and cleared, so the conclusion rests on the lag
and not on a data fault:

* **The forward is right.** Put-call parity across 59 paired strikes on
  2026-02-02 implies a median forward of 141,865 against a bhavcopy futures close
  of 141,669 - 0.14% apart.
* **The prices are internally consistent.** Parity holds across the ladder.
* **The high implied vols are in the data, not the model.** The 2026-02-02
  futures row ranges 131,607-147,800, an 11.4% intraday move, which is what a
  ~60% IV is pricing.

What this run *can* support is the shape question - the strategy traded six real
cycles, 4 winners and 2 losers, without the engine refusing or the devolvement
guard firing. What it cannot support is any statement about expected return.
Answering that needs intraday data, which only the recorder can supply.

### D-109 - The live loop shares the backtest's decision path, not a copy of it
`algo live` connected, reconciled and stopped. The missing piece was never the
broker - `Router` (at-most-once, reconcile-before-send), `KotakBroker`,
`PaperBroker`, `OrderJournal` and `Reconciler` all existed and were tested - it
was the loop between a bar arriving and an order being sent.

**The seam is `BacktestEngine.decide`.** The per-bar body of `run` split cleanly
in two: *settle* (turn queued orders into fills) and *decide* (kill switch, risk
exits, mark, ask the strategy, size). Only settling is genuinely different
between a backtest and a live session - a backtest settles against the next bar's
prices, a live loop learns its fills from the broker asynchronously. Deciding is
identical, so it is now one method that both call, and `LiveLoop` cannot choose
what to trade, how much, or when to exit.

This follows the principle `paper.py` already stated for fills - "not a similar
one, the same `FillSimulator` object" - and it matters more for sizing and exits
than for fills.

The extraction was proven behaviour-preserving rather than assumed: the golden
trade-log digest is unchanged, and the six-cycle bhavcopy run still returns
22,957.40 to the paisa.

**Two seams were added for live, both narrow.** `append_bar` (a live session
learns its bars one at a time) and `apply_fill` (book a fill the engine did not
invent). `_execute` now calls `apply_fill` for its own fills, so simulated and
real fills reach the portfolio and the strategy by one path.

**A real bug fell out of it.** `BarPriceSource` indexed its bars into a dict at
construction, so a bar appended later was invisible to it and the first mark
against that bar raised. A backtest never sees this because it knows every bar up
front. Fixed with `BarPriceSource.add`, called from `append_bar`; sources that
carry their own data (a chain feed) have no `add` and need nothing.

**The four safety properties `LiveLoop` is tested for**, each because it costs
real money when wrong: it settles before it decides (asked "given what you
actually hold", never "given what you asked for last time" - `is_flat` is
`DeltaStrangle`'s first gate); it acts on each closed bar exactly once, keyed on
the bar timestamp, because a duplicated entry signal is a doubled position; it
treats a refused order as a result rather than an exception, since
`BLOCKED_UNRECONCILED` is the reconcile-before-send rule working; and it stops -
`max_passes` is required and has no default, because a trading loop's stopping
condition is not a detail to leave to a caller who forgot.

Still to come before this can place a real order: wiring `LiveLoop` into the
`algo live` command against the paper broker, persisting `DeltaStrangle`'s
`_traded_cycles` across a restart (flagged in its own docstring since M4), and a
chain feed for the options path.

### D-110 - The traded-cycle cadence is persisted, guarded by the parameter hash
`DeltaStrangle`'s docstring has said since M4 that the traded-cycle set "is
genuine strategy state and must be persisted for a live restart to behave
correctly". It now is, through `Strategy.state()` / `Strategy.restore()` and a
`strategy_state` table.

The hazard is narrow and expensive: **a flat account looks identical whether this
cycle was traded and closed or never entered at all.** A restarted process with
an empty cadence set sells a second strangle into a cycle it has already traded,
which defeats the whole "one per cycle" rule.

Four decisions inside it:

* **Almost nothing belongs in `state()`.** Position state comes from the context
  every bar (D-041) precisely so it cannot drift, and anything derivable must
  keep being derived. Only what is genuinely the strategy's own and not
  reconstructible from the book goes here.
* **The parameter hash is a guard, not a label.** `strategy_state` returns None
  for state saved under a different `params_hash`, and the caller cannot
  distinguish that from "nothing saved" - both mean "you have no usable prior
  state". Signal ids already depend on the parameter set for the same reason.
* **A garbled payload raises rather than starting empty**, because starting empty
  is exactly the state that permits the duplicate entry.
* **Saved on every fill, not at end of run.** A live process that dies has no end
  of run, and a fill is the only thing that changes this state.

Restoring is opt-in (`restore_strategy_state`), never automatic: a backtest
starts from nothing by definition, and silently inheriting a previous run's
cadence would make results depend on whatever was in the state file.

### D-111 - `algo live --passes` runs the paper loop, and refuses anything else
The loop is wired end to end: `SmartApiBarFeed` -> `IterableBarFeed` ->
`LiveLoop` -> `BacktestEngine.decide` -> `OrderRouter.place_all` -> `PaperBroker`,
with `BrokerFillFeed` closing the circuit back into the portfolio.

**Paper only, and the refusal happens before any credential is read or any
session opened.** The first version had that guard inside `_run_paper_loop`,
which meant refusing *after* connecting to a real broker - the right answer in
the wrong place. It now sits at the top of `live()`, behind the pre-existing
`resolve_mode` gate rather than replacing it, so a live config is stopped twice.
Verified: `TRADING_MODE=live` plus the real-money flag exits 1 with a message and
touches nothing.

Two details in the feeds worth keeping:

* **`BrokerFillFeed` records zero charges, deliberately.** An execution report
  carries a price and a quantity, not a contract note. Writing a modelled charge
  into the field a real one belongs in would make the two indistinguishable
  later; `Fill.is_modelled` is False for these, and the charges stay empty until
  something authoritative fills them (Q6).
* **A fill for an unknown instrument raises.** Booking it against a guess
  corrupts the portfolio; skipping it leaves a real position invisible. Neither
  is acceptable, so it stops.

`PaperBroker` is quoted from `BacktestEngine.mark_for`, so paper fills and
backtest fills are marked from the same data and a disagreement between them can
never be two price lookups that diverged.

Not yet done: the options path needs a live chain feed (`KotakChainFeed` exists
and is not yet connected to the loop), so `--passes` currently drives the futures
instrument only.

### D-112 - The live chain is enriched and staleness-bounded, one poll per bar
`KotakChainFeed` returns every row with `iv=None, delta=None` - a market-data
poll carries prices, not greeks. `DeltaStrangle` selects strikes **by delta**, so
handing it a raw live chain would match no strike and the strategy would
**silently never trade**. `LiveChainProvider` is the piece between them.

**It answers both questions from one poll.** The engine asks a chain provider
"what does the ladder look like" and a price source "what is this leg worth". In
a backtest those are two objects built from the same snapshot list; live they
must come from the same poll or a strike can be chosen at one instant and marked
at another. So one object implements both protocols, and `refresh` is driven by
the loop **once per bar** rather than per question - a single `decide` asks
several times over (the strategy, the dashboard snapshot, then each mark).

**Staleness is refused, not tolerated.** `ChainPriceSource` keys marks by exact
timestamp, which cannot work when snapshots arrive whenever a poll returns. This
answers from the most recent snapshot and returns None once it is older than
`max_staleness_s` (default 120s), which makes `require_mark` raise and the loop
stop. That is deliberate: `prices.py` already argues a missing mark priced at
zero "would show a short option as fully profitable on exactly the bar the feed
dropped out - the most dangerous possible direction for that error". A stale
price is the same error with a smaller number on it. The ladder goes stale too,
not just the marks, or the strategy would select strikes from prices nobody is
showing any more.

**It will not answer for the wrong cycle.** A `chain_at` for an expiry other than
the one polled returns None rather than the polled ladder - "no chain for that",
never "here is one".

Two supporting changes: `KotakChainFeed.poll` is now public (`snapshots` owns its
own cadence and sleeps, which suits a recorder; a trading loop already has a
cadence - its bars - and must not be handed a second one), and
`_expiries_from_master` builds an `ExpiryCalendar` from what the broker actually
lists, pairing each option cycle with the first futures contract expiring on or
after it - the identical pairing `nearest_futures_expiry` applies to bhavcopy, so
live and backtest cannot disagree about a cycle's underlying.

A chain failure ends the pass with a stated reason rather than a quiet no-trade,
because "the feed broke" and "the strategy declined" must not look the same.

Proven end to end: `LiveLoop` -> `decide` -> `DeltaStrangle` now emits a real
two-legged short strangle (one CE, one PE, both SELL) through the production path
with only the market-data transport stubbed.

### D-113 - Three real bugs, found the first time the adapter met the live Kotak API
The paper loop was pointed at the real broker on 2026-08-27. Every one of these
had been passing its tests, because the fakes modelled envelopes Kotak does not
actually send.

**1. `_ack_ok` could not read the login envelope, so `connect()` never worked.**
It checked for a flat `stat`/`stCode`. The login endpoints send neither: they
nest everything under `data` with `status: "success"`. A perfectly good login was
read as a rejection, which means the Kotak adapter had **never** established a
session against the real API - only against fixtures using the flat shape. Both
shapes are now recognised; the nested branch is deliberately narrow
(`data.status` must be exactly "success"), because this function is what stands
between a broken session and the router believing it may trade. The fakes were
corrected to the real shapes rather than the check being loosened - the fakes
being wrong is *why* this survived.

**2. TLS trust was injected by SmartAPI only, so Kotak worked by accident.**
`_trust_the_os_certificate_store` lived in `smartapi_feed`, and `algo live`
happened to construct that transport first, patching SSL process-wide before the
Kotak SDK opened a socket. Reordering two lines would have broken the broker
connection for a reason nobody would look for in that file. Moved to
`algo/core/tls.py`; both transports now call it in their own constructors.
Confirmed by a standalone Kotak login failing with `CERTIFICATE_VERIFY_FAILED`
until it was added.

**3. An empty book was treated as a fatal error.** Kotak reports "nothing to
report" as an *error* envelope - `stat: Not_Ok`, `stCode: 5203`,
`errMsg: "No Data"` - for the trade report, order report and positions alike.
The first live run died on `Kotak trade report call failed: No Data`, which only
meant the account had not traded that day: the state **every** session starts in,
and precisely what reconciliation needs an answer to rather than an exception.
`_is_empty_book` now recognises it, and is deliberately narrow - the code and the
message must both match and there must be no `data` - because widening it to any
`Not_Ok` would turn a failed positions call into a silent empty list, i.e.
believing the account is flat when it is not.

**One safety check added while in there.** The first login stage returns
`kType: "View"`; only MPIN validation upgrades it to `"Trade"`. `connect()` now
refuses a session that is not trade-scoped, rather than discovering it when the
first order is rejected - which would be mid-strategy with a leg possibly already
open. Verified against the live account: stage one returns View, stage two
returns Trade.

**What the live run proved**, at 02:33 IST with MCX closed: both sessions
connect, both masters load (3 listed GOLDM option expiries), reconciliation runs
clean and reports `safe to trade: True`, and the loop degrades correctly with
"no closed bars yet today; nothing to decide on".

**What it could not prove**, because the market was shut: the chain poll against
live quotes, strike selection from live deltas, routing to the paper broker, and
fill settlement. Those need a session between 09:00 and 23:30 IST.

**Still open:** `limits` (which backs `funds()`) returns `stCode: 300015`,
`"bridge API error out"`. Not diagnosed - it may be a closed-market artefact or a
separate entitlement. It did not block reconciliation, and is recorded rather
than guessed at.

### D-114 - A Kotak backend outage is retryable, not fatal
`limits` returns `stCode: 300015`, `"bridge API error out"` (Q19). The adapter
would have called that `FatalBrokerError`, because anything failing `_ack_ok`
was fatal by default.

That is the wrong side to err on. This module's stated rule is "transient network
failures are retryable, everything a broker **rejects** is fatal", and a bridge
outage is not a rejection on the merits - it is a component being unavailable,
which is far closer to a network failure. Fatal would kill a live session over an
outage that may clear on its own; retryable costs a backoff before the router
surfaces it anyway. The asymmetry decides it.

The check is narrow - the status code only - and applies at both places a
response is validated (`_ok_data` for book reads, `funds` for the limits call).
Note the live payload sends `stCode` as a **string** where the empty-book
envelope sends it as a number, so the parse handles both; a test pins that,
because it is the kind of difference that silently disables a guard.

The classification follows from what the error *is*, not from confirmed
transience - Q19 records the re-test needed during market hours to distinguish a
closed-market artefact from an entitlement problem.

### D-115 - Every config field must decide something, or refuse
A scan found **21 of 79 config fields that nothing ever read**. That is worse
than a missing setting: the file described a system that did not exist, and two
of the dead fields read as safety policy.

* **`flatten_on_trip: false`** - never consulted. Only a *dashboard* halt request
  carrying `flatten` ever closed a position; the switch tripping on its own
  limits left an open strangle running whatever the file said. Now wired: a
  self-trip flattens when configured to.
* **`allow_unverified_calendar: false`** - never consulted, and every command
  built `synthetic_calendar()`, which says of itself "never for a real run" and
  passes `allow_unverified=True`. **MCX holidays were not modelled anywhere**,
  including in `DevolvementGuard`, whose exit deadline is computed by walking
  back trading days - so a deadline could land on a day the market was shut.
  Running without a stop loss, that guard is the only hard backstop there is.
  Now `mcx_calendar` builds from config and refuses to start unverified.

  The config had to become honest about it: there is no sourced MCX holiday list,
  so `allow_unverified_calendar` is now **true** with `holidays_file: null` and a
  comment saying why (Q20). A gate that reads "false" while nothing checks is
  worth less than one that reads "true" and is enforced.

**The treatment, by category.** *Wired*: `max_stale_seconds` (now 120s and read
by `LiveChainProvider`, which had hardcoded its own 120 while config said 10),
`partial_last_bar`, `flatten_on_trip`, `allow_unverified_calendar`,
`holidays_file`, and the whole `logging` section (nothing configured structlog at
all - it emitted JSON by default, so `json_format: true` described what was
happening without causing it). *Refused*: `timezone`, `dst_reference_zone`,
`act_on_partial_bar`, `reject_crossed_quotes`, `reject_empty_book` and
`evaluate_on: tick` now raise at load, because their behaviour is fixed in code
and accepting a value that does nothing is the bug. *Deleted*: `out_dir`,
`bars_path`, `chain_path`, `research_db`, `wal`, `on_violation`.

`tests/test_config_has_no_inert_fields.py` keeps the scan itself as a test, so
the class cannot come back.

### D-116 - The rest of the audit: dead code, untested logic, a stale path
**Dead symbols removed**: `can_place_real_orders` (a safety helper nothing
called), `write_equity_curve`, `OrderUpdate`, `RiskRejection`. `ExpiryProvider`
was an *incomplete* protocol - it declared `option_expiry` but not `expiry_set`,
and `ExpiryCalendar` annotated the concrete class instead - so it was completed
and used rather than deleted.

**`check_exit` stays and now says why.** It holds the intrabar stop/target logic
including gap handling, is called by nothing, and read as an oversight. It is
staged work for Q15; the docstring now says so, and `evaluate_on: tick` refuses
at load, so no run can believe it has intrabar protection it does not have.

**Untested modules.** Four were never imported by any test. Two carried real
logic: `bhavcopy_runner` (305 lines - it produced the only real-data result the
project has) and `validate` (158 lines - the data quality gate). Both now have
tests. Writing the first set corrected my own assumption: the runner emits **two**
chain snapshots per session, priced from the open and the close respectively,
which is right and better than the one I expected.

**`_expiries_from_master` moved out of the CLI.** It decides which futures
contract every option cycle settles into, and it sat in `algo/cli/main.py` - the
one module (1782 lines) that no test imports. Now `expiries_from_master` in
`algo/exchange/expiries.py`, with tests.

That untested CLI bit immediately: wiring `flatten_on_trip` introduced an
`UnboundLocalError` on the no-config path that the whole suite passed straight
over. Found by running the command, not by the tests.

**D-090 cited `algo/strategy/paper.py`**, which has never existed; the paper
broker is `algo/execution/paper.py`. Path corrected.

### D-117 - The CLI is tested, and the tests were checked against the real bug
`algo/cli/main.py` is the largest module in the project and no test imported it.
That is how wiring `flatten_on_trip` (D-115) shipped an `UnboundLocalError` on
the no-config path which a 790-test suite passed straight over; it was found by
running the command, not by the suite.

**The test was verified against the bug it exists for.** Reintroducing the
missing binding fails three of the new tests while the other 829 pass clean -
which is the proof that the gap was real rather than a matter of taste.

Two rules shape the file:

* **Every command that takes `--config` is invoked twice**, with and without one.
  The bug was a name bound inside `if config is not None` and read outside it,
  and that shape recurs in every such command.
* **Nothing touches the network.** `live`, `credentials` and `serve` open real
  sessions, so only the paths that return *before* any credential is read are
  exercised - which happen to be the safety gates, the part most worth pinning.
  A test suite that logs into a broker is one nobody can run.

Also covered: the falsification line itself (a silent change there would be the
most serious regression the project could have), the `config` command surfacing
`NO STOP LOSS` and the roll, the kill switch round-tripping a halt request
through a real state file, and `backtest-bhavcopy` end to end on a two-session
CSV fixture - enough to exercise loader, calendar-from-config,
strategy-from-config, engine and report without adding a large file to the repo.

Writing it surfaced a safety feature worth pinning that I had forgotten: `--trip
--flatten` **asks for confirmation**, because flattening market-closes a short
strangle that may be mid-move. Declining now has a test asserting that nothing is
recorded - a half-applied halt being worse than none.

The ASCII guard moved from CI-only into the suite as well, scoped to our source
rather than rendered output: Typer draws its help panels with box characters and
those are its business.

Untested modules are down from four to none.

### D-118 - Config resolution happens once, not once per command
Three commands each carried "read these fifteen settings out of config, or use
these defaults" - about a hundred and twenty lines of near-duplicate where every
new risk setting had to be added in **six** places.

Not a style complaint: adding `flatten_on_trip` (D-115) meant the same edit three
times, the three `else` branches were missed, and one shipped as an
`UnboundLocalError` a 790-test suite did not see (D-117). A shape that produces
that bug once produces it again.

`RunSettings` in `algo/config/runsettings.py` resolves both paths through one
object, with `defaults()` **built from the schema** rather than written out a
second time - so "the default" has exactly one definition and cannot drift from
the reference config. It carries kill-switch *parameters* rather than a built
`KillSwitch`, keeping config free of any dependency on the risk layer.

**`backtest` was deliberately left alone.** Its no-config branch really is
different - no margin model, no kill switch, no margin cap - because the
Milestone 3 falsification is meant to run bare: a kill switch could halt it and a
margin cap could refuse its trades, and either would break the arithmetic check
it exists to perform. That divergence is intentional, and is now stated rather
than looking like the drift the other two had.

The refactor was proven behaviour-preserving the way the others were: the golden
digest is unchanged and the six-cycle bhavcopy run still returns 22,957.40.

### D-119 - The README described a system that no longer existed
Its second paragraph read "**Nothing has been connected to a real broker, and no
real market data has been recorded.** Every number the system can currently
produce comes from generated data." Both halves were false: the Kotak and
SmartAPI sessions connect and reconcile, and a backtest has run over 82,020 real
MCX bhavcopy rows.

The "What exists" section was worse - it still said the live trading loop "does
not yet construct a strategy or place an order", which had been the single most
important capability claim in the file and had been wrong since D-109.

Corrected, and with the caveats attached rather than left for the reader to find:
the paper loop has never traded a live session (only outside market hours), and
the bhavcopy P&L is not an estimate of edge (D-108). Also fixed: the calendar
paragraph claimed a verified-holiday refusal that `allow_unverified_calendar:
true` currently switches off (Q20), the bhavcopy column mapping is no longer an
"unverified assumption" for the layout that was checked (D-105), the coverage
example now shows the real archive instead of a three-session sample, `algo/live/`
was missing from the tree, and the test count was 469 against an actual 880.

The same rule the config now follows applies here: a document describing a system
that is not the one running is worse than no document, because it is trusted.

### D-120 - The variant configs are pinned against drift
`config/goldm_bhavcopy_frontcycle.yaml` and `_allstrikes.yaml` were made by
copying `goldm.yaml`, so every later edit to the reference has to be repeated by
hand. It had already gone wrong: `max_stale_seconds` was raised to 120 in the
reference (D-115) and left at 10 in both variants.

`tests/test_config_variants_do_not_drift.py` flattens all three and asserts each
variant differs from the reference **only** on the settings it declares, naming
any other divergence. A second test asserts the declared differences are actually
different, so a stale allowance cannot quietly widen the exemption. Verified
against the real drift before being trusted.

### D-121 - The MT5 venue layer: XAUUSD on Vantage
`docs/milestone-0-plan.md` has a section headed "S6 is an MT5 forex/CFD model;
this is MCX commodity options", which reconciled the original brief *away* from
MT5 item by item. This adds MT5 back as a second venue - not by reversing that
reconciliation, but because a CFD really is the other thing, and the differences
it listed are exactly the ones that had to be built.

**MT5 cannot trade this strategy.** Its API has no option-chain concept at all -
no strike, no right, no expiry ladder - and no broker routes MCX through it.
`DeltaStrangle` does not port; a `CfdId` has no expiry, so there is no roll, no
cadence, and nothing for `DevolvementGuard` to guard. What ports is everything
else: the engine's `decide` loop, risk layer, portfolio, backtest, live loop and
dashboard.

Everything below was **measured from the live terminal**, account 25804244
(VantageMarkets-Demo), 2026-08-28, not taken from documentation.

**Two correctness traps in the data feed.** MT5 stamps every bar and tick in
*broker server time* and the API never says what that is - Vantage runs UTC+3 in
summer, and EET brokers shift twice a year, so a hard-coded offset is wrong for a
third of the year. `measure_server_offset` measures it against a real clock,
rounds away network latency and refuses an implausible result. Separately,
`copy_rates_from_pos(..., 0, n)` includes the bar **still forming**, whose high,
low and close can all still change; handing that to a strategy is look-ahead by
the back door, and it is the one form the backtest firewall cannot catch because
in live there is no future array to withhold. `closed_bars` drops it.

**The unit decision, because getting it wrong is a 100x position error.** MT5
sizes XAUUSD in 100-ounce broker lots in steps of 0.01; `Order.lots` is an
integer validated `>= 1`. Both cannot hold, so **one engine lot = one troy ounce
= 0.01 MT5 lots**. That keeps the order model, sizer, portfolio and fill
simulator untouched and preserves MT5's own granularity. The conversion belongs
in exactly one place - the broker adapter - and anything printing a size to a
person must name the unit.

**Financing is back, and it is the dominant cost.** Milestone 0 struck "swap /
financing, triple-swap Wednesday" off with "Does not exist" - true for MCX, where
margin is blocked rather than borrowed. Measured here: long **-80.54 points/lot/
night = -6.59% a year**, short **+32.67 = +2.67% a year**, tripled on Wednesday.
A long held a year pays 6.6% of notional before making anything; for any
multi-day hold this is larger than spread. The sign convention is stated and
tested because reversing it turns the largest cost in the model into income.

The swap rate is a **snapshot and can never be otherwise**: MT5 publishes no
historical series, so a backtest over four years applies today's rate to all of
it. `SwapModel.is_verified` is permanently False - unlike a contract note, no
evidence could ever settle a rate that was never published.

**Commission, by contrast, is verified.** Not from marketing: all 54 XAU deals in
the account's own dealing history carry `commission == 0.0` and `fee == 0.0`.
`CfdChargeModel.vantage_standard()` is verified on that evidence, scoped to this
account tier. The MCX taxes (CTT, SEBI, stamp, GST) are zero here because the
venue has none, not because they are unmodelled.

**The trading week, derived from 5,000 real bars** rather than from what an FX
week is generally like. Sunday 22:00 to Friday 21:00 UTC, no Saturday, with a
90-minute daily break whose empty slots are exactly 21:00 and 21:30. Naming a
session by the date it *closes* on makes the rest fall out: Sunday evening's bars
belong to Monday's session, and the daily break stops being an intra-session
special case - it is precisely the gap *between* sessions. 21:00 UTC is midnight
on the broker's clock, which is also when financing is charged, so a session
boundary and a swap charge are the same instant. Validated: **0 of 5,000 real
bars fall outside the modelled session.**

`Exchange.OTC` names the venue for what it is. A CFD is a bilateral contract with
the broker - no central order book, the broker is the counterparty, and none of
the guarantees that make an MCX fill comparable across venues apply.

**Still to build: the broker adapter.** Two findings shape it. The account is
**HEDGING**, so MT5 holds independent tickets per trade rather than one netted
position per symbol, which the engine's `Portfolio` assumes. And MT5 **overwrites
the order comment** with its own text (`[sl 4641.92]`, `closePosition`), so the
comment cannot carry a client order id through a lifecycle - the adapter needs
its own persisted ledger, exactly as `KotakBroker` does for the same reason.
Filling mode is IOC-only on this symbol.

### D-122 - The MT5 broker adapter
Implements `Broker` against a running terminal, so `OrderRouter`, `Reconciler`,
`OrderJournal` and `LiveLoop` all work unchanged. Four things about MT5 shaped
it, each found by probing the live account rather than reading documentation.

**The comment field is not a tag.** MT5 lets an order carry a `comment`, which
looks like the client-order-id slot the Kotak SDK lacks. It is not: the terminal
**overwrites it**. Deals pulled from the same account carry comments MT5 wrote
itself - `[sl 4641.92]`, `[tp 4635.00]`, `closePosition` - in place of whatever
was sent, so an id put there survives until the first stop-out and is then gone.
Same answer as Kotak, for a different reason: the adapter keeps its own persisted
ledger, and `magic` (a fixed constant) is what separates our orders from the
account's existing ones.

**The account is HEDGING; the engine nets.** `positions_get` returns an
independent ticket per trade, while `Portfolio` and `BrokerPositionSnapshot`
assume one signed net position per instrument. Tickets are aggregated with a
signed-volume-weighted average entry. That is the right arithmetic, but netting
hides something real - two opposing tickets cancel on paper while both still pay
financing - so `opposing_tickets` reports the pair rather than letting the
netting swallow it.

**Sizes are ounces here and lots there.** `volume = lots / 100`, in one place
only, tested in both directions and at the venue's own limits. A volume finer
than MT5's 0.01 step raises rather than rounding, because a silently rounded size
is a silently wrong position. This is the hundredfold error the spec file already
warns about (D-121).

**Filling is read, not assumed.** `symbol_info("XAUUSD").filling_mode` reports
IOC only on this account; sending FOK would be rejected. A symbol advertising
neither raises rather than guessing how the venue wants to be filled.

Two smaller decisions worth stating. `cancel` **raises** rather than no-opping:
every order this adapter sends is a market IOC that fills or is rejected
immediately and never rests, so a caller believing it had cancelled something
would be reasoning about a position that is open. And `connect` refuses when the
terminal reports `trade_allowed == False` - better than discovering Algo Trading
is switched off when the first order bounces.

**A security property worth noting.** Unlike the Kotak adapter there is no login
step here: the terminal is already signed in, so **no credential passes through
this process at all** - no TOTP seed, no MPIN, nothing to leak into a log.

Verified against the live account, read-only: funds and health correct, flat on
XAUUSD, and **0 of the account's 72 existing deals adopted** - the `FixedVol100`
position and every prior hand-placed trade are correctly foreign. No order has
been placed through it.

### D-123 - `MacdCrossover`, and the honest first measurement against real costs
Mirrors `tools/macd_telegram_alert` exactly: `EMA(adjust=False)`, MACD(12,26,9)
by default, the same `<=`/`>=` crossover rule (`algo/pricing/indicators.py`,
cross-checked line-for-line against pandas with `adjust=False` - 0.0 max
difference over an 800-bar random walk). A signal here and an alert there answer
the same question the same way.

**Indicator state is incremental, not recomputed per bar.** The alert tool's
live poller refetches a rolling 300-candle window and reseeds the EMA from
scratch on every poll - a deliberate, bounded-cost approximation for a
rate-limited exchange API. That approximation would cost this engine its
live/backtest parity guarantee (two polls of the same instant could show
different histogram values depending on window alignment), so `MacdCrossover`
instead carries the three EMAs as running state, fed one close per `on_bar`.
Persisted across a restart, for the same reason `DeltaStrangle`'s cadence is
(D-110): reseeding from zero on every restart would silently spend
`warmup_bars()` bars re-converging with no signal, at exactly the moment - a
restart during an open position - that blind spot is least acceptable. Verified
by feeding one continuous run and one restarted-midway run the same bars and
requiring bit-identical subsequent decisions.

**It owns its own exits, unlike `DeltaStrangle`.** A strangle's exit is a
devolvement deadline or a P&L level unrelated to the entry signal, so
`DeltaStrangle` does nothing while held and lets the risk layer close it. A
crossover strategy's entry signal *is* the exit signal for the opposite side -
holding a long after MACD has turned bearish means holding a position the
strategy's own logic no longer believes in. So `MacdCrossover` follows
`CoinFlip`'s pattern instead: read the held position from the context, never
from memory (D-041), close on an opposing cross, open on a fresh cross when
flat. **No independent stop-loss or take-profit is added** - the alert tool has
none, and inventing one would no longer be the same strategy it watches. Between
crossings the strategy sits flat rather than always being long or short: closing
a position consumes the crossing event that triggered it, and re-entry waits for
the *next* one rather than immediately reversing into the opposite side. That is
a real design choice, not an oversight, and it costs roughly half of every
reversal's timeliness.

**Measured against real M5 XAUUSD bars and the real cost structure (D-121),
literal and unfiltered, in `scripts/measure_macd_xauusd.py`**: not routed
through `BacktestEngine`, which groups every session by `ist_date(bar.ts)`
throughout (margin lookups, chain snapshots, devolvement) - correct for MCX's
one-session-a-day shape, wrong for a 22:00-21:00 UTC continuous session, and not
something to generalise silently in code that also backs live MCX trading. The
same spirit as `bhavcopy_runner.py`: real strategy, real data, real costs,
reported as a shape test.

    50,000 M5 bars, 2025-12-12 .. 2026-08-28 (0.71 yr), 1 MT5 lot (100 oz) fixed
    trades closed   1,952         win rate    32.3%
    gross P&L       $12,271.00    buy-and-hold over the same window: $15,682.00
    spread paid     $56,608.00
    swap paid       $-4,541.13 (a net credit - see below)
    net P&L         -$39,795.87

**The literal, unfiltered crossover does not clear its own costs.** Gross P&L is
positive; spread ($56,608, at the $0.29 round-trip measured live) is more than
four and a half times larger and dominates completely. This is the same
arithmetic the back-of-envelope estimate gave before any strategy code was
written - 15.2 crossovers/day implies roughly 36% of notional a year in spread
alone - now confirmed against the actual strategy and the actual account's
numbers rather than a frequency count. Swap nets to a small *credit* here,
consistent with the account being roughly balanced between long and short
exposure over the window and short paying more than long costs (D-121);
consistent with the sign convention tests in `test_cfd_costs.py`.

This is not evidence the underlying MACD signal has no edge - gross P&L being
positive says the direction is doing something. It is evidence that trading
*every* crossover on a five-minute chart, with no filter and no minimum move
size, pays away the edge in spread faster than it collects it. Reported plainly
rather than adjusted to look better, per this project's standing rule (D-011,
D-108): a number is not trustworthy for having been made comfortable.

### D-124 - Slower timeframes, and a second strategy, measured the same honest way
Follow-up to D-123, on two fronts: does a slower bar interval change the
spread-dominates-everything conclusion, and how does a structurally different
signal (`TrendlineBreakout`, a Donchian channel - the well-defined form of
"trend line breakout": the highest high / lowest low of the last `lookback`
bars, no incremental state, no numeric-matching concern the way an EMA has)
fare under the same real costs.

**The first comparison was confounded, and worth stating exactly how.**
Fetching each timeframe's full 50,000-bar history (MT5's per-request cap) gives
each one a *different* calendar window - M15 covers 2.11 years, M30 4.22, H1
8.73 - so a naive comparison conflates "this timeframe" with "this historical
period happened to trend". `scripts/measure_macd_xauusd.py` now fetches all
requested timeframes, then trims every series to their **common overlapping
window** before the fair comparison, so the only thing that differs between
rows is genuinely the bar interval.

**On the same 2.11-year window (2024-07-17 .. 2026-08-28), both strategies get
better as the timeframe slows - dramatically, not marginally:**

    MacdCrossover              M15            M30            H1
      trades                 1,946            965            473
      win rate               30.6%          37.2%          37.2%
      net P&L          -$230,052.41     $97,653.20    $190,185.54

    TrendlineBreakout(20)       M15            M30            H1
      trades                   937            451            204
      win rate               36.1%          36.8%          42.6%
      net P&L            $50,983.08    $102,206.55    $162,297.62

Trade count roughly halves with each step to a slower timeframe (crossover/
breakout frequency, on this data, scales with **bar count** far more than with
calendar time - MACD's own full-history run had ~1,900-1,950 trades at every
one of M15/M30/H1 despite those windows differing by a factor of four, which is
what motivated checking whether the *fair* comparison told a different story).
Spread cost falls in lockstep with trade count while gross P&L holds up or
grows, so net P&L improves for the same reason it collapsed on M5 in D-123:
fewer round trips paying the same $0.29 each.

**`TrendlineBreakout` is net positive at every timeframe tested, including
M15** - the one place `MacdCrossover` was most deeply negative. Its spread cost
is lower at every timeframe too (a 20-bar channel breaks out far less often than
a MACD histogram crosses zero), and at H1 its net P&L ($162,297.62) is over 80%
of buy-and-hold ($199,700) on only 204 trades across 2.11 years - about one every
3.6 trading days.

**Two things this does not establish, stated because a positive number invites
skipping past them.** This is one 2.11-year window on one instrument, and it is
gold's own recent trending period (buy-and-hold itself returns ~$200k on the same
window) - a trend-following signal doing well while the underlying trended is not
distinguishable, from a single run, from the signal having genuine edge versus
merely being long-biased through a bull run. And neither strategy carries a
stop-loss (D-123's gap statement applies to `TrendlineBreakout` too, stated in
its own module docstring) - a single badly-timed hold between the entry and
the eventual opposing signal is uninsured against on either instrument, at any
timeframe.

### D-125 - A 0.5% price stop, added to both strategies, measured the same honest way
D-123 and D-124 both flagged the same gap in their own module docstrings: neither
strategy carried a stop-loss, so a single badly-timed hold between entry and the
next opposing signal was uninsured against. Closed for both, via one shared,
independently tested module (`algo/strategy/price_stop.py`) rather than two
copies that could drift apart: `stop_touched()` checks the bar's actual `low`/
`high` against a level `stop_pct` away from entry, not just the close - the same
"pessimistic reading, stop goes first" doctrine `algo/risk/exits.py` already
states for the MCX path (Q15), extended here because MT5 bars carry real
intrabar range that bhavcopy never did. A stop-triggered close fills at the
level itself, or the bar's `open` on a gap through it (`GAPPED_STOP`, mirroring
`algo/execution/fills.py`) - never at `bar.close +/- spread`, which would be
dishonestly optimistic for the one exit whose entire purpose is bounding a loss.
The stop is checked first in both strategies' `on_bar()`, before the warmup
gate, so a held position is never unprotected while an indicator is still
converging.

Three tests were found passing for the wrong reason while wiring this in - a
stop-triggered close and a signal-triggered close both look like "CLOSE,
opposite direction" from the outside, so assertions that only checked the
outcome couldn't tell which mechanism fired. Confirmed by computing the exact
move at which each fixture would trip a stop vs. a crossover/breakout, then
fixed by isolating the mechanism under test (`stop_loss_pct=Decimal("0")` where
a test means to exercise the signal alone) and asserting `"stop loss" not in
reason` to make the isolation checkable rather than incidental. Separately,
`stop_level()`'s side (which side subtracts vs. adds the move) was deliberately
sign-flipped and the suite re-run to confirm real coverage: 11 tests failed
across 3 files, including strategy-integration tests that never import
`price_stop.py` directly.

**Re-measured on the same fair, common-window methodology as D-124
(2.11-year overlap, both strategies, all three timeframes), stop vs. no-stop:**

    MacdCrossover          M15 (no stop -> stop)   M30                H1
      trades                  1,946 -> 2,055     965 -> 1,075     473 -> 569
      net P&L          -$230,052 -> -$77,668   $97,653 -> $77,836   $190,186 -> $132,919

    TrendlineBreakout(20)  M15 (no stop -> stop)   M30                H1
      trades                    937 -> 1,083     451 -> 582        204 -> 305
      net P&L            $50,983 -> -$14,779   $102,207 -> $52,467  $162,298 -> $136,477

**The effect is not uniformly good, and that is the honest result.** Trade
count rises everywhere (a stop-out is an exit that can re-enter before the
original signal would have reversed) which adds spread on every row. Where the
strategy was previously worst - `MacdCrossover` on M15, the one deeply negative
case in D-124 - the stop is a large net improvement (-$230k to -$78k): it caps
the worst crossover holds before they run further against the position. But on
every row that was previously net *positive*, the stop makes it worse,
including flipping `TrendlineBreakout` M15 from +$50,983 to -$14,779. A
Donchian channel already carries its own exit (the opposing breakout); a
tighter 0.5%-of-entry price stop sitting in front of it now closes some winning
trend-following holds early, before the channel's own signal would have, and
the resulting re-entries add cost without adding edge. A stop-loss bounds the
worst case; it does not come free on a strategy whose own exit was already
doing useful work, and reporting only the M15 MACD improvement while leaving
the other five rows out would have been the same kind of dishonesty D-011/D-108
already rule out.

This does not mean 0.5% is the right distance for every case above - only that
a single, unfitted default was measured plainly rather than tuned to look good.
Choosing a per-strategy or per-timeframe stop distance (tighter for the M15
crossover, looser or absent for the slower trend-following rows) is a genuine
follow-up, not attempted here since it was not asked for and risks fitting the
stop to this one 2.11-year window's particulars.

### D-126 - Widening the stop to 1% does not simply split the difference
Direct follow-up to D-125's own closing caveat, now actually checked: re-ran
the identical fair-window methodology with `STOP_LOSS_PCT` at 1.0 instead of
0.5, both strategies, all three timeframes.

    MacdCrossover net P&L      M15               M30              H1
      no stop            -$230,052.41       $97,653.20      $190,185.54
      0.5% stop            -$77,668.32       $77,835.84      $132,919.15
      1.0% stop           -$246,375.46       $44,000.66       $77,915.80

    TrendlineBreakout net P&L  M15               M30              H1
      no stop              $50,983.08      $102,206.55      $162,297.62
      0.5% stop           -$14,778.90       $52,466.78      $136,477.21
      1.0% stop           $131,882.10       $71,710.76       $79,592.87

**There is no monotonic "wider stop is closer to no stop" relationship in this
data, and it is worth being precise about why not.** A wider stop is not simply
a weaker version of a tighter one on the same trade: whether a trade's outcome
touches 0.5% before recovering, touches 1% before recovering, or never
recovers at all is a property of that trade's own path, not something that
interpolates smoothly as the threshold moves. Two opposite failure modes are
visible side by side here. On `MacdCrossover` M15, 1% is worse than *no stop at
all* ($-246,375 vs $-230,052, on nearly the same trade count) - some fraction
of trades run past 0.5% (where D-125's tighter stop would already have cut
them for less), keep going to 1% before the stop locks in a larger loss, on
holds that would have closed smaller, or even recovered, by the time the next
opposing crossover fired. On `TrendlineBreakout` M15, the same 1% widening
does the opposite: it turns 0.5%'s worst result (-$14,779, the one row D-125
flagged as the stop actively hurting a working exit) into the *best* result of
all nine cells in both tables ($131,882) - loose enough here to stop avoiding
the channel's own genuine winners, tight enough to still cut the trades that
were dragging the unstopped case down.

Both strategies' M30/H1 rows tell a third story again: every stop setting
underperforms no-stop at H1 for both strategies, and the ordering between 0.5%
and 1% flips between the two strategies at M30 (`MacdCrossover` gets worse as
the stop widens; `TrendlineBreakout` gets better). Nine cells, no shared
pattern a single number would capture - which is the actual finding, not a
gap in the analysis. **A stop distance is not a property of "safety" that a
strategy either has enough of or not; it interacts with each strategy's own
holding-period and exit logic differently enough that picking one without
testing per case is a guess, not an improvement.** Restated from D-125: choosing
a distance per strategy/timeframe pairing - or concluding some pairings (both
strategies at H1, on this window) are better left with no stop at all - is the
real next step, and still has not been attempted, to avoid fitting nine cells
of one 2.11-year sample.

### D-127 - A minimum-2%-profit, 0.5%-trailing exit, and why it is not the same question as D-125/D-126
A different request from the flat stop D-125/D-126 measured: a trailing stop
that does nothing until a position is up 2%, then follows the best price seen
since entry at 0.5%. New shared module, `algo/strategy/trailing_profit_stop.py`
(`start_trail`/`advance_trail`/`is_armed`/`trail_level`/`trail_touched`/
`trail_fill_price`), wired into both strategies as a second, independent exit
behind a `trail_pct` parameter that defaults to 0 (off) so every existing
caller and test keeps today's behaviour unchanged. Same conventions as the
flat stop: the peak advances to each bar's best-case price before the trail is
tested against that same bar's worst-case price (documented as a stated,
deliberate resolution of the OHLC same-bar ordering ambiguity, the same way
`price_stop.py` resolves "which touched first"); a triggered exit fills at the
trail level or the bar's open on a gap, never `bar.close +/- spread`. Enabling
it is the one thing that gives `TrendlineBreakout` persisted state - the
running peak needs to survive a restart the same way `MacdCrossover`'s EMAs
already do.

**Measured with the flat stop turned off** (`stop_loss_pct=0`) so the trailing
mechanism's own effect is not tangled with D-125/D-126's already-measured one -
this is "does 2%-then-0.5%-trail work as the *only* exit beyond the strategy's
own signal", not "does adding it on top of a flat stop help":

    MacdCrossover net P&L        M15               M30               H1
      no stop (D-124)      -$230,052.41       $97,653.20      $190,185.54
      2%/0.5% trail         -$406,480.37     -$245,940.96     -$136,984.56

    TrendlineBreakout net P&L    M15               M30               H1
      no stop (D-124)        $50,983.08      $102,206.55      $162,297.62
      2%/0.5% trail        -$162,229.22     -$144,519.44      -$48,683.50

**Every one of six cells is negative, several by more than any configuration
measured so far** - worse than no stop, worse than either flat-stop width in
D-125/D-126, on both strategies, at every timeframe. Win rate actually rose on
every row (MACD: 30.6/37.2/37.2% -> 32.8/34.1/36.1%; breakout: 36.1/36.8/42.6%
-> 36.6/36.3/45.3%) while net P&L collapsed - the signature of winners being
cut short while losers run unbounded. That is exactly what this configuration
does by construction: a trade that never reaches +2% has **no exit of its own**
until the opposing crossover or breakout eventually fires - the flat stop that
would have bounded it is off - while a trade that does reach +2% is locked in
at roughly that level the moment it gives back 0.5%, well short of how far
`MacdCrossover` M30's and `TrendlineBreakout` H1's own unstopped winners ran in
D-124. Both strategies' real edge on this window came from a small number of
large trend-following winners (D-124's own read of the numbers); capping
exactly those while leaving every loser fully exposed is close to the worst
combination available, and the measured collapse is that mechanism, not a
defect in it.

**This is not a verdict on trailing stops in general - it is a verdict on
running one with no downside protection under it.** D-125/D-126 already showed
a flat stop alone is not a uniform improvement either. Whether a flat stop
*and* a 2%-activated trail together - the flat one bounding the loser side this
measurement left open, the trail locking in the winner side once a trade earns
it - beats either alone is a real, different question this run does not
answer, and is the natural next measurement rather than a conclusion to draw
from extrapolation.

### D-128 - A whole-project audit, run as three parallel scans, eight real fixes
"Scan the project and make improvements" - not scoped to XAUUSD/CFD work, so
this covered every directory under `algo/` and the standalone alert tool. Three
independent audits ran in parallel (core/risk/execution/costs/pricing;
backtest/exchange/data/portfolio; api/cli/config/live/persistence/reporting),
each told the codebase already passes `ruff check .` and `mypy --strict`
cleanly - so nothing reported here is something either tool would have caught,
and each finding needed to survive being checked against the file's own
docstring before counting (this codebase explains a lot of its own unusual
decisions in prose; "looks wrong" is not "is wrong" here).

A second Claude Code session was independently active on this same working
tree at the same time. Coordinated by message rather than by guessing: it
confirmed an uncommitted `ProtectiveExits` refactor and a round of mypy-strict
test fixes already in the tree were neither its work nor this session's,
almost certainly done directly by the person running both sessions - left
untouched throughout, along with `pyproject.toml` and everything under
`scripts/`. A `git show` on a fixed commit SHA reportedly returning different
content on two reads was checked immediately (`git fsck --full`: clean;
repeated reads: identical) and not chased further once it stopped reproducing.

**Eight verified, fixed, and tested; several more flagged rather than
blind-fixed.** Every fix below followed the same discipline as this session's
own stop-loss and trailing-stop work: reproduce the exact failure first
(a live `load_config` call, a real `requests` exception, a constructed
`Signal` with a non-default field), write the regression test, and for the
ones with real teeth - the SmartAPI request window, the Kotak expiry
conflation, the MACD index guard - deliberately revert the fix and confirm the
new test actually fails before restoring it, the same sign-flip discipline
D-125 used on the stop-loss primitives.

    algo/config/loader.py      ALGO_API_TOKEN (the API server's own bearer
                                token, documented in .env.example) was not
                                excluded from the env-to-config sweep, so
                                setting it exactly as documented broke every
                                CLI command with "api_token - Extra inputs are
                                not permitted". Reproduced directly; fixed by
                                adding it to a new exact-match exclusion set
                                alongside the existing namespace-prefix one
                                (which itself had no test coverage before now).

    algo/config/schema.py      entry_bars_ist: [] loaded successfully and then
                                crashed `algo config` with a raw IndexError
                                instead of an actionable ConfigError. Added the
                                same at-least-one validator `instruments`
                                already has.

    algo/risk/engine.py        The max_lots_per_underlying cap compared
                                lots_held against the sizer's raw lot count,
                                but the order actually placed scales every leg
                                by SignalLeg.ratio (validated to allow >= 1,
                                consumed the same way by backtest/engine.py's
                                margin notional). No strategy in the codebase
                                emits ratio != 1 today, so this was latent, not
                                exploited - but a real landmine for whichever
                                multi-leg strategy is next. Fixed to check
                                against the worst-case scaled leg.

    algo/execution/paper.py    PaperBroker.restore()'s instrument-kind
                                dispatch handled "future" and "option" only;
                                InstrumentId is now a three-way union
                                (CfdId, added this session for XAUUSD). A
                                restart with a CFD position open would have
                                raised a pydantic ValidationError instead of
                                recovering - the crash-recovery path failing
                                exactly when a real position most needs it
                                recovered. Added the missing branch.

    algo/data/smartapi_feed.py fetch_bar_history's request window formatted
                                UTC datetimes directly as the candle API's
                                fromdate/todate - which the same file's own
                                _bar_from_candle already documents as IST wall
                                time. Every live poll and every backtest fetch
                                was asking for a window shifted 5.5 hours from
                                the one actually wanted. Fixed with the
                                project's own to_ist() helper; reverting it
                                made the new regression test fail exactly as
                                predicted (03:30 leaking through where 09:00
                                was expected).

    algo/data/kotak_feed.py    _option_id built every OptionId's
                                underlying_future using the *option's own*
                                expiry, not the real futures contract's -
                                precisely the conflation algo/core/instrument.py
                                warns "walks a short leg into devolvement".
                                Every other OptionId construction site in the
                                codebase already resolves the real futures
                                expiry; this was the one live (not
                                synthetic/historical) chain path that didn't.
                                Threaded the already-available futures row's
                                own expiry through instead.

    algo/cli/main.py           _run_paper_loop - the function behind `algo
                                live --passes N` - built its BacktestEngine
                                without flatten_on_trip, stop_viability_threshold
                                or on_stop_viability_breach, silently falling
                                back to the engine's own defaults (no flatten,
                                no viability guard) regardless of what the
                                config said. The sibling backtest_bhavcopy and
                                backtest_smartapi commands already wire all
                                three correctly - this was the one command that
                                actually runs live/paper trading, missing them.
                                The same "one settings object, not fifteen
                                hand-copied reads" bug class D-117 already
                                named and fixed once (RunSettings) recurring at
                                the one call site never migrated to it. Fixed
                                by reading the same three config paths inline,
                                matching the existing style of that function
                                rather than pulling in RunSettings wholesale.
                                Verified by direct comparison against
                                BacktestEngine's real constructor signature and
                                a clean mypy --strict pass; not integration-
                                tested end-to-end - built a fake feed/chain
                                harness for that would be a separate,
                                larger undertaking than this fix warranted.

    tools/macd_telegram_alert  The bot token lives in the request URL
    /macd_alert.py             (Telegram's own API shape). A network-level
                                requests exception's str() includes the full
                                URL, and every retry/failure path logged the
                                exception verbatim - a transient connection
                                blip during a send or a command-panel poll
                                wrote the live token to disk in plaintext.
                                Reproduced with a real ConnectTimeoutError
                                against a fake token. Fixed with a small
                                regex redaction applied at all three logging
                                sites; verified directly since this
                                standalone tool has no existing test suite to
                                extend.

    algo/pricing/indicators.py Macd._crossed's warmup guard checked
                                `index != 0` to detect "the first element,
                                no predecessor" - true for the positive form
                                but not for index == -len(histogram), the
                                equivalent negative form, which fell through
                                to histogram[-len(histogram) - 1] and raised
                                IndexError. Currently unreachable in
                                production (macd_crossover.py reimplements
                                the same comparison inline rather than
                                calling these methods - itself flagged below,
                                not fixed), but a real defect in code that is
                                still a public method on a dataclass nothing
                                stops a future caller from using at that
                                boundary. Fixed by normalizing both forms
                                with a single modulo check.

**Flagged, not fixed - each is a design call, not a one-line correction, and
guessing at the right one would be exactly the kind of unrequested feature
work this project's own prime directive (correctness over features) argues
against.** `algo/costs/cfd.py`'s `SwapModel` is fully built and tested but
never called from `BacktestEngine` or the execution layer - almost certainly
deliberate staging given this session's own precedent (the MT5 measurement
work runs through a standalone script specifically because the engine's
`ist_date`-everywhere shape doesn't fit a continuous FX session), not
re-litigated here. `algo/execution/reconcile.py`'s docstring claims state
differences are "reported," but an ordinary SENT->FILLED catch-up produces no
`Drift` and `DriftKind.STATE_MISMATCH` is never constructed anywhere - could be
an intentional "only *unexpected* differences count" reading, could be a
dropped path; needs a decision about what counts as reportable, not a guess.
`algo/backtest/engine.py`'s margin model treats "is an `OptionId`" as a proxy
for "is short" (accurate only because `DeltaStrangle` never buys an option
today) and its `lots_held` for options always resolves to the futures
instrument's position, currently masked because every CLI wiring hardcodes
`max_concurrent_positions=2` for a strangle regardless of what config says -
the same hardcoding the risk-engine.py fix above depends on for its own safety
margin, so loosening it without also fixing these two would be a regression,
not an improvement. Recorded here so the connection isn't lost if any one of
these is picked up in isolation later.

### D-129 - The MT5 path finally runs, on paper, and session-day had to become pluggable
Asked to "set up and trigger this algo to start trading" on a Vantage demo
account. Four things stood in the way, and only one of them was a switch:
`trade_allowed` is False in the terminal (the operator's own toggle), the market
was shut (Saturday), the measured evidence for these strategies is weak
(D-124/D-126/D-127), and - the real blocker - **there was no runnable MT5 path
at all**. `Mt5Broker` was referenced nowhere outside its own tests, and neither
CFD strategy appeared in `algo/cli/` or `algo/live/`. `algo live` is MCX-only
by construction (SmartAPI bars, Kotak chain, `DeltaStrangle`, `mcx_calendar`).

**Paper fills on live data, chosen deliberately over demo orders.**
`algo/live/mt5_runner.py` wires `Mt5BarFeed` -> strategy -> `RiskEngine` ->
`OrderRouter` -> `PaperBroker`, with `StateStore` so the dashboard sees it.
Real bars, simulated fills, `Mt5Broker.place` not in the loop at all. That
method has never been called against a live endpoint; an unattended loop is the
wrong place to find out what it does, and a demo fill was an approximation
anyway. `build_mt5_paper_loop` takes a `Broker` it does not construct, so
swapping in `Mt5Broker` later is a caller's decision with a caller's eyes on
it, not a flag this module flips.

**`ist_date` session grouping was not an approximation for FX - it was a
crash.** D-121/D-123 recorded that `BacktestEngine` groups every unit of work by
`ist_date(bar.ts)`, correct for MCX and the reason the measurement script
bypassed the engine. The `mt5_runner` docstring initially repeated that as a
soft caveat: "the daily loss limit resets at IST midnight rather than the 21:00
rollover." The first end-to-end test showed that was wrong and too generous. A
bar closing 20:00 UTC on a **Friday** is IST *Saturday*; `ForexCalendar` has no
Saturday session and raises `CalendarError`. Roughly half the bars in a normal
week land that way, so the loop could not complete a single pass. Fixed by
giving `BacktestEngine` and `LiveLoop` a `session_day_for` callable defaulting
to `ist_date` - every MCX path byte-identical, nothing to re-verify there - and
passing `ForexCalendar.session_day_for` on the CFD path, which names a session
by its 21:00 close, the same instant financing is charged. The docstring was
corrected rather than left describing the gentler bug. Supporting changes:
`ForexCalendar.bar_boundaries` (the engine needs it and only `MarketCalendar`
had it - the truncation rule now lives once, in `session_bar_boundaries`, rather
than as two copies that could disagree about the stub), and a `SessionCalendar`
Protocol so a CFD run types cleanly without pretending a 24/5 venue is an
exchange.

**A second wiring bug the tests caught.** The loop was first seeded with
`seed_bars[:1]`, copied from `_run_paper_loop`, where the chain rather than bar
history drives `DeltaStrangle`. For a rolling-window strategy that starves it:
a 20-bar Donchian channel sits in warmup forever and silently never trades -
the failure mode that looks like "the market was quiet." Now seeded with all but
the newest bar, since `LiveLoop` appends that one itself and `append_bar`
refuses anything not strictly after the last.

`tests/test_mt5_runner.py` drives the real feed, router, journal and paper
broker against a scripted terminal and asserts the things that actually matter:
a breakout reaches the broker as a fill, the same bar polled twice does not
double the position, a flat market places nothing, and the broker in the loop is
the paper one. `algo live-mt5` reports the account, states "PAPER - no order
reaches this account", measures the server clock rather than assuming it, and
exits cleanly when the venue is shut instead of looping on a stale tick -
verified against the real terminal (account 25804244, VantageMarkets-Demo).

**Still true and still stated: this loop does not charge swap.** `SwapModel` is
tested but wired into no engine path (D-128 flagged it). A position carried past
the rollover pays spread but not financing, so P&L here is optimistic in exactly
the direction `cfd.py` warns about.

### D-130 - A research console in the dashboard, without weakening the read-only rule
Asked for "advanced and most wanted features... so I can customize anything,
backtest there, change my strategies and timeframe". That collides with a
decision written down in four places - brief Q21, `api/app.py`, `page.tsx` and
the dashboard footer - that live parameters are deliberately **not** editable
from the UI: "every live parameter traces to a committed file rather than to
something someone typed into a browser once."

**The split that resolves it.** A backtest touches no live state: it reads
history, runs a strategy instance built for that one request, and returns
numbers, holding no `Portfolio`, no `OrderRouter`, no broker, and writing to no
`StateStore` the engine reads. Choosing a timeframe for a *study* is
exploration; changing what the running loop trades is a deployment. So the
console got everything - strategy, timeframe, channel length, stop, trail,
size, history depth - and there is deliberately **no "apply to the live loop"
button**. Q21 stands untouched.

**The guard caught the first attempt, and the guard was right.** `/research/
backtest` was written as a POST, because it takes parameters. That tripped
`TestReadOnly.test_exactly_one_write_endpoint_exists` - "the guard that
survives someone adding a 'close position' button later." The fix was not to
widen the test. A backtest is nullipotent, so it became a GET with query
parameters: semantically correct, and the invariant stays *literally* true
rather than explained away. New tests assert the endpoint is a GET and that no
POST variant exists, so the next person to reach for a POST here trips the same
wire.

**One implementation of what a trade costs.** The per-bar CFD backtest existed
only inside `scripts/measure_macd_xauusd.py`, where D-123 through D-127 grew it.
Extracted to `algo/backtest/cfd_runner.py` so the console and the script price a
trade identically - the same argument `strategy_for` already makes for strategy
lookup. Stop and trail exits still fill at their own level or the bar's open on
a gap, never at `bar.close +/- spread`.

**The offset cache, and why it is safe here but not in the live loop.**
`measure_server_offset` refuses a stale tick, which is correct for trading and
wrong for research: the weekend is exactly when you want to run a study, and it
is exactly when the newest tick is hours old. `algo/data/mt5_history.py`
measures whenever a fresh tick allows, caches to disk, and reuses otherwise -
reporting a cached offset *as* cached, never as a live reading. `CACHE_MAX_AGE`
(21 days) bounds it, comfortably shorter than the gap between DST transitions
and longer than any weekend, so the case it exists for always works and the case
it must not get wrong cannot happen silently. The live loop still measures every
start and does not use this.

Seeding the first cache entry was itself a measurement, not an assumption: the
last tick read 2026-08-28 23:56:59 as-if-UTC, and of the two EET candidates only
+3h places it inside the trading week - 3 minutes before the Friday 21:00 UTC
close (D-121's measured structure). +2h would put it 57 minutes *after* the
market shut, which is impossible. `measured_at` was stamped with the tick's real
time, not with now.

**Building the console found a real bug in `TrendlineBreakout`.** Its exit
reason named the break direction off `closing_side`, which inverts it: a short
is closed by a fresh *high* and its closing side is BUY, so every close was
labelled backwards. Visible immediately once trades rendered in the browser -
"fresh 20-bar low" against `close 4470.20 vs channel [4428.80, 4469.28]`, a
close plainly *above* the channel high. Cosmetic in that it moves no money, and
not cosmetic at all in a codebase whose trade log exists to answer "why did this
fire six weeks later." No test asserted the label, which is why it survived;
two now do, and reverting the fix makes both fail on exactly the inverted text.
The label is now taken from `broke_up` directly, so it cannot invert again.

A second, smaller finding from the same pass: `run_study` validated the strategy
name only after attaching to MT5 and pulling bars, so a misspelling cost seconds
and reported the wrong problem first. Cheap checks now run before the terminal
is touched.

**Every result carries its caveats, and `caveats` is never empty.** The
tearsheet already refuses to print a ratio without its sample size and the
stats panel renders `null` rather than a flattering zero; a console returning a
big green number with nothing qualifying it would undo both at the last step.
Trade counts under 30 and windows under 180 days say so explicitly. Worth
noting what the console immediately demonstrated: breakout/M30 over the last
154 days returns **-$6,681**, against the **+$102,207** D-124 measured for the
same strategy and timeframe over 2.11 years. Same code, same costs, different
window, opposite conclusion - which is the caveat, stated by the tool itself.

### D-131 - Walk-forward reaches the dashboard, and says the optimisation is fitting noise
`algo/backtest/walkforward.py` has held the honest machinery since Milestone 5 -
rolling windows, per-parameter stability, the fixed-parameter baseline, and a
`Feasibility` gate that refuses a confident headline on thin data. Nothing had
ever driven it with real bars; a repo-wide grep found zero references from the
API or the dashboard, and the only CLI entry point was the *synthetic*
feasibility calculator. It was the most valuable thing already built and
entirely unreachable.

Wired now: `algo/backtest/cfd_walkforward.py` drives it over MT5 history using
the same `run_cfd_backtest` the console uses, adapting the CFD equity curve to
what `metrics.compute` reads. `CfdResult.equity_curve` gained a third field -
positions open on that bar - so exposure is *measured* rather than defaulted;
without it every run would report 0% or 100% alike.

**The first real run is the finding, and it is not flattering.** Breakout on
H1, optimising channel length, 13 windows, 89 out-of-sample trades - past
`MIN_OOS_TRADES`, so `ADEQUATE`: this is a conclusion the data can support.

    in sample (fitted)            $471,555.87
    out of sample                  $31,287.82
    fixed params, out of sample    $91,074.74
    optimisation beat doing nothing:  False
    lookback: UNSTABLE  40 -> 20 -> 20 -> 20 -> 40 -> 10 -> 20 -> 20
                        -> 10 -> 10 -> 80 -> 80 -> 20

In-sample P&L is **fifteen times** the out-of-sample figure, and choosing the
channel length per window did roughly **a third** as well as never touching it.
The chosen value wanders across the whole grid. That is the textbook signature
of curve fitting, produced by the project's own tooling on the project's own
strategy - and it is exactly what every single-window backtest here (D-124,
D-127, D-130) could not have told us. The panel renders "optimising did not
beat doing nothing" as a verdict rather than a number, because it is the most
informative line on the page and the easiest one to skim past.

**Design choices worth naming.** The grid is capped at `MAX_GRID` (24) and
refuses rather than truncates - silently searching a smaller grid than asked
for would report a study the caller did not run, and a wider grid fits the
in-sample window better without producing more evidence. `MIN_IS_TRADES` makes
a candidate ineligible below 5 in-sample trades, so the optimiser cannot pick
whichever parameter set took two lucky trades. The objective is net P&L rather
than Sharpe: over a 90-day window the ratio is dominated by how few trades it
took, which is the same reason `metrics.py` refuses to print one without its
sample size. Optimising `lookback` on MACD is refused outright - MACD has no
channel length, so the grid would be identical runs and the stability verdict
would describe noise that never varied.

Endpoint is a `GET`, for the reason D-130 established: nullipotent, and the
"exactly one write endpoint" guard is worth more than request-shape
convenience. The catalogue is now memoised at module scope in `lib/api.ts` so
the two research panels share one fetch and cannot disagree about which
strategies or axes exist.

### D-132 - A parameter sweep that argues with its own best cell
D-125 through D-127 were this done by hand: run the strategy across timeframes
and stop settings, tabulate nine cells, read the pattern. `algo/backtest/
sweep.py` does it in one request, with a heatmap panel.

**The design problem, and the whole reason this entry exists.** A heatmap
invites reading off the greenest square and adopting those parameters. D-131
had just finished demonstrating that doing exactly that is how you fit noise -
and a sweep is a *weaker* procedure than that walk-forward, not a stronger one,
because it has no out-of-sample step at all. Shipping a grid of pretty colours
with a bright maximum would have handed the reader the precise mistake the
previous panel exists to catch.

So every sweep is scored for **robustness** before its maximum is reported:

    PLATEAU        the best cell's neighbours perform similarly - what a real
                   effect looks like
    ISOLATED PEAK  neighbours average below PLATEAU_RATIO (0.5) of the best -
                   a spike in a rough surface, fitted to whichever trades fell
                   inside this window
    NOTHING WORKS  no cell is profitable; there is no setting to pick here

The verdict renders **above** the grid, not below it, and the best cell is
*outlined* rather than filled brightest so the eye finds the shape of the
surface before the maximum. Colour is diverging and anchored at **zero**, never
at the grid's own minimum - otherwise a grid where every setting loses money
would render as a pleasant gradient with a "best" green corner. The positive-
cell fraction is reported next to the maximum, because a grid where three of
sixteen cells make money is better read as "this does not work" than as "one
setting works". Every result carries the note that a sweep has no out-of-sample
step and the walk-forward panel should be run before trusting any cell.

**Bounded, and refusing rather than truncating.** `MAX_CELLS` (64) caps the
grid; past it the request is refused, because silently searching a smaller grid
would report a study the caller did not run - the same rule `build_grid` and
the config loader already follow. Bars are fetched once per *timeframe* rather
than once per cell, so a 4x4 grid varying timeframe pulls four histories, not
sixteen. Sweeping channel length on MACD is refused outright: MACD has no such
knob, so the grid would be identical columns and the verdict would describe
noise that never varied.

Two bugs in my own tests, caught by running them: `"ab c".split()` is
`['ab', 'c']`, so the fixtures built 2x3 grids while asserting against 9 cells;
and `run_sweep_study` had no default timeframe, which a sweep varying timeframe
as an axis does not need to supply. Both fixed rather than worked around.

### D-133 - Graceful shutdown, and repairing the CLI split that arrived with it
Two things this entry covers, because they arrived together.

**The CLI split landed from outside this session, and broke the tree.** An
improvement plan named "CLI monolith - main.py is 2,141 lines" as its top
priority; by the time it was read, `main.py` was 51 lines and the commands lived
in `algo/cli/cmd_*.py`. The refactor was right and overdue. It also left:

    algo backtest          crashed outright - `expiry=calendar.date(2026, 9, 4)`,
                           a botched find-replace, with a comment left in
                           admitting it ("calendar does not have .date()")
    cmd_live.py            12 undefined names - AppConfig, InstrumentMaster,
                           SystemClock, SmartConnectTransport used in
                           annotations, imported only inside function bodies
    ruff                   57 errors, 24 of them B008 because the per-file
                           ignore was pinned to `algo/cli/main.py` and stopped
                           covering the commands the moment they moved
    mypy                   16 errors, incl. implicit re-exports strict rejects

All repaired: a real `date` import, `TYPE_CHECKING` blocks for annotation-only
names (the bodies import lazily on purpose - `algo live --help` should not drag
in a broker SDK), `OptionChainSnapshot` imported from its canonical home
`algo.core.chain` rather than through `chain_greeks`, `__all__` on `_helpers`,
and the B008 ignore rescoped to `algo/cli/*.py`. Every command smoke-tested.

**Graceful shutdown.** `LiveLoop.run` was bounded by `max_passes` and `until`,
which stops a loop that runs to completion and does nothing for the case that
actually happens: Ctrl-C at an arbitrary instant. Python's default SIGINT
handler raises at the next bytecode boundary, and inside `pass_once` that
boundary can fall between `router.place()` handing an order to the broker and
the journal recording what came back - exactly the `SENT` state nobody wants to
restart from.

`algo/live/shutdown.py` installs handlers that **do not raise**. They set a
flag; the in-flight pass finishes on its own terms; `run` stops at the boundary
between passes, where the journal is consistent by construction. A second
signal restores the default handler and re-raises, because an operator who
presses Ctrl-C twice means it and a loop that cannot be stopped is not safer
than one that stops messily. Handlers are restored on exit, and outside the
main thread it degrades to "no handlers installed" rather than refusing to run.

Same shape as the kill switch (D-012): a request the loop reads, not an action
the handler takes. A signal handler runs at an arbitrary moment and the list of
things it is safe to do there is very short.

`run` also sleeps in 0.5s slices when a `should_stop` is supplied, so a stop
asked for one second into a sixty-second poll is acted on in about a second. A
loop that ignored Ctrl-C for a whole poll interval would simply get Ctrl-C
pressed again, which is the outcome this avoids. Callers that pass no
`should_stop` still get a single `sleep(interval)` call - no behaviour change.

**A test that overclaimed, caught by trying to break it.** The first version
included `test_a_pass_already_running_is_never_truncated`, and deliberately
sabotaging the loop did **not** fail it - because non-truncation is a
*structural* property (`run` never hands `should_stop` to `pass_once`, which
takes no such parameter), not a behavioural one a runtime test can demonstrate.
Replaced with an honest pair: one asserting `pass_once`'s signature has no stop
hook, which fails the moment someone threads one in, and one asserting the
observable half - the pass that ran before the stop reached a decision. Both
verified by sabotage, as was the sleep-slicing test.

`live-mt5` now records a note distinguishing "asked to stop and did" from
"died mid-bar" - the two states a journal in `SENT` cannot tell apart on its
own - and sets `engine: stopped` in health so the dashboard shows it.

### D-134 - Alerting, built around the rule that it must never break trading
The system had a kill switch and a dashboard, and both are *pull* - they answer
a question someone thought to ask. A halt at 11pm was learned about whenever
somebody next looked, which is the opposite of what a kill switch tripping
means.

**The rule the module is written around.** A notifier that can raise into the
loop, block it, or halt it converts a Telegram outage into a *trading* outage -
strictly worse than no alerter. So `Alerter.send` never raises: every failure is
caught, logged and dropped, including a notifier that violates the protocol and
throws. Sending is not retried either. `macd_alert.py` retries with backoff
because delivering the alert *is* its job; here the job is trading, and a loop
sleeping through a backoff is a loop not watching the market. Timeout is 5s for
the same reason.

The failure paths are what the tests actually cover - a notifier that throws, a
network that times out, an endpoint that rejects - and both safety properties
were verified by deliberate sabotage: letting exceptions escape failed two
tests, and logging the raw exception instead of redacting it failed a third with
the live token visible in the captured output.

**D-128's lesson, applied at the design stage rather than found later.**
Telegram puts the bot token in the URL path, so a `requests` exception's own
`str()` carries it - which is how a live token got written to `macd_alert.log`
in plaintext. Every log line here goes through `redact`, and a test asserts a
token cannot survive one.

**Credentials never touch config.** `ALGO_TELEGRAM_*` read straight from the
environment and added to `NON_CONFIG_ENV_PREFIXES`, so they cannot be swept into
`AppConfig` and therefore into `config_hash`, which is stamped into every signal
id and artefact. `test_config.py` now asserts the token does not appear anywhere
in a dumped config.

**What actually fires.** Loop started, loop stopped (with the open-position
count), orders placed, and orders the router refused. Nothing per-poll: a
message every thirty seconds is noise nobody reads, and an alerter people mute
is an alerter that is not there. A refusal is `BLOCKED_UNRECONCILED` doing its
job, but it also means intent and reality may now differ - which is exactly what
deserves a person rather than a log line.

An unconfigured run gets a log-only alerter rather than an error. Refusing to
start a trading loop because nobody set a Telegram token would be the tail
wagging the dog.

**A bug found by actually running it.** The first version marked severities with
`•`, `⚠` and `■`. Printing a rendered alert on this machine raised
`UnicodeEncodeError: 'charmap' codec can't encode character '\\u25a0'` - exactly
what `TestTheCliSourceStaysAscii` already warns about ("Windows consoles default
to a legacy code page... a rupee sign raises"), and `LogNotifier` writes to one.
Replaced with `*`, `!`, `!!`, and a test now encodes every rendered severity as
cp1252 so a decorative glyph cannot creep back in.

**Also re-applied here: the graceful-shutdown CLI wiring from D-133**, which had
been overwritten in `cmd_mt5.py` between sessions. The module and the
`should_stop` support in `LiveLoop.run` had survived; only the command-level
wiring was lost, and it was noticed because the file's mtime was later than the
edit that wrote it.

### D-135 - A bad poll is not a reason to stop trading, and a crash must not be silent
Both of these came from the same real failure rather than from a review. A live
paper run died two minutes into an hour-long session:

    DataError: MT5 returned no 60m bars for XAUUSD: (-10001, 'IPC send failed')

One transient hiccup in the terminal's IPC channel, raised by
`Mt5BarFeed.closed_bars`, propagated straight out of `pass_once`, past
`LiveLoop.run`, and ended the process. Nothing was open so nothing was lost -
but a loop that dies on one bad poll is not a loop anyone can leave running.

**The retry, and where its line is drawn.** `errors.py` already had the
distinction this needed: "the split that matters operationally is Retryable vs
Fatal". `run` now catches `DataError` and `RetryableBrokerError`, reports them
through `on_error`, and tries again on the next poll. `FatalBrokerError`,
`DomainError` and `LookAheadError` still propagate untouched, because those say
the run itself is wrong and retrying would only repeat it.

`max_consecutive_errors` (5) bounds it, and the counter **resets on any
successful pass** - five failures spread across an hour is a flaky feed, five
in a row is an outage, and only the second should stop a run. A loop that spun
silently through a dead feed would be a worse failure than the one being fixed.
The retry also sleeps the normal poll interval rather than hammering a feed
that is already struggling.

**The crash is now evidence rather than absence.** The dead run had alerted
only that it had *started*; the operator's entire signal that something was
wrong was that no further messages arrived - which is indistinguishable from a
quiet market. The loop is now wrapped so a fatal exit clears the stop sentinel,
records `engine: crashed` with the exception, and sends a CRITICAL alert naming
the error and the open-position count. Transient failures escalate to a WARNING
only on the second consecutive one; alerting on every flaky poll is how an
alerter gets muted.

**The heartbeat that made the crash visible at all.** Written just before this
happened, and immediately earned: `engine: running` is set at startup and
`stopped` only by the exit handler, which a crashed process never reaches - so
the word alone cannot distinguish a working loop from a dead one, and the
dashboard would have shown a confident green "running" indefinitely. The loop
now stamps `heartbeat` every pass (not every bar: on a 60m timeframe a
bar-driven heartbeat looks stalled for an hour at a time, which is exactly what
it must distinguish from). `/health` reports `stale` past `HEARTBEAT_GRACE_S`
and says so in `warnings`. The crashed run's state file read `engine: running`
with a heartbeat 54,157 seconds old, which is the case in one line.

A cleanly stopped loop is never called stale, and a state file with no
heartbeat at all - written by an older build - is not treated as dead either.
Both verified, along with the staleness itself, by sabotage.

**Testing note.** The first version of the retry tests monkeypatched
`pass_once`, which `LiveLoop.__slots__` forbids. Driving the failure through
the *feed* instead is both possible and more faithful: it is precisely where
the real error came from. `ScriptedBars` grew `fail_next` and `failure` for it.
Both halves - retrying transients, and giving up on an outage - were confirmed
by deliberately breaking each and watching the right tests fail.

### D-136 - The account is shared, and half the adapter did not know it

You mentioned, after the demo balance moved on its own, that a second robot is
trading the same MT5 account. That is not an unusual setup and the adapter was
supposed to handle it: `MAGIC` exists for exactly this, the module docstring
says foreign orders stay foreign, and the test file's docstring says anything
without our magic number must not be adopted.

Two of the four read paths did not implement it.

| Read path | Filtered on `magic` |
|---|---|
| `open_orders` | yes |
| `executions` | yes |
| `positions` | **no** |
| `opposing_tickets` | **no** |

`positions_get(symbol=...)` returns every ticket on that symbol regardless of
who opened it, so the other robot's XAUUSD exposure was netted into ours and
returned as our book.

**Why this is a trading fault and not a reporting one.** The strategy reads its
position from the context (D-041). A foreign long makes it believe it is
already long: it declines its own entry, and on the opposing breakout it sends
a close for a position it does not own - closing or reversing the other
robot's trade. A foreign ticket mirroring ours nets to zero, the strategy reads
flat, and its next entry doubles a position it has misread. Kill switch flatten
would do the same, deliberately and all at once.

Nothing was at risk when this was found: the loop runs on the paper broker and
`Mt5Broker.place` has still never been called. But `--broker live` exists as a
flag, and this would have been waiting behind it.

**The fix** is one shared `_our_tickets` helper both paths now go through, so
the filter cannot be present in one and missing in the other again.

**`account_health` deliberately still counts every ticket**, and unfiltered by
symbol too. That snapshot describes the *account* - the balance, equity and
margin beside it are account-wide, and a foreign robot's tickets consume the
same margin ours do. Filtering there would report a margin level that no set of
positions explains. The distinction is now commented at both sites.

**Testing note.** Applying the filter broke four existing netting tests, which
was the fix working: their fakes never stamped `magic`, so a `Row` without one
defaults to 0 and is correctly foreign. They now stamp `MAGIC` explicitly,
which is what they always meant. Five new tests cover the shared account -
an unstamped manual trade, a different EA's magic, mixed netting, the
mirrored hedge, and opposing-ticket counting - and all five fail when the
filter is reverted.

**Still true regardless.** Magic separates the books; it does not separate the
*money*. The other robot's positions draw on the same margin and the same
balance, so a margin call it causes closes our positions too. That is a
property of sharing an account, not something an adapter can fix.

### D-137 - `for result in loop.run(...)` is not a stream, and the heartbeat depended on it

Checking the loop after the D-136 fix, `/health` reported:

    status : stale
    engine : running
    warnings: the engine last reported 653s ago but still says it is running

The loop was fine. Process alive, eleven minutes into a two-hour run. The
staleness warning added in D-135 was correct about the evidence and wrong about
the conclusion, because the evidence was never being written.

`LiveLoop.run` returns `list[PassResult]`. It is not a generator. The MT5
command consumed it as `for result in run.loop.run(...)`, which is valid Python
that does the exact opposite of what it looks like: the body does not run per
pass, it runs after **every** pass has finished. Inside that body were the
terminal echo, the alert dispatch, the heartbeat write, the broker ledger save
and the account snapshot.

So a 120-pass run at a 60s cadence wrote its first heartbeat two hours in,
saved no ledger until then, and sent no alert about anything that happened in
between. The single heartbeat in the state file was the one written at startup.

**This is the more serious half of D-135 failing.** That entry added staleness
detection so a dead loop could not sit there claiming to be running. It works.
But the same run also has to *emit* a heartbeat for it to mean anything, and
the emitter was batched to the end - so a live loop and a dead one looked
identical for the whole session, which is precisely the state D-135 set out to
make distinguishable.

**The fix** uses `on_pass`, which already existed for this and is called at the
pass boundary. The per-pass body moved into `handle_pass` and the run became a
statement. Nothing in `loop.py` changed; the hook was there and unused.

**Testing.** Two levels, because neither alone is honest:

- `test_on_pass_runs_during_the_run_not_after_it` drives a real loop with a
  recording `sleep` and asserts the passes and waits alternate. It fails when
  `on_pass` is moved to the end of `run`, which is the sabotage that recreates
  the bug.
- The CLI's use of the hook cannot be reached without a live terminal, so it is
  asserted structurally, in the manner of `TestTheCliSourceStaysAscii`: the
  command passes `on_pass=`, and does not iterate the finished list.

**Worth stating plainly**: the batching also meant `broker.save(ledger_path)`
was deferred for the whole run on the `--broker live` path. A crash mid-run
would have left the ledger empty. The order journal still carries the intent,
which is what reconcile reads first, so this was recoverable rather than
silent - but only because that ordering was already deliberate (D-127).

### D-138 - A scalper, built in the regime the measurements say loses money

You asked for an intraday scalping expert. `GoldIntradayScalper.mq5`, magic
`20260903`. It is the first expert here that is **not** a port: nothing in
`algo/strategy/` corresponds to it, so there is no backtest it must agree with
and none standing behind it either.

**The tension, stated before the design.** D-124's common-window numbers are
the reason this entry is not just a feature note:

| | M15 | M30 | H1 |
|---|---|---|---|
| MACD, 0.5% stop | -$77,668 | $77,836 | $132,919 |
| Breakout(20), 0.5% stop | -$14,779 | $52,467 | $136,477 |

The stated cause was never the signal. Trade count roughly halves per step to a
slower interval while the $0.29 round-trip spread is charged *per round trip*,
so cost is the dominant term. A scalper trades **more** than the column that
lost money. I said so before writing it and I am recording it here rather than
letting the EA's own docstring be the only place it appears.

That is not an argument the thing cannot work. It is an argument that the only
part worth engineering carefully is the cost side, which is what drove every
decision below.

**The cost gate is mandatory and defaults ON.** `InpMinTpSpread` (4.0) refuses
any trade whose target does not clear the *live* spread by that multiple.
`GoldMacdCrossover` ships `InpMaxSpreadPoints` **off**, reasoned there as
"enabling it makes live diverge from the backtest in a way the backtest cannot
score." That reasoning is correct for a port and does not transfer: there is no
backtest here to diverge from, and D-124 says this guard is the whole game.
Setting it to 0 is allowed and logs a warning naming the M15 column.

It **rejects rather than widens**. Widening a target until it clears the spread
would quietly convert a scalp into a swing trade still carrying a scalp's stop,
which is the worst of both.

**A separate module, not a reuse of `ProtectiveExits.mqh`.** That file is
anchored in percent of price because the Python it ports is, and its 0.5%
default is about $23 on XAUUSD near 4,600 - a swing stop. A scalp cannot
express its risk in that unit. `ScalpFilters.mqh` is ATR-relative throughout.

This is a deliberate exception to the "one shared, tested piece rather than two
copies that could drift" rule the README states. The two modules are not two
copies of one idea; they are two different units of risk, and merging them
would produce a module with a mode switch and two half-tested paths. They are
documented as non-interchangeable at the top of both.

**`BuildBracket` applies its three constraints in a fixed order** - broker
`SYMBOL_TRADE_STOPS_LEVEL`, then `InpMinStopPoints`, then the cost gate. Order
matters: the first two *widen* the stop and therefore the target, so the gate
must test the final target. Testing the requested one would pass trades whose
real target had already moved.

**The bracket goes out with the order.** `OpenBracket()` attaches SL and TP to
the same request rather than `Open()` then `ApplyStop()`. Between those two
calls a position exists unprotected for a server round trip - which is exactly
when the fast move that motivated the entry is still moving. A $23 swing stop
tolerates that window; a stop a few ATR-tenths wide does not. The broker takes
the whole bracket or rejects the whole order, and SL/TP - not the bar-close
logic - becomes the primary exit.

**A bug this created, found before it shipped.** `ApplyStop` clamps a level to
the broker's stops band, and the clamp always pushes the level *away* from
price. `TightenStop` was checking monotonicity first and letting `ApplyStop`
clamp afterwards, so a trail level inside the band would be tested as a
tightening and then applied as a **loosening** - the one direction a trail must
never move. The clamp now happens in `TightenStop` before the test, so the
check and the order agree; the clamp inside `ApplyStop` is then a no-op. Narrow
window, real fault, and it only exists because the trail is broker-side here
rather than modelled as it is in the Python.

**The day is bounded, not just the trade.** Realised loss limit, profit target
and trade cap. Scalpers rarely die on one bad trade; they die on forty round
trips through a flat market, each paying the spread. `InpMinSepAtr` (0.25)
refuses to trade while the EMAs are tangled, which is the state that generates
those forty trades, and the governors bound the day if it happens anyway.

All three are **recomputed from deal history every bar**, never accumulated in
a variable - the same reasoning that makes the trail replayed rather than
persisted. An expert is reloaded on recompile, on a chart change, on a terminal
restart. A counter in memory would reset mid-day and hand back a budget already
spent, which is the most dangerous way for this particular guard to fail.
Commission is summed on entry deals too, because commission paid on the way in
is money gone whether or not the position has closed; omitting it would
understate the day's loss in the optimistic direction.

**Sizing fails closed.** `LotsForRisk` converts through
`SYMBOL_TRADE_TICK_VALUE`/`TICK_SIZE` rather than contract size - tick value is
already in the account currency, whereas the contract-size route is correct
only while quote and account currency coincide. If the symbol reports nothing
usable it returns 0 and the entry is **skipped**. There is deliberately no
fallback lot size: a sizing failure must not become a position. Where the
computed size falls below `volume_min` the trade is taken at the minimum and
the log says plainly that it risks *more* than asked.

**`Trader.mqh` changed, so the other two experts were recompiled.** The two
additions are purely additive and both existing experts still build 0 errors,
0 warnings; all three `.ex5` were regenerated, since the two existing binaries
went stale the moment the shared include changed.

**It does not hand-roll its indicators**, and that is not inconsistent with
`GoldMacdCrossover` avoiding `iMACD()`. That expert avoids the built-in because
it must agree bar-for-bar with the EMA seeding in `algo/pricing/indicators.py`.
Nothing here has a Python counterpart to agree with, so the terminal's own
indicators are the right choice - they are what the Tester and the chart show.

**What is not settled.** `InpDailyLossLimit` ships at 0 (off) because inventing
a currency amount for a live account is not mine to do; it needs setting before
the expert runs. Nothing here has been measured - not the signal, not the
defaults, not the gate's threshold. D-131's finding was that parameter
optimisation on this data fits noise, and a scalper has more parameters, not
fewer, so tuning these on one window would be that finding repeated rather than
learned from. Real-tick Tester first, and specifically **not** 1-minute OHLC
modelling: interpolation inside the bar cannot say whether a bracket this
narrow was hit stop-first or target-first, which is the entire question.

### D-139 - The scalper now trades 1,261 times instead of 2, and loses money doing it

You asked it to trade more, and to work on M1. Both done. The result is
negative and this entry records it rather than tuning until it is not.

**Why it only traded twice.** The first M5 run took two trades in three months.
The cause was not the gates - it was a four-way conjunction resting on a
*single-bar coincidence*: the RSI dip on shift 2 and the recovery on shift 1,
adjacent bars, plus the regime filter and the close confirm on top.

`InpPullbackBars` generalises it to what the idea always described - price
dipped RECENTLY and has now resumed. The dip may sit anywhere in the last N
closed bars; the recovery is still required on the bar that just closed, and
both are still required in that order. **`InpPullbackBars = 1` reproduces the
old rule exactly**, which is what makes this a generalisation rather than a
different signal wearing the same name.

**Telemetry, because "nothing happened" had two meanings.** The M5 run could
not distinguish a signal that never fired from a signal refused at a gate -
both leave no trace, and they have opposite fixes. `GateStats` counts every
fired signal and every refusal by reason, and prints at shutdown. It answered
the question immediately on the next run and should have existed from the
start.

**Measured, M1, XAUUSD, 2026.06.01-08.31, $10,000, 0.5% risk:**

| | breakeven+trail ON | OFF |
|---|---|---|
| Trades | 1,261 | 1,223 |
| Net | **-$2,775.13** | **-$2,641.42** |
| Profit factor | 0.91 | 0.92 |
| Expected payoff | -$2.20 | -$2.16 |
| Win rate | 47.82% | 42.4% |
| Avg win / avg loss | 45.26 / -45.69 = **0.99** | 58.68 / -46.86 = **1.25** |
| Max drawdown | 31.02% | 28.96% |

Gate telemetry for the first column: 3,451 signals, 2,086 blocked by the
session window, 104 by cooldown, **0 by the cost gate and 0 by the spread
guard**, 1,261 taken.

**A diagnosis I got wrong, and the run that corrected it.** Seeing avg win
0.99x avg loss against a bracket designed at 1.5R, I concluded the breakeven
and the trail were cutting winners short and destroying the reward side. The
control run says otherwise: turning both off *did* restore the ratio to 1.25,
but the win rate fell 47.8% -> 42.4% and the two effects cancelled almost
exactly. Profit factor moved 0.91 -> 0.92. **The management is roughly neutral,
not harmful** - it trades win rate for reward at close to fair odds.

**What is actually wrong is the thing D-124 said would be wrong.** With
management off the realised reward:risk is 1.25 against a designed 1.5. That
0.25 shortfall is the cost drag, and it is decisive: at a 42.4% win rate
break-even needs 1.358, and the *designed* 1.5 would give a profit factor near
1.10. Costs turn a marginally-positive system into a 0.92 one. That is D-124's
"trade count roughly halves per step while the spread is charged per round
trip", reproduced at the frequency this expert was asked to run at.

**The cost gate did not save it, and that is not a gate failure.** It refused
nothing because `InpMinStopPoints = 80` floors the stop at 0.80, so the target
is 1.20 against a ~0.22 spread - comfortably over the 3x threshold on every
trade. The gate catches trades whose target is small relative to spread; it
cannot catch a system whose edge is merely *thinner* than its costs. Those are
different failures and only the first has a gate.

**Not tuned further, deliberately.** The obvious move is to sweep the
thresholds until the curve turns up. D-131's finding was that parameter
optimisation on this data fits noise, and this is one instrument on one
90-day window with the trending period included. A profit factor of 0.91
across 1,261 trades is not a near miss to be optimised across; it is the
measurement saying this signal does not clear costs at M1 frequency.

**An operational trap worth its own paragraph.** The first M1 run reported
`EMA(21/50) + RSI(14) | cost gate 4.0x` - the *old* defaults - because the
Strategy Tester silently reuses the last input set it saved for an expert
(`MQL5\Profiles\Tester\<Expert>.set`) in preference to the compiled defaults.
Newly added inputs take their compiled value while every pre-existing one keeps
the stale saved value, so the run is a hybrid that matches neither the source
nor any set file. Changing a default in `.mq5` and re-running proves nothing
until that cache is cleared. The expert prints its whole configuration on init
for exactly this reason; that banner is what caught it.

### D-140 - Screened all 76 installed experts; ours is dead, and the one survivor rests on an assumption this broker cannot test

You asked for every installed expert to be backtested, not just ours. 76 found,
75 completed (one licensed EA hung past its cap), on identical settings: XAUUSD
M1, 2026.06.01-08.31, real-tick model, $10,000, 1:100, each on its own compiled
defaults.

**48 traded, 16 took no trades, 11 could not initialise** (licence checks -
`MHD Scalper Pro 9.5`, `Piner EA`, `Grid Scalper MA`, `VALHALLA EDGE`,
`TwisterPro Scalper`). **Only 13 of 48 cleared profit factor 1.0.**

**Our scalper does not work, and this is the entry that says so.** Three
windows, ~5,900 trades, eighteen months:

| Window | Trades | PF | Net | Max DD |
|---|---|---|---|---|
| 2026.06-08 (in-sample) | 1,261 | 0.91 | -$2,775 | 31.0% |
| 2026.01-05 (out-of-sample) | 1,898 | **0.87** | -$5,007 | 51.4% |
| 2025.06-12 (out-of-sample) | 2,739 | **0.73** | -$8,748 | **88.5%** |

Below break-even in every window, and *worse* out-of-sample. A signal that loses
everywhere it is shown does not have an edge hidden behind a parameter, and
D-131 already established that sweeping thresholds on this data fits noise.
**Stop work on GoldIntradayScalper as a strategy.** The plumbing around it -
`ScalpFilters`, the attached bracket, the daily governors, the gate telemetry -
is sound and reusable. The entry rule is not.

Its one virtue is diagnostic: PF moved only 0.91 -> 0.87 -> 0.73 across regimes
*because* it realises every loss through a hard stop, and equity drawdown
tracked balance drawdown to within 0.4% in all three runs. The numbers are
stable because they are honest, which is more than most of the field managed.

**The survivor.** `Adaptive Gold Scalper v2.3` held up on windows it was never
selected on - PF 17.75 (221 trades) and 5.44 (263 trades) against 25.81
in-sample, strike rate steady near 91%, max loss pinned at -$40.30/-$40.40/
-$40.70 every time. That constant is its 400-point stop on a fixed 0.10 lot.
Its M5 run returned byte-identical results to M1: it is a price-level strategy
(`_order_price_gap`, `_point_shift` are distances, not bar counts) and ignores
the chart timeframe entirely.

Economics worked back from the report: average win ~$53, average loss ~$32,
strike rate 91%. Its `_take_profit=10000` ($100) is essentially never reached -
the $0.20 trailing stop closes almost everything. So the real claim is **~1.3:1
reward at a 91% strike rate**, which has very little margin.

**And that is the assumption this broker cannot test.** Real ticks begin
2026.08.26; the account retains about five days. Every run above used ticks
synthesized from M1 bars. This EA fills through pending stop orders resting ~$50
from price - the most slippage-sensitive order type there is. The tester fills a
resting order at exactly its price; a spike violent enough to travel $50 does
not. At 1.3:1 and 91%, a few points of real slippage per fill inverts the
expectancy. No historical run here can settle it; **forward-testing on demo is
the only remaining discriminator.**

**Two things I asserted and had to withdraw.**

*Max/min lot ratio is not a martingale tell.* I introduced it as the cleanest
one available without source. Our own EA scores 14.7 on it and contains no
martingale - the spread is ATR-based risk sizing choosing larger lots when stops
are tighter. It flags *variable sizing*, nothing more.

*`Adaptive Gold Scalper` does not hide its losses.* Seeing a 100% win rate on
the real-tick window, I concluded it had an exit that never realises a loss. The
balance-versus-equity drawdown decomposition refutes it: 0.38% against 0.58%.
That decomposition, not the win rate, is the test that settles the question, and
it cleanly separates the field - `Quantum Athena` 1.17% balance against 11.16%
equity (9.5x), `ExpertMAPSAR` 5.0x, `MoonDog` 2.1x all *do* carry unrealised
losses.

**Four harness traps, each of which silently produced plausible wrong output.**

1. **A UTF-8 BOM voids the entire config.** PowerShell 5.1's `Set-Content
   -Encoding UTF8` writes one; MT5 then reads `<BOM>[Tester]`, fails to match
   the section header, and ignores every setting - while still logging
   *"successfully initialized from start config"* and opening normally. Two full
   batch attempts tested nothing at all before this was found.
2. **The tester prefers its saved input cache to compiled defaults** (D-139), so
   `MQL5\Profiles\Tester\<Expert>.set` must be cleared per EA or the run
   silently uses stale values.
3. **`ShutdownTerminal=1` is not reliable here.** Waiting on process exit made
   every EA burn its full timeout.
4. **The batch left a 9.19 GB tester log** (martingale EAs log every tick), and
   reading it to detect completion threw `OutOfMemoryException` - which a
   `try/catch` converted into "0 finishes", so the check could never succeed.
   Disk hit 97% full. Completion is now signalled by the report file appearing
   and its size settling: one `stat()`, no log read.

The shape shared by (1) and (4) is worth naming: **an error path that returns a
plausible value instead of failing loudly.** Both times the instrument broke
silently and the silence read as progress - the same failure the gate telemetry
in D-139 exists to prevent, repeated in the tooling that measures it.

### D-141 - The gold EAs do not transfer, and two of them were never trading the chart symbol

Follow-on to D-140. You wanted `Adaptive Gold Scalper` on FixedVol100, then the
top 10 on FixedVol100 and BTCUSD. Both symbols carry **real ticks from
2026.06.01** - XAUUSD retains only ~5 days - so unlike everything in D-140,
these rest on ticks that happened.

**The broker constraints are the whole story, and they differ enormously.**

| | XAUUSD | FixedVol100 | BTCUSD |
|---|---|---|---|
| price | 4,326 | 4,911 | 76,640 |
| spread (price) | 0.28 | 0.88 | **16.95** |
| spread / price | 0.0065% | 0.018% | **0.022%** |
| `STOPS_LEVEL` | 0.20 | **7.123** | 0 |
| $ per 1.0 price at min lot | 1.00 | 0.10 | 0.01 |

**AGS is not symbol-locked; it is constraint-locked.** On FixedVol100 with its
own defaults it sent 1,027 orders and had **2,054 rejected as `Invalid stops`,
filling none**. Its stop sits 4.000 from entry; the broker minimum there is
7.123. Scaling the distances past that (`_stop_loss` 400->1000,
`_trailing_points` 20->800, `_take_profit` 10000->25000, `_order_price_gap`
5000->12500) makes it trade normally: 265 trades, **profit factor 0.61**, win
rate 43.0% against 94.9% on gold, expected payoff -$0.23.

**And it cannot be fixed by tuning, for a structural reason.** AGS's profit
engine is its trailing stop - the $100 take-profit is essentially never reached,
the $0.20 trail closes almost everything. On XAUUSD that trail is 1x the broker
minimum, ~5% of the stop distance. On FixedVol100 the tightest *legal* trail is
7.123, which is **71% of a 10.000 stop**. The mechanism that makes this EA money
is unavailable on that instrument at any parameter setting.

**Top 10 by gold profit factor, on both symbols: none transfer.** Genuine
results only (see the invalidated rows below): Quantum Titan 0.78 and
ExpertMAPSAR 3.85 on FixedVol100; AGS 0.39, Smart Gold Hunter 0.15,
ExpertMAPSAR 0.18 on BTCUSD. Five EAs produce no signal at all off gold.
ExpertMAPSAR's 3.85 is the *stock MetaQuotes sample* over 59 trades, and it
scored 1.69 on gold and 0.20 on the real-tick gold window - noise, not edge.
Quantum Titan's BTCUSD 2.65 is unusable: **1,332 of ~1,400 orders were
rejected**, so the 68 fills are a biased survivor sample.

**Two EAs ignore the chart symbol entirely, which invalidates their rows.**

- `Iron Stops v1.18` traded **XAUUSD** when attached to FixedVol100 *and* when
  attached to BTCUSD. Net $6,817.04 and $6,816.64 against $6,811.41 on gold -
  the same 49 trades every time.
- `Range Breakout v5.20` traded a **XAUUSD / USDJPY / BTCUSD** basket on both
  charts.

Their apparent cross-symbol profit factors are the gold result re-run. **Any
EA screen that does not verify which symbol was actually dealt will silently
report this as a transferable edge.** The check is cheap: read the deals
table's Symbol column, not the chart the test was configured with.

**A third instance of the same measurement failure.** The harness was built to
distinguish `rejected` from `no signal` - precisely the distinction that matters
here - and it reported 0 rejects for the run that had 2,054. The tester log is
UTF-16; seeking to a byte offset past the BOM and decoding from there yields
garbage matching no pattern. Correct counts came from re-reading the file whole
with encoding detection and scoping by run block.

That is the third time in this exercise: the UTF-8 BOM voiding the tester config
(D-140), an `OutOfMemoryException` swallowed by a `catch` and returned as "not
finished", and now this. **Every one was an error path returning a plausible
value instead of failing loudly**, and every one initially read as a result
rather than a fault. Where a measurement can fail silently, it needs a positive
signal that it ran - which is the same argument the gate telemetry in D-139
already made, now paid for three more times.

### D-142 - Adding a stop to ExpertMAPSAR fixed the risk and revealed there was no edge

You asked for a stop loss and take profit on the MetaQuotes `ExpertMAPSAR`
sample. Added as `mt5/Experts/Samples/ExpertMaPsarBracket.mq5` (magic 14599, a
new file - the stock sample stays as shipped). The bracket works. The strategy
does not.

**The sample has no stop and no target, and nothing in its `.mq5` says so.**
`CExpertSignal`'s constructor sets `m_stop_level(0.0)`/`m_take_level(0.0)`, and
`OpenLongParams` does `sl = (m_stop_level==0.0) ? 0.0 : ...`, so every order
goes out with `sl=0 tp=0`. Worse, `CTrailingPSAR::CheckTrailingStopLong` uses
`base = (pos_sl==0.0) ? PriceOpen() : pos_sl` - with no initial stop, `base` IS
the entry, so the trail can only move the stop *above entry* and a position
going straight against you is unprotected until the opposite signal.

**Pattern 0 makes a bracketed version churn.** `CSignalMA` votes with four
models; pattern 0 - "price is on the necessary side of the indicator" - is a
persistent STATE whose default weight of 80 clears `threshold_open` 50 alone.
Unbracketed that is harmless (one position, held). Bracketed it is not: stop
out, state still true, re-enter next bar. Measured on XAUUSD M1 2026.06-08:

| | Trades | PF | Net | bal DD | eq DD | max loss |
|---|---|---|---|---|---|---|
| stock, no bracket | 51 | 1.69 | +117.87 | 1.17% | 5.82% | -119.93 |
| bracket, pattern 0 on | 4,936 | 0.88 | -1,173.84 | 11.94% | 11.96% | -38.26 |
| bracket, pattern 0 off | 1,405 | **1.06** | +142.41 | 1.15% | 1.17% | -5.95 |

Patterns 1/2/3 are crossings and piercings - events - so disabling pattern 0
keeps the bracket without the churn. That was predicted from reading the source
before running it, not found by sweeping.

**And it does not survive out-of-sample.** The pattern_0 choice was still made
after seeing the window above, so the window above is in-sample:

| bracket, pattern 0 off | Trades | PF | Net |
|---|---|---|---|
| 2026.06-08 (in-sample) | 1,405 | 1.06 | +142.41 |
| 2026.01-05 | 554 | **0.77** | -296.36 |
| 2025.06-12 | 717 | **0.81** | -157.16 |

**The 1.06 was the window doing the work.** No edge. Same conclusion as D-140
reached for our own scalper, by the same route.

**The stock sample is not merely edgeless, it is dangerous.** PF 1.69, then
11.06, then 0.10 across the three windows on 51/56/33 trades, and in 2025H2 a
**single trade lost $1,013 on a $10,000 account**. Its equity drawdown runs
12-18x its balance drawdown in two of three windows (0.69% balance against
12.60% equity in 2026H1). That is the unrealised-loss signature D-141 used to
disqualify `Quantum Athena` and `MoonDog`, and it is what produces a 94% win
rate: it does not close losers. **Its own largest loss exceeds its entire net
profit in the one window where it made money.**

**What the bracket actually bought, since the strategy is a write-off:**

| | stock | bracketed |
|---|---|---|
| worst single loss | -$1,013.11 | -$37.87 |
| equity vs balance DD | up to 18x gap | 1.01x |

A 27x smaller worst case and no hidden unrealised risk. A stop cannot
manufacture edge; it can stop a bad strategy from being catastrophic, and it
makes the absence of edge *visible* instead of hidden behind a win rate.

**Gap risk, measured rather than assumed.** The bracketed runs' worst losses
were -$37.87 and -$15.04 against a $4.00 stop at 0.01 lots - price jumping the
stop over a weekend or on a spike, **4-9x the intended risk when it happens**.
Rare (2 of 3,286 losing exits in-sample) but real, and the same exposure D-141
flagged as AGS's unverifiable assumption.

**One harness bug worth recording**, since it wasted a run and is easy to
repeat: clearing the tester cache with `Get-ChildItem -Filter "$leaf*"` is
case-insensitive AND prefix-based, so clearing `ExpertMAPSAR*` also deleted
`ExpertMaPsarBracket.set`. Matching `"$leaf.*"` - with the dot - keeps them
apart. Related: a PowerShell function that both `Write-Output`s and returns a
value folds the output INTO the return value, so the confirmation line never
printed and the run's inputs had to be verified from the `.set` afterwards.

**Asked to make it tradable; it cannot be, and this is where that stops.**
Two changes were added, both principled rather than searched:

- **The cost gates** - spread cap, target-vs-spread ratio, session window,
  cooldown, daily loss limit and trade cap - reusing `ScalpFilters.mqh` rather
  than a second copy. All default to OFF so the measured baseline above is
  unchanged, and the expert prints which gates are live on init, because a
  filter that is silently off looks exactly like one that never triggers.
- **The timeframe**, tested as a hypothesis. D-124 established slower-is-better
  on different strategies before this expert existed - trade count roughly
  halves per step while spread is charged per round trip - so M15 is a
  prediction, not a knob.

| XAUUSD | M1 | M15 |
|---|---|---|
| 2026.06-08 | 1.06 | 0.90 |
| 2026.01-05 | 0.77 | **0.58** |
| 2025.06-12 | 0.81 | 1.04 |

**Six window-configurations, scattered around break-even with no consistency.**
The timeframe hypothesis was a fair test and it failed. Trying M30, H1, H4
until one came back above 1.0 would be D-131's finding repeated deliberately,
so it was not done. `CSignalMA` on XAUUSD has no edge this repo can find, and
no bracket, gate or interval fixes that.

**What the work is actually worth keeping for.** The signal is a write-off; the
scaffolding is not. The bracket, the `STOPS_LEVEL` pre-flight that refuses to
start rather than send orders that will all be rejected (D-141), the gates, and
the init banner that states its own configuration are all signal-agnostic and
reusable. That is the same split D-140 reached for `GoldIntradayScalper`: the
machinery survived, the entry rule did not. Two independent strategies now,
same verdict, same cause.

### D-143 - The grid made it five times worse while raising the win rate

You asked for a scale-in ladder on GoldTrendlineBreakout: add on the same side
at every $5 of loss, up to 5 positions, lot x1.25 each add, close the basket at
$2 combined profit. Built as `dda3486` and measured. It is now DISABLED, and
this records why.

**Controlled before/after.** Same expert, same symbols, same window
(2026.06.01-08.31), same M15, real ticks, $10,000. The only change is the grid:

| | trades | PF | net | max DD | win rate |
|---|---|---|---|---|---|
| FixedVol100, single position | 1,166 | 0.83 | -$1,385 | 14.57% | 39.5% |
| FixedVol100, **with grid** | 3,246 | **0.50** | **-$7,191** | **73.65%** | **55.7%** |
| BTCUSD, single position | 708 | 0.67 | -$1,460 | 14.93% | 41.4% |
| BTCUSD, **with grid** | 2,509 | **0.39** | **-$6,527** | **65.40%** | 48.3% |

**Roughly 5x the loss and a 15% drawdown turned into 65-74%.**

**The win rate went UP while the account did far worse** - 39.5% to 55.7% on
FixedVol100. That is the whole grid trade in one number: the adds pull the
average entry toward price, so baskets resolve profitably more often, and the
ones that do not resolve cost several positions instead of one. It is the same
signature D-141 used to disqualify `Quantum Athena` and `MoonDog`, reproduced
here deliberately and measured rather than inferred.

**A behaviour worth recording.** The tester log shows `scale-in 5 of 5` firing
three times inside one basket. The cap is on CONCURRENT positions, not total
adds: when one of the five closes on its own bracket a slot frees and the ladder
refills it. So the init banner's "WORST CASE ... about 83.34" is the worst case
at any INSTANT, not the most a single basket can lose over its life. The banner
wording is accurate but easy to read as the stronger claim.

**Timeframe, measured on the same runs.** Before the grid existed:

| | trades | PF | net | max DD |
|---|---|---|---|---|
| FixedVol100 M1 | 9,789 | 0.78 | **-$10,004** | **100.05%** |
| FixedVol100 M15 | 1,166 | 0.83 | -$1,385 | 14.57% |
| BTCUSD M15 | 708 | 0.67 | -$1,460 | 14.93% |
| BTCUSD M1 | 0 | - | - | - |

**M1 lost the entire account** - 100.05% drawdown on a $10,000 deposit across
9,789 trades in three months. Both charts were moved to M15 on that basis.
D-124's finding again, now on two more instruments.

BTCUSD M1 returning zero trades is UNEXPLAINED. The report is genuine (22 KB
against megabytes for the others) and the run completed; I did not determine the
cause and am not claiming one.

**What is running now**, after disabling: M15, bracket attached at entry at
1.5:1, money trail arming at $5, salvage at $5 adverse -> exit at $2, scale-in
OFF, basket exit OFF. That is within a salvage rule of the configuration that
measured 0.83 and 0.67 - still losing, but survivable rather than
account-ending.

**The strategy underneath is unchanged and still has no edge.** PF 0.07 on M1
gold (D-140), 0.78-0.83 on FixedVol100, 0.67 on BTCUSD. Every exit mechanism
added over this session - bracket, points, money, trail, salvage, grid - changes
the SHAPE of the outcome distribution. None of them changed the sign. A grid on
a losing signal wins more often and loses more.

---

## Judgement calls made because the brief was silent or self-conflicting

| # | Call | Question |
|---|---|---|
| 1 | §6's spread/swap/rollover model replaced wholesale with an MCX commodity-options cost stack | plan §2 |
| 2 | §8's stop-distance sizing formula not used — you chose fixed lots; implied risk still reported | answered |
| 3 | No entry filter and no within-cycle re-entry in v1 | Q3, Q5 |
| 4 | Greeks float, everything monetary Decimal | D-004 |
| 5 | Backtest evaluates stops at bar granularity and is therefore optimistic vs live | Q15 |
| 6 | Mandatory pre-expiry exit overrides any strategy intent | D-016, Q4 |
| 7 | Build order changed — M1.5 inserted before M2 | D-019 |

### D-144 - Gold Sniping ignores the chart timeframe entirely, and its 88% win rate is a martingale

You asked to test the Market EA `Gold Sniping` over two months on M1, M5, M15,
M30 and H1. All five ran: XAUUSD, 2026.07.01-08.31, every tick based on real
ticks, $10,000 deposit, five separately written reports.

**Every timeframe returned the identical result, to the cent.**

| TF | trades | net | PF | win rate | balance DD | equity DD |
|---|---|---|---|---|---|---|
| M1  | 4,424 | +$3,457.59 | 1.07 | 87.73% | 29.11% | 43.34% |
| M5  | 4,424 | +$3,457.59 | 1.07 | 87.73% | 29.11% | 43.34% |
| M15 | 4,424 | +$3,457.59 | 1.07 | 87.73% | 29.11% | 43.34% |
| M30 | 4,424 | +$3,457.59 | 1.07 | 87.73% | 29.11% | 43.34% |
| H1  | 4,424 | +$3,457.59 | 1.07 | 87.73% | 29.11% | 43.34% |

Not a caching artefact: each report was deleted before its run, each was written
at a distinct time, and the tester journal shows five separate `automatic
testing started` lines. **The EA never reads the chart period.** It is driven by
price and tick events. There is no timeframe to optimise, and any claim of a
recommended timeframe for it is empty. This is the same class of finding as
D-141, where two gold EAs ignored the chart *symbol*.

**The win rate is structural, not skill.** From the tester log: baskets close on
about $1.00 of profit and reopen with `Rest period = 0 minutes`, and lot size
escalates on adverse moves (0.01 -> 0.02 -> ...). The statistics agree:

- 87.73% of trades win, yet PF is only **1.07** - gross profit $54,891 against
  gross loss $51,434. The 12% of losers give back nearly everything.
- Largest win **+$506** vs largest loss **-$734**.
- Max consecutive losses: **7, costing -$4,088** - 41% of the account in one run.
- Equity DD (43.34%) exceeds balance DD (29.11%), the martingale signature: it
  carries unrealised losses rather than taking them.

+34.6% in two months, at a 43% equity drawdown, with the drawdown bounded only
by the worst gold trend that happened to fall inside the window. For a
martingale the entire risk lives in the tail, and **a two-month sample cannot
contain it.** These numbers describe a survivor, not an edge.

**Two limits on the measurement itself.** This broker retains only ~5 days of
real XAUUSD ticks, so despite `Model=4` most of the window is ticks synthesised
from M1 bars - which flatters basket EAs specifically, by understating the
intrabar spikes that break baskets. The report does not say so. And two months
is too short a window to locate the failure point of a martingale.

**Why the first attempt reported "no report" on three timeframes.** The tester
log directory had grown to **10.2 GB**, throttling each run past the 420 s cap.
I labelled that failure "licence?" in the moment - a guess, and wrong. Cleared
the logs, raised the cap, and every run completed in 6.4-6.8 minutes. Same
lesson as D-140: a run that dies for an environmental reason and a run with
nothing to report look identical from the outside unless the cause is read
rather than assumed. The re-run script now captures the tester log tail on
failure so there is something to read.

### D-145 - Gold Sniping destroys the account in eight weeks; the two-month profit was survivorship

Following D-144, you asked for the 2024-2026 run - the window the two-month
test could not reach. XAUUSD, 2024.01.01-2026.08.31 requested, real-tick model,
$10,000, M15 and M1.

**It never reached March 2024.** Last deal 2024.02.26. Balance drawdown 101.35%
means the account went through zero: a $10,000 deposit lost $10,145.03. The run
finished in 2.4 minutes rather than the expected ~100 because after February
2024 there was no money left to trade; the remaining 30 months are empty.

| | 2026.07-08 (D-144) | 2024.01-2026.08 (requested) |
|---|---|---|
| last deal | 2026.08.28 | **2024.02.26** |
| trades | 4,424 | 340 |
| net | **+$3,457.59** | **-$10,145.03** |
| profit factor | 1.07 | **0.49** |
| expected payoff | +0.78 | **-29.84** |
| win rate | 87.73% | 62.65% |
| balance DD | 29.11% | **101.35%** |
| equity DD | 43.34% | **101.43%** |
| max consecutive losses | 7 (-$4,088) | 10 (-$1,338) |

M1 returned the identical figure again, so the timeframe-independence of D-144
holds over this window too.

**The two-month result was a survivor, and this is what it was hiding.** Same
expert, same settings, same broker - only the window moved. +34.6% over two
months was not an edge; it was one calm stretch that happened to contain no gold
trend large enough to break a basket. The win rate falling from 88% to 63% is
the mechanism becoming visible: baskets that normally close for ~$1 instead kept
escalating until margin ran out. For a martingale, the sample either contains
the tail event or it does not, and the reported statistics look excellent right
up until it does.

**The wipeout is still the optimistic case.** MT5 states the reason in the
report itself:

```
History Quality:  0% real ticks   (2024-2026)
History Quality:  8% real ticks   (2026-07/08)
```

`Model=4` cannot supply real ticks this broker never retained, so every price in
the 2024 window was interpolated from M1 bars. That understates precisely the
intrabar spikes that break baskets. The real outcome would have been worse and
sooner. Note also that the two-month test was only 8% real - the number that
looked like a result was itself almost entirely synthetic.

**Rule this establishes: never accept a martingale or grid EA's backtest from a
single window.** Balance drawdown near or above 100% is not a bad score on a
scale, it is account death, and it is the only statistic in the table that
matters. Cross-reference D-143, where adding a grid to our own expert turned a
15% drawdown into 65-74% while the win rate went up.

### D-146 - the Asia value-area fade loses, but its real problem is geometry, not the sample

You brought a gold strategy from a video: mark 05:30 IST, draw a volume profile
over 05:30-05:45, take the session's direction from the broker open, and fade
the first close back inside the value area toward the opposite edge. Claim
attached to it: 400-1000 pips a day.

Measured by `scripts/measure_asia_value_area_xauusd.py` on 100,001 real M1
XAUUSD bars, 2026-05-26 to 2026-09-04, 74 broker sessions, at the measured
spread profile (median $0.22, 199,170 ticks). One MT5 lot, flat by 12:00 UTC so
no swap applies. The source rule specifies no stop and no formula for
"direction", so two assumptions were taken and are named in the script's
docstring: **stop at the liquidity wick** the excursion printed, **bias** from
the 21:00 UTC session open to 00:00 UTC.

| | as described | control: bias inverted |
|---|---|---|
| trades | 55 (74% of sessions) | 59 (80%) |
| win rate | 45.45% | 45.76% |
| gross | -$2,775.00 | -$995.00 |
| spread paid | -$1,196.00 | -$1,284.00 |
| net | **-$3,971.00** | **-$2,279.00** |
| profit factor | 0.65 | 0.80 |
| expectancy | -$72.20 a trade | -$38.63 a trade |
| max drawdown | $5,138.00 | $4,908.50 |
| t of the mean | -1.22 | -0.51 |

**Neither result is distinguishable from zero, and saying otherwise would be
the D-144 mistake in reverse.** At n=55 and a per-trade standard deviation of
$438, |t| = 1.22. This run cannot prove the strategy loses money. What it can
do is bound the size of any edge: whatever is there is far too small to have
shown up in fourteen weeks, which is the only window this broker's M1 history
reaches - M1 stops at 2026-05-26, and a fifteen-minute profile cannot be built
from coarser bars.

**The finding that does not depend on the sample is the geometry.** Averaged
over the trades taken:

```
value area       $4.89 wide
target distance  $3.79      (the opposite edge, from the fill)
stop distance    $4.98      (the liquidity wick, from the fill)
reward : risk    0.76 : 1
```

The stop is **wider than the target**, every day, by construction. Fading a
liquidity sweep means entering after the excursion has already happened, so the
wick you must survive is further away than the edge you are aiming at. At 0.76:1
the break-even win rate is **56.8%**; the setup delivered 45.5%. That gap is not
a statistical accident to be resolved with more data - it is what the rule asks
for. Widening the stop makes the ratio worse; tightening it below the wick
throws away the only level the rule supplies.

**The direction filter carries no visible information.** Inverting the bias -
chasing instead of fading - produced 45.76% wins against 45.45%, and lost
somewhat less. The part of the rule presented as the edge ("pehle yeh clear ho
jayega") does not separate the two populations at all in this window. Both
readings sit inside the same noise, which is itself the point: a filter that
cannot be told apart from its own inverse is not yet evidence of anything.

**On the pips claim.** The average target was $3.79 away, which is 379 pips only
under the $0.01-a-pip convention; at $0.10 it is 38. The target was reached on
26 of 74 sessions - 35%. So "400-500 pips a day" requires the loosest pip
definition *and* that every session both signals and wins. The measured figure
for the same sessions is -$72 a trade.

**What this establishes.** Not "the strategy fails" - the sample cannot support
that. It establishes that the setup is structurally short of reward against
risk, that its direction filter is indistinguishable from its own inverse here,
and that **no EA should be written for it on this evidence.** Same ordering
lesson as D-141 and D-144: the cheap measurement comes before the expert, and
this one cost an afternoon instead of a build.

**One limit worth repeating.** The profile is built from MT5 `tick_volume` -
quote updates, not traded volume, because a CFD feed has no traded volume. Every
value area here is a distribution of quotes. It will not match a value area
drawn on a futures feed, and that is a different measurement wearing the same
name rather than an approximation that improves with more data.

### D-147 - no stop placement rescues the value-area fade; win rate and reward:risk move together

D-146 ended with one open question: its reward:risk of 0.76 came from the
liquidity-wick stop, and the obvious next move was to ask whether a different
stop fixes the geometry. Six stop rules, both bias directions, same 74 sessions,
same measured spread. The entry rule is untouched, so every variant takes the
same 55 trades (59 inverted) and only the exit level differs.

```
stop rule                      n   win%   b/e%    R:R       gross     spread         net     PF      t
wick (as described)           55   45.5   56.7   0.76  -$2,775.00 -$1,196.00  -$3,971.00   0.65  -1.22
wick, capped at 1.0x width    55   38.2   46.0   1.18  -$5,793.00 -$1,196.00  -$6,989.00   0.43  -2.39
fixed 0.25x width             55   38.2   38.0   1.63    -$993.00 -$1,196.00  -$2,189.00   0.74  -0.92
fixed 0.50x width             55   45.5   48.3   1.07  -$2,123.00 -$1,196.00  -$3,319.00   0.70  -1.08
fixed 0.75x width             55   50.9   55.7   0.79  -$3,450.50 -$1,196.00  -$4,646.50   0.66  -1.27
fixed 1.00x width             55   56.4   61.3   0.63  -$3,643.00 -$1,196.00  -$4,839.00   0.68  -1.13
```

Bias inverted, the same six rules run -$1,808 to -$8,000, profit factor 0.57 to
0.82. **Twelve cells, twelve losses, gross and net.**

**The see-saw is the finding.** `b/e%` is the win rate each geometry needs
simply to stand still, `risk / (risk + reward)` from the averages. Read the
sweep down the two win-rate columns:

```
stop            0.25x   0.50x   0.75x   1.00x   wick
achieved win%    38.2    45.5    50.9    56.4    45.5
required  b/e%   38.0    48.3    55.7    61.3    56.7
```

Widening the stop buys win rate at almost exactly the price the geometry charges
for it. **The achieved rate never gets above the required one.** That is not a
statement about this sample - it is what it looks like when the entry carries no
directional information: you can move where the loss is taken, and the market
gives back precisely the difference. There is no stop distance to be found by
searching, because the two numbers are the same number seen twice.

**The best row is flat before costs and loses because of spread.** `fixed 0.25x`
achieves 38.2% against a required 38.0% - a fifth of a point of margin, which is
another way of writing zero - and its gross is -$993 over 55 trades. Spread on
those trades is $1,196, at $22 a round trip (half of the measured $0.22 median,
100 ounces, two legs). So the least-bad geometry in the sweep is a coin flip
that pays a toll, which is the same shape D-123 found on M5 and D-142 on the
bracketed EA.

**Two things this run must not be read as saying.**

- **It does not select `fixed 0.25x`.** Choosing the best of six rules on 74
  sessions is curve-fitting with a table for a face. The sweep bounds the family
  - nothing in it clears break-even - and that is all it does.
- **`wick capped at 1.0x` is not "significantly worse".** Its |t| = 2.39 is the
  only cell past 2 out of twelve, which is roughly what twelve draws produce by
  chance. Reading it as a result would be the multiple-comparisons version of
  the D-144 error.

**Where this leaves the strategy.** D-146 said no EA on that evidence and left
the stop as the open door. The door is shut: the deficit is in the entry, not
in the exit. Anything further would have to change what the trade is - a
different bias formula, a different profile window, a filter on which days to
skip - and each of those is another parameter searched against the same 74
sessions, which is how D-143's grid came to look good on the win rate while
being five times worse. **Stopping here is the finding.**

### D-148 - a flat stop under the trail does exactly what D-127 predicted, and it is not enough

D-127 measured a 2%-armed, 0.5% trailing exit with the flat stop switched off,
found all six cells negative, and closed by naming the configuration it had not
run: the flat stop bounding the loser side the trail leaves open, *and* the
trail locking the winner side, together. That question is now answered. It is
no.

`scripts/measure_stop_trail_matrix_xauusd.py` runs six exit configurations x
three timeframes x two strategies from one code path - `run_cfd_backtest`,
which has taken `stop_loss_pct`, `trail_activation_pct` and `trail_pct` as
arguments since D-130, so no module constant is edited and all thirty-six cells
are directly comparable. Window 2024-07-24 to 2026-08-28 (2.09 yr), 100 engine
lots, measured Vantage costs.

```
MacdCrossover net              M15          M30           H1
  0.5% stop only           -$81,192     $81,739     $131,272
  2%/0.5% trail only      -$402,757   -$241,608    -$137,628
  0.5% stop + trail       -$206,552    -$76,947     -$21,616
  1.0% stop + trail       -$244,050   -$108,482    -$103,353

TrendlineBreakout net          M15          M30           H1
  0.5% stop only           -$17,974     $48,714     $133,039
  2%/0.5% trail only      -$165,841   -$147,443     -$52,759
  0.5% stop + trail       -$133,029    -$86,243     -$47,914
  1.0% stop + trail       -$102,872   -$147,574    -$136,622
```

**The stop does its job.** D-127's hypothesis was that the flat stop would bound
the losers the trail leaves uncovered, and it does: **ten of the twelve
stop-plus-trail cells beat their trail-only counterpart**, several by a lot -
MACD H1 goes from -$137,628 to -$21,616, MACD M30 from -$241,608 to -$76,947.
The two exceptions are both `TrendlineBreakout` with the 1.0% stop: M30 is
marginally worse (-$147,574 against -$147,443) and H1 substantially so
(-$136,622 against -$52,759), the same non-monotonicity in stop width D-126
recorded.

**And it is irrelevant, because the damage is on the other side.** Every cell
that was *positive* with the stop alone turns negative the moment the trail is
added:

```
  MACD M30        +$81,739  ->  -$76,947
  MACD H1        +$131,272  ->  -$21,616
  Breakout M30    +$48,714  ->  -$86,243
  Breakout H1    +$133,039  ->  -$47,914
```

**All twelve trail-bearing cells are negative**, at both stop widths, on both
strategies, at every timeframe. That is D-127's own mechanism read confirmed
from the other direction: these strategies' returns on this window come from a
small number of large trend-following holds, a 2%-armed 0.5% trail caps exactly
those, and bounding the loser side does not give back what capping the winner
side takes away. **A stop cannot rescue an exit that is cutting the trades the
edge lives in.**

The question is closed. Not "the trail needs a different activation or
distance" - that is a parameter search against one window, and D-131 already
showed what those produce here.

### D-149 - an incremental indicator plus unbounded holds; either alone is stable

D-148's script quotes each published figure beside the cell that reproduces it,
because a new harness agreeing with the old one is what makes its new cells
worth reading. Twenty-two of twenty-four reproduced within 2-4%, consistent with
the window starting a week later than D-124's. **Two did not**, and chasing that
gap is this entry.

```
                            this run    D-124 published      delta
  MacdCrossover M15, no stop  -$60,720       -$230,052    +$169,333
  MacdCrossover H1,  no stop   $18,713        $190,186    -$171,472
```

Every *stopped* MACD row reproduced within a few thousand, and so did every
`TrendlineBreakout` row, stopped or not. Only unstopped MACD moved, and it moved
by more than the result itself.

**It is not a harness bug. It is the window - but only for one of the four
combinations.** `scripts/measure_window_sensitivity_xauusd.py` fetches once,
fixes the end, and slides only the first bar, so every run sees a strict subset
of the same history priced by the same costs:

```
    measure_window_sensitivity_xauusd.py --strategy macd --timeframes M15 H1 --end 2026-08-28

  MacdCrossover M15          no stop      0.5% stop      MacdCrossover H1     no stop    0.5% stop
    +0d   2024-07-24        -$60,720       -$81,192        +0d  2018-03-20   -$54,190      $22,857
    +3d   2024-07-28        -$58,136       -$79,944        +3d  2018-03-23   -$52,317      $25,585
    +7d   2024-07-31        -$55,672       -$75,715        +7d  2018-03-27    $93,626      $23,418
   +14d   2024-08-07        -$53,229       -$75,664       +14d  2018-04-03    $92,757      $22,549
   +21d   2024-08-14       -$216,817       -$84,818       +21d  2018-04-10   -$54,205      $23,891
   +28d   2024-08-21       -$211,740       -$79,741       +28d  2018-04-17   -$54,170      $23,926
  spread                   $163,588         $9,153       spread              $147,831 SIGN FLIP  $3,035

    measure_window_sensitivity_xauusd.py --strategy breakout --timeframes M30 H1 --end 2026-08-28

  TrendlineBreakout M30      no stop      0.5% stop      TrendlineBreakout H1 no stop    0.5% stop
    +0d   2022-06-13         $77,993        $36,490        +0d  2018-03-20  $142,713      $89,308
   +21d   2022-07-04         $86,603        $45,784       +21d  2018-04-10  $141,165      $86,040
   +28d   2022-07-11         $81,104        $40,935       +28d  2018-04-17  $142,719      $87,570
  spread                      $8,610         $9,294       spread               $3,392      $3,554
```

(Each timeframe uses its own full 50,000-bar history rather than D-148's common
window, so levels are not comparable to that table - only the spread within each
column is.)

**The 2x2 is the finding.**

| | no stop | 0.5% stop |
|---|---|---|
| `MacdCrossover` - incremental EMAs | **unstable**: $163,588 spread on M15, $147,831 and a sign flip on H1 | stable: $9,153 and $3,035 |
| `TrendlineBreakout` - stateless Donchian | stable: $8,610 and $3,392 | stable: $9,294 and $3,554 |

Three of four cells are stable, all of them within roughly $3,000-$9,000 across
the whole sweep and none changing sign. Unstopped MACD is two orders of
magnitude worse in absolute terms and larger than its own result.
**Instability needs both ingredients, and neither is sufficient alone.**
Unbounded holding time is not the cause - `TrendlineBreakout` holds until the
opposing channel break and is the steadiest thing in the table. Incremental
state is not the cause either - stopped MACD carries the same EMAs and is fine.

**The mechanism, and why the two must meet.** `MacdCrossover`'s three EMAs are
running state seeded at the window's first bar (D-123 chose that deliberately,
for live/backtest parity). Move the start and the whole indicator path moves
slightly, so a marginal crossover fires on a different bar - a small
perturbation. A Donchian channel has no such memory: it is the max and min of
the last twenty bars, identical after twenty bars regardless of where the series
began, which is why its rows do not move. What turns MACD's small perturbation
into six figures is the unbounded hold: at 100 ounces a $1,000 move in gold is
$100,000, gold ran from roughly $2,400 to $4,470 across this window, and an
unstopped crossover holds until the opposing signal - so a shifted entry decides
which side of an enormous trend leg the position sits on. The flat stop cuts
those holds short and the amplifier is gone.

**What this costs us.** D-124's unstopped MACD figures - the -$230,052 that made
M15 look catastrophic and the +$190,186 that made H1 look like one of the best
cells in the project - are **not properties of `MacdCrossover`.** They are
properties of 2024-07-17. D-125's and D-126's readings of "the stop makes it
worse on rows that were positive" need re-reading wherever the positive baseline
was an unstopped MACD row, because that baseline was a coin flip. Every
`TrendlineBreakout` comparison stands unchanged, including D-124's H1 result,
which is now the better-supported of the two strategies for a reason that has
nothing to do with its P&L.

**One correction worth recording.** The first version of this check was a
throwaway script whose start-date arithmetic snapped each cut to midnight
instead of preserving the base timestamp, so its edge rows landed on different
bar counts and two of its numbers were wrong. The qualitative finding was
unaffected - the spread was six figures either way - but the entry above quotes
the committed script, which is the point of committing it.

**The rule this establishes.** A strategy carrying incremental indicator state
*and* unbounded holding time must report its sensitivity to the window edges, or
it is not reporting a result. The test is a start-date shift of a few weeks, it
costs one extra run, and it should come before walk-forward rather than after -
D-131 caught this class of problem the expensive way. A strategy with either
ingredient alone does not need it, which is what makes the check cheap to
target.

### D-150 - Gold Sniping's inputs can stop the wipeout but cannot create an edge

You asked whether the EA can be refined through its input settings. It ships
with every risk control disabled - `InpMaxPositions=0` (unlimited ladder depth),
`InpMaxFloatingDrawdown=0.0`, `InpDailyLossLimit=0.0`, `InpRestMinutes=0` - so
D-145's wipeout was the defaults with no brake connected. Four variants tested
against the same killer window (XAUUSD M15, 2024.01.01-2026.08.31, $10,000).
Each run is verified by parsing the parameters back out of MT5's own report, so
a variant whose settings failed to apply would be flagged, not counted.

| variant | trades | net | PF | balance DD | equity DD | win rate | last deal |
|---|---|---|---|---|---|---|---|
| defaults (D-145) | 340 | -$10,145 | 0.49 | 101.35% | 101.43% | 62.65% | **2024.02.26** |
| A `MaxPositions=5` | 417 | -$4,057 | 0.88 | 144.58% | 54.91% | **94.00%** | 2026.08.28 |
| B `MaxFloatingDD=$500` | 23,856 | -$9,990 | 0.95 | 102.01% | 100.39% | 84.86% | **2026.01.09** |
| C combo | 4,510 | -$5,515 | **0.89** | **63.09%** | 58.40% | 85.94% | 2026.08.28 |
| D `LotMultiplier=1.0` | 340 | -$10,145 | 0.49 | 101.35% | 101.43% | 62.65% | 2024.02.26 |

C = MaxPositions 5 + MaxFloatingDrawdown $500 + DailyLossLimit $300 + RestMinutes 60.

**Refinement works, up to a point.** Variant C converts a fatal EA into a merely
losing one: it survives all 32 months and holds drawdown to 63% instead of going
through zero. Capping ladder depth is the single most effective change, which
matches the diagnosis - unbounded depth, not lot escalation, was the killer.

**No setting reaches profitability.** Every variant sits below PF 1.0. The
restraints reduce the *rate* of loss; they do not manufacture an edge, because
there was none to uncover. The tail was truncated and what remained was a
negative-expectancy system paying spread on every basket.

**Two results worth keeping.**

*D is byte-identical to the defaults.* Setting `InpLotMultiplier=1.0` changed
nothing at all, which under a multiplier-driven ladder would be a drastic
change. So `InpLotMode=1` selects *step* mode and the multiplier input is inert;
escalation is additive via `InpLotStep=0.01`, capped by `InpMaxLot=0.1`. The
"2.0 multiplier" in the default set is decoration. An input that appears
dangerous and does nothing is worth more than a guess about what it does - this
is why D was run rather than reasoned about.

*A reaches a 94% win rate while losing $4,057.* The single cleanest illustration
in this project that win rate carries no information about profitability. B is
the mirror image: capping floating loss alone converts one fatal basket into
23,856 small realised losses and still ends at 102% drawdown - death by a
thousand cuts rather than one.

**Scope limit, stated deliberately.** This tested the risk-control inputs, not
the entry logic (`InpEMAPeriod`, `InpDistancePoints`, `InpBasketProfit`). A
sweep of those on this one window would very likely find a profitable
combination, and it would be curve-fitting: with 32 months and a handful of free
parameters, some setting always fits. Any such result would need walk-forward
across separate periods before it meant anything. Cross-reference D-145 on
single-window evidence.
