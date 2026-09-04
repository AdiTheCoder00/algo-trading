# GOLDM strangle engine

An event-driven trading system for a short strangle on **MCX GOLDM options** — one
~0.25-delta call and one ~0.25-delta put, one cycle per monthly expiry, traded through
Angel One.

**Status: Milestones 1-8 complete, except the chain recorder (M1.5).** Domain models,
configuration, the MCX calendar, feeds and resampler, the look-ahead guarantees, option
pricing, the backtest engine and cost model, the strangle strategy and full risk layer,
walk-forward analysis, the broker adapters with reconciliation and crash recovery, the
paper trading loop, and the monitoring dashboard are all built and tested.

**It has been connected to the real broker, and it has run on real market data — and
neither result should be read as more than it is.**

- The Kotak Neo and Angel SmartAPI sessions connect and reconcile cleanly. Doing that
  for the first time found three bugs in code that had passing tests (D-113). The
  paper loop has **not** yet traded a live session; it has only run outside market
  hours.
- A real backtest ran over 82,020 MCX bhavcopy rows, six GOLDM cycles, Jan-Jun 2026.
  **Its P&L is not a usable estimate of edge**: exits fill at the next bar, and with
  two bars a day the median overnight gap (~₹5,950/lot) is larger than the take-profit
  target it is chasing (~₹4,533). It tells you the shape works, not what it earns
  (D-108).

Everything else the system produces still comes from generated data.

