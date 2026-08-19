# Backtest assumptions — MCX GOLDM options

**Status: DRAFT — written at Milestone 0, before any engine exists.** Every assumption is
`SETTLED`, `PROVISIONAL` (my default, overridable) or `UNRESOLVED` (blocked on a question).
Nothing reaches `SETTLED` without a citation or a test that demonstrates it.

Per §6 of the brief: *any assumption not written down is a lie waiting to happen.* The
inverse holds too — writing it here does not make it true, it makes it **auditable**.

---

## 1. Market and session

| # | Assumption | Status |
|---|---|---|
| 1.1 | MCX non-agri commodities (gold included) trade a single continuous session, 09:00 IST to **23:30 IST during US daylight saving** (2nd Sunday March → 1st Sunday November) and **23:55 IST otherwise**. No lunch break. Agri commodities close at 17:00 and are out of scope. | PROVISIONAL — verify against MCX circular |
| 1.2 | The evening close time is driven by **US** DST, not Indian DST (India has none). The calendar keys its close off `America/New_York` transitions while expressing the session in `Asia/Kolkata`. A session model written only in IST is wrong for ~8 months a year. | SETTLED |
| 1.3 | All internal timestamps are tz-aware UTC. Session logic converts via named zones only, never a fixed offset. | SETTLED |
| 1.4 | Bars are labelled by **close** and anchored to the session open: 09:30, 10:00 … 23:30. In US-DST months that is exactly 29 bars; otherwise 29 bars plus a 25-minute stub 23:30–23:55 flagged `is_partial`. **Bars per day changes twice a year.** | PROVISIONAL (Q9) |
| 1.5 | The strategy may not act on a partial bar; the risk layer may. | PROVISIONAL (Q9) |
| 1.6 | Holidays come from an effective-dated MCX calendar with a cited source. A date not in the calendar is an error, never silently a trading day. | SETTLED |
| 1.7 | There is an overnight gap of ~9.5 hours every trading day plus weekends, during which international gold continues to trade. **No stop is active across it.** | SETTLED |
| 1.8 | The MCX evening session exists to overlap the US session. COMEX open and US data releases are where GOLDM moves, so session-of-day is a meaningful axis — §2.6's kill-zone concept applies here, contrary to my first draft. | SETTLED |

## 2. Instrument, contract and settlement

| # | Assumption | Status |
|---|---|---|
| 2.1 | GOLDM options are **options on GOLDM futures**. `F` in Black-76 is the observed futures price — no synthetic forward, no carry term, no dividend term. | SETTLED |
| 2.2 | Trading unit 100 g, quoted in ₹ per 10 g, so contract multiplier = 10. Tick size, strike interval, max order size and DPR circuit limits from an effective-dated spec store with `source`. | PROVISIONAL — verify against MCX contract spec |
| 2.3 | **Option expiry ≠ futures expiry.** Options on compulsorily-deliverable commodities expire before the futures — reported as 2 working days prior to the first day of the tender period. Both dates plus the tender window are modelled. Off by one day here means the backtest holds a position that in reality would have devolved. | PROVISIONAL — verify |
| 2.4 | At option expiry, ITM options outside the **Close-To-Money band** (typically ATM ± 2 strikes) **devolve automatically into a futures position at the strike**. CTM options do not auto-devolve and need explicit instruction. Options are European-style. | PROVISIONAL — verify |
| 2.5 | GOLDM futures are **compulsory physical delivery** — 100 g of gold, with a tender period and delivery obligations. | PROVISIONAL — verify |
| 2.6 | Following from 2.4 and 2.5: the risk layer **force-exits every short option before its expiry session** and **refuses to carry any futures position into the tender period**. These are hard rules, not config. The backtest, paper and live paths enforce them identically, and a test proves that disabling the rule is what produces a delivery obligation. | SETTLED |
| 2.7 | Every order is validated against the spec in force — tick grid, lot step, min/max lots, max order size. Violations are rejected before reaching the broker and logged with the failing constraint. | SETTLED |
| 2.8 | **DPR circuit limits are modelled.** A circuit-locked strike cannot be filled at any price. A backtest that fills through a locked circuit is lying about the one moment it matters most. | SETTLED |

## 3. Costs

| # | Assumption | Status |
|---|---|---|
| 3.1 | The charge stack is itemised, never blended: brokerage, **CTT**, MCX transaction charge, SEBI turnover fee, stamp duty, GST. Each has its own base and rounding. | SETTLED |
| 3.2 | **CTT is charged on the sell side of option premium and paid by the writer.** For a strategy whose every entry is a sale, this is a direct per-entry cost, not a rounding item. Indicative rate ~0.05% of premium; **not trusted until a contract note confirms it.** | UNRESOLVED (Q6) |
| 3.3 | Exercise/devolvement attracts its own CTT treatment. Since the strategy is designed never to reach expiry with an open leg, this should never fire — and if it ever does, that is a rule breach and gets logged as one. | UNRESOLVED (Q6) |
| 3.4 | Indicative secondary-source figures found while planning — MCX transaction charge ~₹260/crore non-agri, stamp duty ~0.002% buy side, SEBI ~₹10/crore, GST 18% on brokerage + exchange charges — are **placeholders only**. Reported net P&L is not authoritative until calibrated against a real Angel One contract note and locked by a unit test reproducing it to the paisa. | UNRESOLVED (Q6) |
| 3.5 | **No swap or overnight financing exists** on MCX F&O. The MT5 triple-swap-Wednesday model in §6 does not apply and is not implemented. | SETTLED |
| 3.6 | Margin (SPAN + exposure) is blocked, not spent. Optional opportunity cost on blocked margin is **off by default** and, if enabled, reported on its own line so it never contaminates trading P&L. | PROVISIONAL |
| 3.7 | Daily MTM is settled in cash; equity is marked daily, not only at trade close. | SETTLED |
| 3.8 | Cost drag reported as total charges as a % of **gross** P&L, itemised by component. | SETTLED |

