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
