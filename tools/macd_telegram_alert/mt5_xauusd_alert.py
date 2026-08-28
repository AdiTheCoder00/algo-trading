#!/usr/bin/env python3
"""XAUUSD MACD crossover monitor via MetaTrader 5, alerts to Telegram.

Sibling to macd_alert.py, not a rewrite of it: the same MACD math, the same
Telegram sender, the same retry helper and state store, and - the point of
this file - the same `Monitor` scheduling loop, imported rather than
reimplemented, so a signal here and a signal there cannot disagree about
what a crossover is or how a cycle is retried. `Mt5MarketAdapter` below
exists to present an MT5 feed through the exact surface `Monitor` already
knows how to drive (ccxt's `MarketData`'s), so nothing about the live loop,
its backoff, its sleep-until-next-close scheduling, or its shutdown handling
is duplicated for a second backend.

Why not just ccxt: XAU/USD spot forex has no real presence on crypto
exchanges (PAXG/USDT, tokenized gold, is a different thing - a different
instrument tracking a similar price, not the same trade). This repo already
has a correctness-tested MT5 feed (algo.data.mt5_feed): the broker's clock is
measured against real UTC rather than assumed - a hard-coded UTC+3 is wrong
for a third of the year across DST - and the still-forming bar is always
dropped, the same no-repainting rule macd_alert.py enforces for ccxt.

No dynamic pair-switching here, unlike the crypto tool's command panel:
XAU/USD is a fixed watch, one instrument, one MT5 terminal - deliberately
out of scope, not an oversight.

Needs, unlike macd_alert.py:
  - The REPO's own venv, not this tool's isolated one - only the repo's has
    MetaTrader5 and the algo package installed. Run this with
    "..\\algo trading\\.venv\\Scripts\\python.exe" mt5_xauusd_alert.py, not
    this folder's own .venv.
  - A running, logged-in MT5 terminal with the symbol in Market Watch.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root, for `algo`
import macd_alert as base  # sibling script, not an installed package

from algo.core.bar import Timeframe
from algo.core.errors import DataError
from algo.data.mt5_feed import Mt5BarFeed, measure_server_offset

LOG = logging.getLogger("mt5_xauusd_alert")

#: MT5 timeframe label -> minutes. Matches Mt5BarFeed's supported set exactly
#: (algo/data/mt5_feed.py's _TIMEFRAME_MINUTES) minus M1, too fast for MACD
#: crossovers to mean anything on an instrument this liquid.
TIMEFRAMES: dict[str, int] = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}


def load_config(env_file: str | None = None) -> base.Config:
    """Builds the shared `base.Config` shape from MT5_*-prefixed keys, plus
    the TELEGRAM_*/MACD_*/MAX_RETRIES/RETRY_BASE_DELAY/DISPLAY_TIMEZONE/
    LOG_LEVEL/SEND_STARTUP_MESSAGE keys the crypto tool already reads from
    the SAME .env file - both scripts can point at one .env without
    colliding, because neither reads the other's keys.

    A few fields exist only to satisfy `base.Config`'s shape and mean nothing
    here: `api_key`/`api_secret`/`api_password` (MT5 needs no credentials -
    it attaches to a terminal already logged in), `enable_command_panel`
    (always off - see the module docstring), `watchlist_path` (never read
    with the panel off).
    """
    base.load_dotenv(env_file or Path(__file__).with_name(".env"), override=False)

    fast = base._env_int("MACD_FAST_PERIOD", 12)
    slow = base._env_int("MACD_SLOW_PERIOD", 26)
    signal_period = base._env_int("MACD_SIGNAL_PERIOD", 9)
    if fast <= 0 or slow <= 0 or signal_period <= 0:
        raise SystemExit("MACD periods must be positive integers.")
    if fast >= slow:
        raise SystemExit(
            f"MACD_FAST_PERIOD ({fast}) must be smaller than MACD_SLOW_PERIOD ({slow})."
        )

    timeframe_label = base._env_str("MT5_TIMEFRAME", "H1").upper()
    if timeframe_label not in TIMEFRAMES:
        raise SystemExit(
            f"MT5_TIMEFRAME must be one of {sorted(TIMEFRAMES)}, got {timeframe_label!r}."
        )

    symbol = base._env_str("MT5_SYMBOL", "XAUUSD")
    default_state = Path(__file__).with_name(f"state_{symbol.lower()}.json")

    return base.Config(
        telegram_token=base._env_str("TELEGRAM_BOT_TOKEN", required=True),
        telegram_chat_id=base._env_str("TELEGRAM_CHAT_ID", required=True),
        exchange_id=base._env_str("MT5_BROKER_LABEL", "MT5"),
        api_key="",
        api_secret="",
        api_password="",
        symbols=[symbol],
        timeframe=timeframe_label,
        fast_period=fast,
        slow_period=slow,
        signal_period=signal_period,
        candle_limit=base._env_int("MT5_CANDLE_LIMIT", 300),
        poll_buffer_seconds=base._env_float("POLL_BUFFER_SECONDS", 15.0),
        max_retries=base._env_int("MAX_RETRIES", 5),
        retry_base_delay=base._env_float("RETRY_BASE_DELAY", 2.0),
        state_path=Path(base._env_str("MT5_STATE_FILE", str(default_state))),
        display_timezone=base._env_str("DISPLAY_TIMEZONE", "UTC"),
        send_startup_message=base._env_bool("SEND_STARTUP_MESSAGE", True),
        log_level=base._env_str("LOG_LEVEL", "INFO").upper(),
        enable_command_panel=False,
        # Written once at startup (Monitor.run()'s log line reads it) but
        # never mutated - there is no command panel here to change it.
        watchlist_path=Path(__file__).with_name(f"watchlist_{symbol.lower()}.json"),
    )


class Mt5MarketAdapter:
    """Presents one MT5 symbol through `Monitor`'s `MarketData` surface.

    Single-symbol only: `Monitor` always calls with the symbol it was given,
    which is always this adapter's own - there is nothing else to route to,
    so the argument is accepted for interface compatibility and not
    consulted.
    """

    def __init__(self, config: base.Config, timeframe: Timeframe) -> None:
        self.symbol = config.symbols[0]
        self._timeframe = timeframe
        self.timeframe_seconds = timeframe.minutes * 60
        self._candle_limit = config.candle_limit
        self._max_retries = config.max_retries
        self._retry_base_delay = config.retry_base_delay
        self._poll_buffer_seconds = config.poll_buffer_seconds

    def load_markets(self) -> None:
        """Confirms the terminal is up and the symbol is real before the
        first poll - the same job ccxt's `load_markets` does: fail loudly at
        startup, not 60 minutes later on the first scheduled check.
        """
        base.with_retries(
            self._select,
            what=f"MT5 symbol_select({self.symbol})",
            attempts=self._max_retries,
            base_delay=self._retry_base_delay,
            retry_on=(DataError,),
        )

    def _select(self) -> None:
        if not mt5.initialize():
            raise DataError(f"could not attach to MT5: {mt5.last_error()}")
        try:
            if not mt5.symbol_select(self.symbol, True):
                raise DataError(
                    f"could not select {self.symbol} in Market Watch: {mt5.last_error()}"
                )
        finally:
            mt5.shutdown()

    def closed_candles(self, symbol: str) -> pd.DataFrame:
        del symbol  # part of the MarketData surface; there is only ever self.symbol
        return base.with_retries(
            self._fetch,
            what=f"MT5 closed_bars({self.symbol} {self._timeframe.label})",
            attempts=self._max_retries,
            base_delay=self._retry_base_delay,
            retry_on=(DataError,),
        )

    def _fetch(self) -> pd.DataFrame:
        if not mt5.initialize():
            raise DataError(f"could not attach to MT5: {mt5.last_error()}")
        try:
            if not mt5.symbol_select(self.symbol, True):
                raise DataError(f"could not select {self.symbol}: {mt5.last_error()}")
            offset = measure_server_offset(mt5, self.symbol)
            feed = Mt5BarFeed(
                terminal=mt5, symbol=self.symbol, timeframe=self._timeframe, server_offset=offset
            )
            bars = feed.closed_bars(count=self._candle_limit)
        finally:
            mt5.shutdown()

        return pd.DataFrame(
            {
                "timestamp": [int(b.ts.timestamp() * 1000) for b in bars],
                "close": [float(b.close) for b in bars],
            }
        )

    def format_price(self, symbol: str, price: float) -> str:
        del symbol
        return f"{price:,.2f}"

    def seconds_until_next_close(self) -> float:
        period = self.timeframe_seconds
        now = time.time()
        return (period - (now % period)) + self._poll_buffer_seconds


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    env_file = argv[0] if argv else None

    config = load_config(env_file)
    base.setup_logging(config.log_level)

    timeframe = Timeframe(minutes=TIMEFRAMES[config.timeframe])
    market = Mt5MarketAdapter(config, timeframe)
    monitor = base.Monitor(
        config=config,
        market=market,
        notifier=base.TelegramNotifier(config),
        state=base.StateStore(config.state_path),
        watchlist=base.WatchList(config.watchlist_path, initial=config.symbols),
        command_panel=None,
    )

    signal.signal(signal.SIGINT, monitor.request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, monitor.request_stop)

    try:
        monitor.run()
    except KeyboardInterrupt:
        LOG.info("Interrupted.")
        return 130
    except SystemExit:
        raise
    except Exception:
        LOG.exception("Fatal error - exiting.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
