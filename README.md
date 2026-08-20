# GOLDM strangle engine

An event-driven trading system for a short strangle on **MCX GOLDM options** — one
~0.25-delta call and one ~0.25-delta put, one cycle per monthly expiry, traded through
Angel One.

**Status: Milestones 1-8 complete, except the chain recorder (M1.5) and the Angel One
adapter (M7).** Domain models, configuration, the MCX calendar, feeds and resampler, the
look-ahead guarantees, option pricing, the backtest engine and cost model, the strangle
strategy and full risk layer, walk-forward analysis, the paper adapter with reconciliation
and crash recovery, and the monitoring dashboard are all built and tested.

**Nothing has been connected to a real broker, and no real market data has been recorded.**
Every number the system can currently produce comes from generated data.

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
  ! CHARGE RATES ARE PLACEHOLDERS - net P&L is not calibrated (D-011, Q6)
  ! SPREAD IS MODELLED, NOT MEASURED - the recorder replaces this at M1.5
```

Whether a walk-forward on your data could tell you anything, before spending months
recording it:

```bash
.venv/Scripts/algo.exe walkforward
```

## The dashboard

Two processes. The API reads a state file the engine writes; it never holds the engine.

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

469 tests. The suite includes the look-ahead canaries required by the brief: cheating
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
├── data/        feeds, resampler, synthetic fixtures, quality gates
├── pricing/     Black-76, IV solver, greeks, forward cross-check
├── costs/       MCX charge stack, spread and slippage models
├── execution/   the fill simulator, shared by backtest and paper
├── portfolio/   position book, cash, the equity identity
├── risk/        sizing and the caps
├── backtest/    the bar-by-bar event loop
├── reporting/   metrics
├── strategy/    Strategy contract, BarContext, the strangle, two reference strategies
├── persistence/ the write-ahead order journal and the dashboard state store
├── api/         FastAPI read-only monitoring + the kill-switch request
└── cli/         verify, config, backtest, walkforward, serve

dashboard/       Next.js monitoring page (4 runtime dependencies, no chart library)
```

Still to come: the **chain recorder (M1.5)** and the **live adapters (M7)**: Kotak
Neo trades the chain live while Angel SmartAPI supplies the historical/closed
bars — see `docs/open-questions.md` Q10/Q11.

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
- [`docs/open-questions.md`](docs/open-questions.md) — what is still blocking, and on what

## A note on what this can and cannot tell you

At roughly twelve expiry cycles a year, no metric this system produces will distinguish
skill from luck for a long time. Trade counts are reported next to every ratio. Synthetic
data proves the arithmetic is right and nothing else — a generator always offers a fill
and never shows a book that empties when you need it. No output of this system will claim
the strategy is profitable.
