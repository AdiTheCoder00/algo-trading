# Pivot/EMA cascade — Telegram alerts

Watches the 5-minute chart and messages you when the Fibonacci-pivot / five-EMA
cascade fires:

* **SELL** — a candle closes down through **R3, R2, R1 or the pivot**, then down
  through the **10, 20, 50, 100** and finally the **200 EMA** (after, or on the
  same candle). The close beyond the 200 EMA is the entry.
* **BUY** — the mirror, arming on **S3, S2, S1 or the pivot**.
* **EXIT** — the first candle that closes back through **both** the 10 and the
  20 EMA, from the other side.

The pivots are TradingView's *Pivot Points Standard* with the type set to
**Fibonacci**, drawn from the previous session.

## Read this before you trade off it

The rule this alerts on **has never been measured on real bars**. `docs/decisions.md`
D-155 records why that matters here: four strategies were built from
published rules in this repo before it, all four were measured, and none of them
had an edge. A message from this tool means *the chart did this*, not *take this
trade*. Run `scripts/measure_pivot_ema_cascade_xauusd.py` first.

## What it watches

| Symbols | Where the bars come from | Needs |
|---|---|---|
| `XAUUSD`, `BTCUSD`, your broker's Volatility 100 | the MetaTrader 5 terminal | Windows, MT5 running and logged in |
| NIFTY 50 constituents | Angel One SmartAPI | API key, client id, password, TOTP secret |

MT5 symbols must be spelled **exactly as your terminal's Market Watch spells
them**. That is why the volatility index is configured rather than hard-coded:
brokers name it differently (`Volatility 100 Index`, `VOL100`, `Vol_100`), and a
guess would watch nothing and only say so in a log line.

## Setup

```bash
pip install -e ".[dev,mt5]"          # from the repo root, on the MT5 machine
```

Then set these (a `.env` beside the repo works — the repo already loads one):

```ini
TELEGRAM_BOT_TOKEN=123456:ABC...     # from @BotFather
TELEGRAM_CHAT_ID=-1001234567890      # your chat or channel
ALERT_MT5_SYMBOLS=XAUUSD,BTCUSD,Volatility 100 Index
ALERT_POLL_SECONDS=75                # optional, default 75
ALERT_STATE_PATH=cascade_alert_state.json   # optional

# only needed for the NIFTY 50 list
ALGO_SMARTAPI_API_KEY=...
ALGO_SMARTAPI_CLIENT_ID=...
ALGO_SMARTAPI_PASSWORD=...
ALGO_SMARTAPI_TOTP_SEED=...          # the TOTP *secret*, not a 6-digit code
```

## Running

```bash
# print what it would send, no token needed - start here
python tools/pivot_ema_telegram_alert/cascade_alert.py --dry-run --once

# live, MT5 symbols only
python tools/pivot_ema_telegram_alert/cascade_alert.py

# live, MT5 symbols plus all 50 NIFTY constituents
python tools/pivot_ema_telegram_alert/cascade_alert.py --nifty50
```

To keep it running on Windows, point a Scheduled Task at it the way
`tools/macd_telegram_alert/setup_scheduled_task.ps1` does for the MACD monitor.

## How it decides

Every poll re-reads about 574 closed bars per symbol and feeds them through
**the same `PivotEmaCascade` the backtest scores**, via
`algo/backtest/signal_replay.py`. It does not reimplement the rule anywhere — an
alert that disagreed with the backtest would put you in a trade no measurement
covers, and `tests/test_signal_replay.py` pins the two paths to the same entries.

574 bars is the 200 EMA's 285-bar warmup, plus the bar a crossing is compared
against, plus one full M5 session so the pivots come from a session the strategy
watched end to end.

Each poll starts from scratch rather than carrying indicator state between
polls. A laptop that slept, a terminal that restarted, a poll that failed — none
of them leave the tool believing something the bars do not say. The only thing
persisted is which bar each symbol was last alerted on, so a restart inside the
same five minutes does not send the message twice.

## Failure behaviour

One symbol failing never stops the others: an MT5 symbol that cannot be selected
or a SmartAPI login that expires is logged once and retried on the next poll.
Nothing here places an order, sizes a position, or touches a broker's trading
endpoint — it reads bars and sends text.
