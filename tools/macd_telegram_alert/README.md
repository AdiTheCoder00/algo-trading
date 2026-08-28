# MACD crossover → Telegram alerts

Watches one or more pairs on any ccxt exchange and sends a Telegram message
whenever the MACD line crosses its signal line on a **closed** candle. Runs live,
or replays history with `--backtest` to see every crossover it would have sent.

Standalone: it does not import the `algo` package and does not share the
repository's `.env`. It only reads market data — it never places an order.

## What it does

- Pulls OHLCV over ccxt on a schedule aligned to the candle clock.
- Drops the in-progress candle before doing anything else, so an intrabar wiggle
  can never produce a signal that later disappears (no repainting).
- Computes MACD as `EMA(fast) - EMA(slow)`, signal as `EMA(macd, signal_period)`,
  default 12 / 26 / 9, with `adjust=False` so the values match TradingView.
- Compares the histogram (`macd - signal`) on the last two closed candles:
  - `≤ 0 → > 0` → **bullish** alert
  - `≥ 0 → < 0` → **bearish** alert
- Records the alerted candle in `state.json`, so restarting mid-candle does not
  re-send an alert you already have.
- `--backtest` runs the identical logic over history instead of the live feed;
  see [Backtest mode](#backtest-mode).

## Setup

```bash
cd tools/macd_telegram_alert
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit .env
```

Getting the two Telegram values:

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token
   into `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `result[0].message.chat.id` into `TELEGRAM_CHAT_ID`. For a group, add the bot
   to the group first — group ids are negative.

Exchange API keys are optional. Public candle endpoints need no credentials on
most exchanges; fill them in only if yours requires a key for OHLCV or you want
the authenticated rate limit. Read-only keys are sufficient.

## Run

```bash
python macd_alert.py
```

With `SEND_STARTUP_MESSAGE=true` (the default) you get a confirmation message on
launch — the quickest check that the token and chat id are right. Console output
looks like:

```
2026-08-28 14:00:16 | INFO    | macd_alert | Watching BTC/USDT on binance (1h), MACD(12, 26, 9), closed candles only.
2026-08-28 14:00:17 | INFO    | macd_alert | BTC/USDT 1h | close=64120.50 macd=112.4013 signal=98.7712 hist=13.6301 (candle 2026-08-28 13:00:00 UTC)
2026-08-28 14:00:17 | INFO    | macd_alert | BTC/USDT: BULLISH crossover detected - alerting.
2026-08-28 14:00:18 | INFO    | macd_alert | Next check in 3599s (just after the next 1h close).
```

A different env file can be passed as the first argument:

```bash
python macd_alert.py /etc/macd-alert/eth.env
```

That is the clean way to run several instances with different pairs or
timeframes — give each its own env file and its own `STATE_FILE`.

## Backtest mode

Replays a stretch of history and lists every crossover the live loop *would*
have alerted on — same closed-candle rule, same crossover definition (both modes
call the same `classify()`), so what you see here is what you would have been
sent.

```bash
python macd_alert.py --backtest                       # last 500 candles
python macd_alert.py --backtest --since 2026-01-01
python macd_alert.py --backtest --since 90d --symbol ETH/USDT --timeframe 4h
python macd_alert.py --backtest --since 180d --csv crossovers.csv
```

| Flag | Meaning |
| --- | --- |
| `--backtest` | replay instead of monitoring |
| `--since` | window start: `2026-01-01`, an ISO8601 timestamp, or a lookback `90d` / `36h` / `4w` / `120m` |
| `--candles N` | window length in candles when `--since` is absent (default 500) |
| `--symbol` | override `TRADING_PAIR` (comma-separated for several) |
| `--timeframe` | override `TIMEFRAME` |
| `--csv PATH` | write the crossovers to CSV |
| `--notify` | also push each historical crossover to Telegram, tagged `[BACKTEST REPLAY]` |

No bot token is needed for a backtest unless you pass `--notify` — and `--notify`
is off by default so a replay of six months does not dump forty messages into
your chat. A backtest never touches `state.json`, so it cannot suppress or
trigger a live alert.

Output:

```
=====================
BTC/USDT  1h  binance
=====================
299 closed candles, 2026-08-16 04:00:00 UTC -> 2026-08-28 15:00:00 UTC
11 crossovers (6 bullish, 5 bearish)

   #  SIGNAL    CANDLE CLOSE                         PRICE          MACD        SIGNAL          HIST
   1  BULL      2026-08-16 17:00:00 UTC              83.72       -7.2974       -7.5096      0.212247
   2  BEAR      2026-08-17 21:00:00 UTC             118.62        6.0791        6.0826     -0.003529
   ...

Always-in-market flip (long on bullish, short on bearish), entered at the next candle open.
Gross arithmetic only - no fees, slippage, funding, or position sizing.
  closed trades   10
  win rate        60.0% (6W / 4L)
  compounded      +18.42%    avg per trade +1.84%
  best +14.21%    worst -8.07%
  open position   LONG from 2026-08-28 11:00:00 UTC @ 86.09 (mark +5.40%)
```

Read the flip summary for what it is: MACD crossovers priced at the **next
candle's open** (never the signal candle's own close — that would be look-ahead
bias), flipping long/short on every signal, with no fees, slippage, funding, or
position sizing. It tells you how the signal behaved on that data, nothing more.

Two things worth knowing about the numbers:

- The window is padded with extra warmup bars before `--since` so the EMAs are
  settled by the first reported candle. Crossovers inside the padding are not
  reported.
- Long windows are paged (exchanges cap one OHLCV request at 500–1500 bars).
  Paging is throttled by `enableRateLimit` and retried like any live poll, but a
  multi-year 1m replay is many requests — start with a coarser timeframe.

## Keep it running

### nohup (quick)

```bash
nohup ./.venv/bin/python macd_alert.py >> macd_alert.log 2>&1 &
```

`echo $!` gives the pid; `kill <pid>` stops it cleanly (SIGTERM is handled — the
current cycle finishes, then it exits). `tail -f macd_alert.log` to watch.

### systemd (survives reboots)

`/etc/systemd/system/macd-alert.service`:

```ini
[Unit]
Description=MACD crossover Telegram alerts
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/macd_telegram_alert
ExecStart=/opt/macd_telegram_alert/.venv/bin/python /opt/macd_telegram_alert/macd_alert.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now macd-alert
journalctl -u macd-alert -f
```

Keep `.env` readable only by the service user: `chmod 600 .env`.

### Windows

Run it as a scheduled task set to "Run whether user is logged on or not" with
the trigger "At startup", action `…\.venv\Scripts\python.exe`, argument
`macd_alert.py`, and "Start in" set to this directory.

## Error handling

- ccxt network errors and 5xx retry with exponential backoff (`MAX_RETRIES`,
  `RETRY_BASE_DELAY`); rate limits (`RateLimitExceeded`, `DDoSProtection`) back
  off 3× harder, and `enableRateLimit` throttles requests before that.
- Telegram 429 honours the `retry_after` the API returns; 5xx retries; other 4xx
  are logged once and not retried, because they mean the token or chat id is
  wrong.
- A failing cycle for one pair is logged and skipped — the loop continues and
  retries at the next candle close.
- Alert state is only recorded after Telegram accepts the message, so an alert
  lost to an outage fires again on the next poll while the crossover candle is
  still the most recent one.

### CERTIFICATE_VERIFY_FAILED

If every request dies with `unable to get local issuer certificate`, something
on the machine is intercepting TLS — a corporate proxy, or antivirus with HTTPS
scanning (Norton, Kaspersky, ESET, Bitdefender). Its CA is trusted by Windows
but not by Python's `certifi` bundle. Point Python at a bundle containing both:

```bash
python -c "import certifi,pathlib; p=pathlib.Path('ca-bundle.pem'); p.write_text(pathlib.Path(certifi.where()).read_text()+'\n'+pathlib.Path(r'C:\ProgramData\Norton\Antivirus\wscert.pem').read_text())"
export REQUESTS_CA_BUNDLE=$PWD/ca-bundle.pem      # Windows: set REQUESTS_CA_BUNDLE=%CD%\ca-bundle.pem
```

Substitute your own scanner's CA path. The script sets ccxt's
`requests_trust_env`, so `REQUESTS_CA_BUNDLE`, `HTTPS_PROXY` and `NO_PROXY` are
honoured — ccxt ignores them by default, which is what makes this failure so
confusing. For systemd, add it as `Environment=REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem`.

## Tuning

| Symptom | Change |
| --- | --- |
| Occasional missed candle | Raise `POLL_BUFFER_SECONDS` (30–60 on slower exchanges) |
| Too many signals | Longer `TIMEFRAME`; MACD whipsaws badly on 1m/5m in chop |
| Want confirmation of the values | Set `LOG_LEVEL=DEBUG` and compare against the same pair on TradingView |
