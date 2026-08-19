# GOLDM strangle engine

An event-driven trading system for a short strangle on **MCX GOLDM options** — one
~0.25-delta call and one ~0.25-delta put, one cycle per monthly expiry, traded through
Angel One.

**Status: Milestones 1-3 complete.** Domain models, configuration, the MCX calendar, the
feeds and resampler, the look-ahead guarantees, option pricing (Black-76 + IV solver), the
backtest engine and the MCX cost model are built and tested. The strangle strategy itself
is Milestone 4. Nothing here has been connected to a broker, and no real market data has
been recorded yet.

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

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

310 tests. The suite includes the look-ahead canaries required by the brief: cheating
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
├── strategy/    the Strategy contract, BarContext, and the two reference strategies
└── cli/         verify, config, backtest
```

Still to come, in order: the chain recorder (M1.5), the strangle strategy and the full
risk layer (M4), walk-forward (M5), the paper adapter (M6), the Angel One adapter (M7),
and the dashboard (M8).

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