## 4. Spread, slippage and fills

| # | Assumption | Status |
|---|---|---|
| 4.1 | **GOLDM option liquidity is thin, and spread is expected to be the dominant cost.** It cannot be inferred from OHLC, which is why the recorder captures depth. | PROVISIONAL — the recorder will settle this with measurement |
| 4.2 | Where depth is recorded, fills walk the book. Where it is not, spread is modelled and the fill is **tagged as modelled**, so no report can silently mix measured and assumed fills. | SETTLED |
| 4.3 | Spread widens at the 09:00 open, around the US session open, and into the close. The MT5 "broker midnight rollover widening" has no analogue and is not modelled. | PROVISIONAL |
| 4.4 | Market orders slip against us by a configured fraction of the spread; stops slip more than limits. | SETTLED (values PROVISIONAL) |
| 4.5 | A stop inside a bar's range fills at the stop price plus slippage. If stop and target are both inside one bar, **the stop is assumed to have hit first**. Deliberately pessimistic, per §6. | SETTLED |
| 4.6 | Gaps fill at the open, not at the stop price. | SETTLED |
| 4.7 | A bar failing a quality gate — empty book, crossed quote, stale print, non-positive premium, circuit-locked — is **untradeable**. The engine does not fill against it and logs the skip. | SETTLED |
| 4.8 | Fill size is capped by a configured fraction of traded volume / available depth. **No assumption of infinite liquidity.** On a thin book this is the difference between a backtest and a fantasy. | SETTLED |
| 4.9 | The **backtest evaluates stops at bar granularity only**; live will evaluate on ticks. The backtest is therefore **optimistic relative to live on fast moves**. Reported, not buried. | UNRESOLVED (Q15) |

## 5. Option pricing and delta

| # | Assumption | Status |
|---|---|---|
| 5.1 | Delta from Black-76 with `F` = the synchronously-observed underlying GOLDM futures price. Never a stale futures price, never spot gold. | SETTLED |
| 5.2 | IV solved from the **mid** of the recorded book where both sides exist, else from the last trade, and which one was used is recorded per row. | PROVISIONAL |
| 5.3 | Risk-free rate is a configured constant, used only for discounting. No term structure in v1. | PROVISIONAL |
| 5.4 | Greeks are `float` and used **only** for strike selection and monitoring. They never enter money math. Delta is rounded to fixed precision before comparison so selection is reproducible. | SETTLED |
| 5.5 | An IV solve that fails to converge marks the row untradeable. No fallback value. | SETTLED |
| 5.6 | "0.25 delta" = the strike whose absolute delta is nearest 0.25 within tolerance, evaluated **on the closed bar**, chosen independently per side, and only among rows passing `is_tradeable`. | PROVISIONAL (Q3/Q4) |

## 6. Data provenance

| # | Assumption | Status |
|---|---|---|
| 6.1 | The data source is recorded in every run's metadata. **Synthetic-fixture runs are labelled `SYNTHETIC` on every report page.** | SETTLED |
| 6.2 | Synthetic and recorded results are **never** blended into one number, chart or metric table. | SETTLED |
| 6.3 | Synthetic fixtures exist to prove the engine is arithmetically correct. They prove **nothing** about the strategy, because a generator always offers a fill and never shows a book that empties when you need it. | SETTLED |
| 6.4 | Recorded data carries a daily coverage report — snapshots captured vs expected — so a recorder outage is discovered the next morning, not in month four. | SETTLED |
| 6.5 | Raw payloads are archived verbatim beside the parsed form, so a parser bug found later is repairable rather than fatal. | SETTLED |
| 6.6 | Each run records the dataset hash, so a result ties to the exact bytes that produced it. | SETTLED |

## 7. Accounting

| # | Assumption | Status |
|---|---|---|
| 7.1 | Equity = cash + unrealised P&L at all times, asserted by a property test on every event. | SETTLED |
| 7.2 | Cost basis is weighted average per instrument; a round-trip `Trade` records realised P&L net of every itemised charge. | PROVISIONAL |
| 7.3 | Short options are marked at the option's own recorded price, not a model price. | SETTLED |
| 7.4 | R-multiples need a defined R. For this strategy R is the **configured stop level** (Q4), not the maximum possible loss, and that is stated wherever R appears. | PROVISIONAL |

## 8. What this backtest cannot tell you

Stated up front so no chart is read as more than it is.

1. **Sample size.** At roughly six expiry cycles a year, no metric here can distinguish skill
   from luck. The trade count is printed next to every ratio, and if walk-forward has too few
   cycles to be meaningful, the report will say so instead of drawing a chart.
2. **The true tail.** A short strangle's losses are dominated by events that may not be in the
   sample. Reported max drawdown is a sample statistic, not a bound.
3. **Import duty risk.** MCX gold ≈ international gold × USDINR + import duty. A duty change
   produces a step move with no international counterpart, arrives without warning, and is
   exactly the event that destroys a short strangle. It is unmodellable from price history and
   will not appear unless one happened to fall in the sample.
4. **USDINR.** Embedded in every MCX gold price and not separately captured by the option's
   implied vol.
5. **Liquidity at size.** Even with recorded depth, the backtest cannot model the effect of
   *our own* order on a thin book.
6. **Broker-side events.** Forced square-off, discretionary margin calls, and exchange halts
   are not modelled.
7. Per §12 of the brief, **no output of this system will claim the strategy is profitable.**
   It reports numbers, in-sample and out-of-sample side by side, with sample sizes attached,
   and you judge.
