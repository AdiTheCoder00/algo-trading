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