Live trading is off by default and cannot be reached by accident. See [Modes](#modes).

---

## Setup

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Python 3.11 or newer. On macOS or Linux the interpreter path is `.venv/bin/python`.

## Run it

The Milestone 1 proof — the data pipeline end to end on synthetic bars, in both
daylight-saving regimes:

```bash
.venv/Scripts/algo.exe verify
```

```
Synthetic pipeline check, 30m bars

  US DST      2026-08-19  session 870 min  ->  29 bars (expected 29, 0 partial)
               29 bars, no findings
  standard    2026-11-10  session 895 min  ->  30 bars (expected 30, 1 partial)
               30 bars, no findings
```

Those two lines are the milestone in miniature. MCX closes at 23:30 IST while **US**
daylight saving is in force and 23:55 IST otherwise, so a 30-minute session divides into
29 bars for part of the year and 29 bars plus a 25-minute stub for the rest. A resampler
that assumed a fixed grid would be quietly wrong for eight months at a time.

To see exactly what configuration a run would use, and its hash:

```bash
.venv/Scripts/algo.exe config
```

The Milestone 3 falsification — a coin flip on a market that never moves, which must
make exactly nothing before costs and lose exactly the costs:

```bash
.venv/Scripts/algo.exe backtest
```

```
  FALSIFICATION: gross P&L on a flat market = 0.00 (must be exactly 0)
  ! CHARGE RATES ARE SOURCED, NOT CONTRACT-NOTE VERIFIED - net P&L is not calibrated to the paisa (D-011, Q6)
  ! SPREAD IS MODELLED, NOT MEASURED - the recorder replaces this at M1.5
```

Whether a walk-forward on your data could tell you anything, before spending months
recording it:

```bash
.venv/Scripts/algo.exe walkforward
```

## Historical option data

There are two historical sources, deliberately kept apart.

**SmartAPI** serves GOLDM *futures* bars at 30 minutes. It cannot serve options that
have already expired - Angel One state that data of expired contracts is not stored,
and an expired contract has no `symboltoken` to request history with. That rules out
every option cycle worth backtesting.

**The MCX bhavcopy** is the daily contract-wise file: open, high, low, close, volume
and open interest for every contract that traded, **including expired ones**, back to
2016 through third-party archives. About a hundred monthly GOLDM cycles are available
today rather than after two and a half years of recording.

Download a day's file from [mcxindia.com](https://www.mcxindia.com/market-data/bhavcopy)
and check it before trusting anything built on it:

```bash
.venv/Scripts/algo.exe bhavcopy path/to/bhavcopy.csv
```

That either parses cleanly and reports coverage, or prints the columns your file
actually has next to each layout the loader tried. Correcting it means passing a
`BhavcopyColumns`, not editing the parser.

**Two things worth knowing about the file itself** (D-105). The "commodity wise"
export arrives with an `.xls` extension and is neither Excel nor CSV — it is an HTML
`<table>` — so the loader sniffs content rather than trusting the name. And its column
layout is now **verified against a real file**; the blind guess got 10 of 12 headers
right, missing only the unit suffixes on `Volume(Lots)` and `Open Interest(Lots)`. The
older `MCX_DEFAULT_COLUMNS` mapping stays as a fallback and stays labelled unverified,
because the plain CSV bhavcopy has still never been seen.

Coverage matters as much as the schema:

Real output, from the archive this project actually ran against:

```
GOLDM: 82,020 option rows over 140 sessions
  span     2026-01-01 .. 2026-07-29
  cycles   7 expiries
  traded   16,685 rows had volume (20.3% of the ladder)
  breadth  119.2 strikes traded per session on average
```

A hundred cycles of history is only worth having if the strikes the strategy wants
were changing hands. That last figure is the honest answer, and it is also how
question **Q1d** - are the 0.25-delta strikes actually two-sided quoted - gets
answered from years of evidence instead of one screenshot.

What this data cannot do, stated here rather than in a footnote, because every number
derived from it inherits these:

- **It is daily.** There is no 09:30 bar. Entry at the 09:30 close has to be proxied
  by the day's open, which approximates the strategy rather than running it.
- **There is no bid or ask.** The spread stays an assumption, and on a thin GOLDM
  option book the spread *is* the dominant cost. Only the recorder settles that.
- **Stops can only be judged against the daily high and low**, which is pessimistic
  and consistent with §6, but coarse.

So a bhavcopy backtest answers "has this shape ever worked, across many real cycles".
It does not answer "what would I actually have been filled at". Shape over a hundred
cycles still beats precision over none.

### Running the real strategy against it

`backtest` (above) proves the engine's cost arithmetic on synthetic data and
deliberately trades nothing resembling the actual strategy. This runs
`DeltaStrangle` itself, against every real monthly cycle the bhavcopy archive
covers:

```bash
.venv/Scripts/algo.exe backtest-bhavcopy path/to/bhavcopy/ --config config/goldm.yaml
```

Each session becomes exactly two bars - entry (09:30 IST, priced from the day's
open) and close (the real session close, priced from the day's close, high and
low) - because that is what end-of-day data can honestly support. Every exit
check in between is invisible to the run, not approximated: a stop that would
have fired and reversed by the close simply is not seen. The command prints this
as a standing warning on every result:

```
  ! SHAPE TEST ONLY - two ticks a day (open, close), not a real
    intraday grid. See the command's --help for what that trades away.
```

`--config` pulls sizing, caps, the kill switch, the devolvement window and the
strategy's own parameters (target delta, DTE band, exit levels) from a config
file instead of the brief's own stated defaults. `--tearsheet` and `--trade-log`
work exactly as they do for `backtest`.

## The dashboard

Two processes. The API reads a state file the engine writes; it never holds the engine.

Feed that file from a backtest:

```bash
.venv/Scripts/algo.exe backtest --config config/goldm.yaml --state state/dashboard.db
```

The engine streams equity, positions, signals, notes, completed trades and health into
`state/dashboard.db` after every bar, and reads halt requests back out of it — a halt
recorded while a run is going trips the kill switch at the engine's next bar, and the
request can ask for an explicit flatten alongside it. Without `--state` the engine is
exactly what it was before; the wiring is invisible when unused.

```bash
ALGO_API_TOKEN=pick-a-secret .venv/Scripts/algo.exe serve
```

```bash
cd dashboard && npm install && npm run dev
```

Copy `dashboard/.env.example` to `dashboard/.env.local` and set the same token. It has
no `NEXT_PUBLIC_` prefix on purpose — the token guards the kill switch, so it stays on
the Next.js server and every call is proxied. It never reaches the browser.

The page shows the equity curve and underwater plot, open positions, signals **with the
reason they fired**, why the strategy declined to trade, system health, and the kill
switch. It is read-only apart from that one button, and the button records a *request* —
the engine acts on it at its next bar, so the UI says "halt requested", never "halted".

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

1320 tests. The suite includes the look-ahead canaries required by the brief: cheating
strategies that try to read the next bar, reach for a dataframe, reach for the feed, or
bolt an attribute onto the context — each must raise. It also runs the whole pipeline
twice, once against a dataset whose future bars have been replaced with randomised
garbage, and asserts the two outputs are identical.

The gates the brief requires:

```bash
.venv/Scripts/python.exe -m mypy
```

```bash
.venv/Scripts/python.exe -m ruff check .
```

---

## Modes

`backtest` and `paper` run freely. `live` requires **three** independent things to agree,
and the failure message names whichever one is missing:

1. `mode: live` in the configuration file
2. `TRADING_MODE=live` in the environment — matched exactly, no case folding, no trimming
3. the `--i-understand-this-is-real-money` flag

There is no default that reaches live, and env and config disagreeing is refused rather
than resolved in either direction.

## What exists

```
algo/
├── core/        domain models, Decimal money, UTC time, the bar window
├── config/      schema, loader, the live-trading gate
├── exchange/    MCX calendar, expiry resolution, contract specs
├── data/        feeds, resampler, synthetic fixtures, quality gates, MT5/Kotak feeds
├── pricing/     Black-76, IV solver, greeks, forward cross-check
├── costs/       MCX charge stack, CFD swap, spread and slippage models
├── execution/   the fill simulator, broker adapters (Kotak, MT5, paper), router
├── portfolio/   position book, cash, the equity identity
├── risk/        sizing, exits, kill switch, devolvement guard
├── backtest/    the bar-by-bar event loop, bhavcopy/CFD/smartapi runners, sweep
├── live/        the trading loop, feeds, alerts, shutdown, the MT5 paper runner
├── reporting/   metrics, significance (bootstrap + permutation)
├── strategy/    Strategy contract, BarContext, the strangle, CFD reference strategies
├── persistence/ the write-ahead order journal and the dashboard state store
├── api/         FastAPI read-only monitoring + the kill-switch request
└── cli/         verify, config, backtest, backtest-bhavcopy, backtest-smartapi,
                 walkforward, significance, live, live-mt5, mt5-replay, serve,
                 killswitch, stop, credentials, telegram-check, bhavcopy, chain

dashboard/       Next.js monitoring page (3 runtime dependencies, no chart library)
```

The Kotak Neo (live broker) and Angel SmartAPI (historical bars) adapters are built
and tested — `algo live` connects both sessions and reconciles against the broker; see
`docs/open-questions.md` Q10/Q11 for the credentials it needs.

**The trading loop exists and runs, in paper mode only.**

```bash
.venv/Scripts/algo.exe live config/goldm.yaml --passes 450 --poll 120 --state state/paper.db
```

`--passes` drives `LiveLoop`: real candles in, the option chain polled and greeked
once per bar, `BacktestEngine.decide` — the *same* decision path the backtest uses,
not a second copy — and whatever it returns routed through `OrderRouter` to the
**paper** broker, which simulates fills with the same `FillSimulator`. Every pass is
bounded (`--passes` is required and has no default) and the run refuses any mode but
backtest/paper *before reading a credential*. It has connected and reconciled against
the real broker; it has not yet traded a live session, because it has only been run
outside market hours (D-109 through D-113).

What is **not** built: routing to a real account, and the **chain recorder (M1.5)**
that would persist a live book continuously — `backtest-bhavcopy`, above, is the
substitute until then.

## Design notes worth knowing before reading the code

- **Money is `Decimal`, never `float`** — including through storage. Parquet price columns
  are written as strings, and a float column is refused on read rather than silently
  converted. `dec()` refuses a float argument outright, because `Decimal(0.1)` is legal
  Python and produces `0.1000000000000000055511151231257827`.
- **Greeks are `float`, and never touch money.** Implied volatility comes out of an
  iterative solver; forcing `Decimal` through it buys no accuracy. The justification is
  that a delta only ever selects a strike, and the strike itself is a `Decimal`.
- **The future is absent, not hidden.** `BarContext` receives a *copy* of `[0..i]`. There
  is no accessor for `i+1` because there is no bar `i+1` in the object.
- **Venue constants carry provenance.** Every entry in `algo/exchange/data/` has an
  `effective_from` and a `source`. Unverified values are left null rather than guessed,
  and the calendar refuses to answer for dates beyond its verified holiday range instead
  of assuming a trading day.

  **That refusal is currently switched off, and the config says so.** There is no
  sourced MCX holiday list yet, so `market.allow_unverified_calendar` is `true`
  and the calendar knows only weekends plus the weekend sessions MCX was
  observed to hold (D-107). `mcx_calendar` refuses to start with the flag false
  and no `holidays_file`, so the gap cannot be closed by accident — it has to be
  closed by supplying the list. Open as **Q20**; it matters most for the
  devolvement deadline, which walks back trading days.
- **Expiry dates are read, never computed.** A derived expiry rule was wrong once in this
  project already. The instrument master is authoritative; the last-Friday rule is only a
  cross-check, and a mismatch halts rather than picking a side.
- **A signal from bar `i` executes at bar `i+1`'s open.** Filling at bar `i`'s own close
  would let a decision taken from a bar profit from that same bar — the subtlest
  look-ahead there is, because every number stays plausible.
- **Fills round against you; limit placement rounds for you.** Two opposite functions,
  deliberately. Rounding a fill the friendly way is a free fraction of a tick per trade.
- **Positions store an exact cost basis, not an average price.** Dividing to get an
  average is not exact in decimal, and the error propagates into every later P&L figure.
  The equity identity is re-checked two independent ways after every event — it caught a
  1e-21 drift on its first run.
- **A metric that cannot be computed returns nothing, not zero.** A Sharpe of 0.0 reads
  as "no edge"; `None` reads as "not enough data", which is usually the truth here.
- **The API reads a file; it never holds the engine.** A web framework holding live
  trading objects is one bug away from mutating trading state to serve an HTTP request.
  A test fails if a second mutating endpoint ever appears.
- **The kill switch is a request, not an action.** The API records that a halt was asked
  for and returns 202; the engine trips its own switch on the next bar. A dead API cannot
  leave the engine half-tripped, and a dead engine cannot swallow a halt.
- **An order is written before it is sent, and never sent twice.** A crash between the
  write and the call leaves an ambiguous SENT, which reconciliation resolves. The reverse
  ordering would leave a JOURNALLED order the broker already holds — and the obvious
  recovery would double the position.
- **An unconfirmable order halts trading rather than being resent.** An order missing from
  the broker may never have arrived, or may have filled and been cleared. Those look
  identical from outside.
- **Devolvement is a hard rule, not a setting.** An in-the-money short leg left at option
  expiry becomes a GOLDM futures position, and GOLDM futures go to compulsory physical
  delivery of gold. The risk layer will force-exit before expiry and refuse to carry
  futures into the tender period; there is deliberately no configuration flag to disable
  that.

## Documents

- [`docs/milestone-0-plan.md`](docs/milestone-0-plan.md) — architecture, interfaces, config
  schema, and the analysis of the live chain
- [`docs/decisions.md`](docs/decisions.md) — every judgement call and its reason, including
  the corrections
- [`docs/backtest-assumptions.md`](docs/backtest-assumptions.md) — every modelling
  assumption, marked SETTLED / PROVISIONAL / UNRESOLVED
- [`docs/backtest-history.md`](docs/backtest-history.md) — every strategy measured and what
  it returned, and the four patterns the whole record shows
- [`docs/open-questions.md`](docs/open-questions.md) — what is still blocking, and on what

## A note on what this can and cannot tell you

At roughly twelve expiry cycles a year, no metric this system produces will distinguish
skill from luck for a long time. Trade counts are reported next to every ratio. Synthetic
data proves the arithmetic is right and nothing else — a generator always offers a fill
and never shows a book that empties when you need it. No output of this system will claim
the strategy is profitable.
