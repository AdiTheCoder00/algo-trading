#!/usr/bin/env python3
"""MACD crossover monitor with Telegram alerts.

Polls an exchange over ccxt, computes MACD(12, 26, 9) on *closed* candles only,
and pushes a Telegram message when the MACD line crosses its signal line.

Two modes:

  live      (default)  poll forever, alert on each new crossover
  backtest  --backtest  replay a stretch of history and list every crossover
                        the live loop would have alerted on

Nothing here places an order or advises a trade - it watches an indicator and
tells you it moved.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import html
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from functools import partial
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

LOG = logging.getLogger("macd_alert")

T = TypeVar("T")


# --------------------------------------------------------------------------- config


def _env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        if required:
            raise SystemExit(
                f"Missing required environment variable {name}. "
                "Copy .env.example to .env and fill it in."
            )
        return default or ""
    return value


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise SystemExit(f"{name} must be a boolean (true/false), got {raw!r}")


@dataclass(frozen=True)
class Config:
    """Everything the monitor needs, resolved from the environment."""

    telegram_token: str
    telegram_chat_id: str
    exchange_id: str
    api_key: str
    api_secret: str
    api_password: str
    symbols: Sequence[str]
    timeframe: str
    fast_period: int
    slow_period: int
    signal_period: int
    candle_limit: int
    poll_buffer_seconds: float
    max_retries: int
    retry_base_delay: float
    state_path: Path
    display_timezone: str
    send_startup_message: bool
    log_level: str
    enable_command_panel: bool
    watchlist_path: Path
    mt5_broker_label: str

    @property
    def warmup_needed(self) -> int:
        # Two closed candles are compared, and the EMAs need room to settle.
        return self.slow_period + self.signal_period + 2


def load_config(
    env_file: str | None = None,
    *,
    require_telegram: bool = True,
    require_pair: bool = True,
) -> Config:
    """Resolve the environment into a Config.

    The `require_*` switches are relaxed for a backtest: replaying history
    should not demand a bot token, and `--symbol` should work on its own
    without a .env file to name the pair.
    """
    load_dotenv(env_file or Path(__file__).with_name(".env"), override=False)

    symbols_raw = _env_str("TRADING_PAIR", required=require_pair)
    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    if require_pair and not symbols:
        raise SystemExit("TRADING_PAIR resolved to an empty list.")

    fast = _env_int("MACD_FAST_PERIOD", 12)
    slow = _env_int("MACD_SLOW_PERIOD", 26)
    signal_period = _env_int("MACD_SIGNAL_PERIOD", 9)
    if fast <= 0 or slow <= 0 or signal_period <= 0:
        raise SystemExit("MACD periods must be positive integers.")
    if fast >= slow:
        raise SystemExit(
            f"MACD_FAST_PERIOD ({fast}) must be smaller than MACD_SLOW_PERIOD ({slow})."
        )

    default_state = Path(__file__).with_name("state.json")
    default_watchlist = Path(__file__).with_name("watchlist.json")

    return Config(
        telegram_token=_env_str("TELEGRAM_BOT_TOKEN", required=require_telegram),
        telegram_chat_id=_env_str("TELEGRAM_CHAT_ID", required=require_telegram),
        exchange_id=_env_str("EXCHANGE_ID", "binance"),
        api_key=_env_str("EXCHANGE_API_KEY"),
        api_secret=_env_str("EXCHANGE_API_SECRET"),
        api_password=_env_str("EXCHANGE_API_PASSWORD"),
        symbols=symbols,
        timeframe=_env_str("TIMEFRAME", "1h"),
        fast_period=fast,
        slow_period=slow,
        signal_period=signal_period,
        candle_limit=_env_int("CANDLE_LIMIT", 300),
        poll_buffer_seconds=_env_float("POLL_BUFFER_SECONDS", 15.0),
        max_retries=_env_int("MAX_RETRIES", 5),
        retry_base_delay=_env_float("RETRY_BASE_DELAY", 2.0),
        state_path=Path(_env_str("STATE_FILE", str(default_state))),
        display_timezone=_env_str("DISPLAY_TIMEZONE", "UTC"),
        send_startup_message=_env_bool("SEND_STARTUP_MESSAGE", True),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        enable_command_panel=_env_bool("ENABLE_COMMAND_PANEL", True),
        watchlist_path=Path(_env_str("WATCHLIST_FILE", str(default_watchlist))),
        mt5_broker_label=_env_str("MT5_BROKER_LABEL", "MT5"),
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- retries


class RetriesExhausted(RuntimeError):
    """Raised when an operation failed on every attempt."""


def with_retries(
    operation: Callable[[], T],
    *,
    what: str,
    attempts: int,
    base_delay: float,
    retry_on: tuple,
    rate_limit_on: tuple = (),
) -> T:
    """Run `operation`, backing off exponentially on the listed exceptions."""

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except rate_limit_on as exc:
            last_error = exc
            delay = base_delay * (2 ** (attempt - 1)) * 3
            LOG.warning(
                "%s rate limited (attempt %d/%d): %s - backing off %.1fs",
                what,
                attempt,
                attempts,
                exc,
                delay,
            )
        except retry_on as exc:
            last_error = exc
            delay = base_delay * (2 ** (attempt - 1))
            LOG.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                what,
                attempt,
                attempts,
                exc,
                delay,
            )
        if attempt < attempts:
            time.sleep(delay + random.uniform(0, base_delay / 2))

    raise RetriesExhausted(f"{what} failed after {attempts} attempts") from last_error


# --------------------------------------------------------------------------- indicator


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, matching what charting platforms plot."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def compute_macd(
    closes: pd.Series, fast: int, slow: int, signal_period: int
) -> pd.DataFrame:
    """Return a frame with macd / signal / histogram columns."""
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = ema(macd_line, signal_period)
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": macd_line - signal_line,
        }
    )


@dataclass(frozen=True)
class Crossover:
    direction: str  # "bullish" | "bearish"
    candle_open_ms: int
    price: float
    macd: float
    signal: float
    histogram: float
    prev_histogram: float


def classify(prev_histogram: float, histogram: float) -> str | None:
    """The one and only definition of a crossover, shared by live and backtest."""
    if prev_histogram <= 0 < histogram:
        return "bullish"
    if prev_histogram >= 0 > histogram:
        return "bearish"
    return None


def _crossover_at(
    candles: pd.DataFrame, macd: pd.DataFrame, position: int
) -> Crossover | None:
    """Build a Crossover for row `position`, comparing it with `position - 1`.

    Both rows must have a defined MACD and signal, otherwise the EMAs are still
    warming up and any "cross" is an artefact of the seed values.
    """
    prev, curr = macd.iloc[position - 1], macd.iloc[position]
    if pd.isna(prev["signal"]) or pd.isna(curr["signal"]):
        return None

    prev_hist = float(prev["histogram"])
    curr_hist = float(curr["histogram"])
    direction = classify(prev_hist, curr_hist)
    if direction is None:
        return None

    candle = candles.iloc[position]
    return Crossover(
        direction=direction,
        candle_open_ms=int(candle["timestamp"]),
        price=float(candle["close"]),
        macd=float(curr["macd"]),
        signal=float(curr["signal"]),
        histogram=curr_hist,
        prev_histogram=prev_hist,
    )


def detect_crossover(candles: pd.DataFrame, macd: pd.DataFrame) -> Crossover | None:
    """Compare the last two *closed* candles and report a sign change, if any."""
    if len(macd) < 2:
        return None
    return _crossover_at(candles, macd, len(macd) - 1)


def find_crossovers(candles: pd.DataFrame, macd: pd.DataFrame) -> list[Crossover]:
    """Every crossover in the series, oldest first.

    Walks the same comparison the live loop makes, one candle at a time, so a
    replay reports exactly what the monitor would have alerted on.
    """
    found: list[Crossover] = []
    for position in range(1, len(macd)):
        cross = _crossover_at(candles, macd, position)
        if cross is not None:
            found.append(cross)
    return found


# --------------------------------------------------------------------------- market data


class MarketData:
    """Thin ccxt wrapper that only ever hands back closed candles."""

    def __init__(self, config: Config) -> None:
        self.config = config
        try:
            exchange_class = getattr(ccxt, config.exchange_id)
        except AttributeError as exc:
            raise SystemExit(f"Unknown EXCHANGE_ID {config.exchange_id!r}") from exc

        params: dict = {
            "enableRateLimit": True,
            "timeout": 30_000,
            # ccxt sets session.trust_env = False, which silently discards
            # HTTPS_PROXY and REQUESTS_CA_BUNDLE. Behind a corporate proxy or a
            # TLS-inspecting antivirus that shows up as an unexplained
            # CERTIFICATE_VERIFY_FAILED, so honour the environment instead.
            "requests_trust_env": True,
        }
        if config.api_key:
            params["apiKey"] = config.api_key
        if config.api_secret:
            params["secret"] = config.api_secret
        if config.api_password:
            params["password"] = config.api_password

        self.exchange = exchange_class(params)
        self.timeframe_seconds = self.exchange.parse_timeframe(config.timeframe)

    def load_markets(self) -> None:
        with_retries(
            self.exchange.load_markets,
            what=f"load_markets({self.config.exchange_id})",
            attempts=self.config.max_retries,
            base_delay=self.config.retry_base_delay,
            retry_on=(ccxt.NetworkError, ccxt.ExchangeNotAvailable),
            rate_limit_on=(ccxt.RateLimitExceeded, ccxt.DDoSProtection),
        )
        # "/" is what marks a symbol as ccxt's in the unified watch list (see
        # MultiBackendMarket) - an MT5 symbol seeded in the same TRADING_PAIR
        # list is not this exchange's to validate.
        missing = [
            s for s in self.config.symbols if "/" in s and s not in self.exchange.markets
        ]
        if missing:
            raise SystemExit(
                f"{self.config.exchange_id} does not list: {', '.join(missing)}"
            )
        offered = self.exchange.timeframes or {}
        if offered and self.config.timeframe not in offered:
            raise SystemExit(
                f"{self.config.exchange_id} does not offer the {self.config.timeframe} "
                f"timeframe. Available: {', '.join(sorted(offered))}"
            )

    def closed_candles(self, symbol: str) -> pd.DataFrame:
        """Fetch OHLCV and drop the candle that is still forming.

        Repainting is prevented here and nowhere else: every downstream
        calculation sees only bars whose close is final.
        """
        raw = with_retries(
            lambda: self.exchange.fetch_ohlcv(
                symbol, timeframe=self.config.timeframe, limit=self.config.candle_limit
            ),
            what=f"fetch_ohlcv({symbol} {self.config.timeframe})",
            attempts=self.config.max_retries,
            base_delay=self.config.retry_base_delay,
            retry_on=(ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.ExchangeError),
            rate_limit_on=(ccxt.RateLimitExceeded, ccxt.DDoSProtection),
        )

        return self._closed_frame(raw)

    def fetch_history(
        self, symbol: str, since_ms: int, *, max_pages: int = 200
    ) -> pd.DataFrame:
        """Page backwards through history from `since_ms` to now.

        Exchanges cap a single OHLCV request (commonly 500-1500 bars), so a long
        replay window has to be walked. `enableRateLimit` throttles between
        pages; each page still gets the same retry treatment as a live poll.
        """
        period_ms = self.timeframe_seconds * 1000
        now_ms = self.exchange.milliseconds()
        rows: list = []
        cursor = since_ms

        for page_number in range(1, max_pages + 1):
            # partial, not a lambda: the cursor moves every iteration, and a
            # lambda would close over the variable rather than this page's value.
            fetch_page = partial(
                self.exchange.fetch_ohlcv,
                symbol,
                timeframe=self.config.timeframe,
                since=cursor,
                limit=self.config.candle_limit,
            )
            page = with_retries(
                fetch_page,
                what=f"fetch_ohlcv({symbol} {self.config.timeframe} @ {cursor})",
                attempts=self.config.max_retries,
                base_delay=self.config.retry_base_delay,
                retry_on=(
                    ccxt.NetworkError,
                    ccxt.ExchangeNotAvailable,
                    ccxt.ExchangeError,
                ),
                rate_limit_on=(ccxt.RateLimitExceeded, ccxt.DDoSProtection),
            )
            # Exchanges routinely re-send the `since` bar; keep it strictly increasing.
            fresh = [r for r in page if not rows or r[0] > rows[-1][0]]
            if not fresh:
                break

            rows.extend(fresh)
            LOG.info(
                "  page %d: %d candles, through %s",
                page_number,
                len(fresh),
                stamp(rows[-1][0], UTC),
            )
            cursor = rows[-1][0] + period_ms
            if cursor >= now_ms:
                break
        else:
            LOG.warning(
                "Stopped after %d pages - the window is longer than this script "
                "will page through in one run.",
                max_pages,
            )

        return self._closed_frame(rows)

    def _closed_frame(self, raw: Sequence) -> pd.DataFrame:
        frame = pd.DataFrame(
            list(raw), columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        if frame.empty:
            return frame

        now_ms = self.exchange.milliseconds()
        period_ms = self.timeframe_seconds * 1000
        # A bar is closed once its open time plus one period is in the past.
        return frame[frame["timestamp"] + period_ms <= now_ms].reset_index(drop=True)

    def parse_since(self, value: str) -> int:
        """Accept `2026-01-01`, an ISO8601 timestamp, or a lookback like `90d`."""
        text = value.strip()

        relative = re.fullmatch(r"(\d+)\s*([mhdw])", text, re.IGNORECASE)
        if relative:
            units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
            seconds = int(relative.group(1)) * units[relative.group(2).lower()]
            return self.exchange.milliseconds() - seconds * 1000

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text += "T00:00:00Z"
        parsed = self.exchange.parse8601(text)
        if parsed is None:
            raise SystemExit(
                f"Could not read --since {value!r}. Use 2026-01-01, "
                "2026-01-01T12:00:00Z, or a lookback like 90d / 36h / 4w."
            )
        return int(parsed)

    def format_price(self, symbol: str, price: float) -> str:
        try:
            return str(self.exchange.price_to_precision(symbol, price))
        except (ccxt.BaseError, LookupError, TypeError, ValueError):
            # Precision metadata is best-effort: a market the exchange did not
            # describe fully should not cost us the alert.
            return f"{price:,.8f}".rstrip("0").rstrip(".")

    def seconds_until_next_close(self) -> float:
        period = self.timeframe_seconds
        now = time.time()
        return (period - (now % period)) + self.config.poll_buffer_seconds

    def exchange_label(self, symbol: str) -> str:
        del symbol  # one exchange for every ccxt symbol this instance serves
        return self.config.exchange_id

    def is_valid_symbol(self, symbol: str) -> bool:
        return symbol in self.exchange.markets


# --------------------------------------------------------------------------- MT5 backend
#
# Optional: only imported if MetaTrader5 and the repo's own `algo` package are
# both importable. Neither is in this tool's own requirements.txt - the
# crypto path stays fully standalone (own venv, ccxt only) for anyone running
# this tool outside this repo, or on a platform MetaTrader5 does not ship for.
# Where they ARE available (this repo's venv, on Windows, with a logged-in
# terminal), `/watch` can route a non-ccxt symbol like XAUUSD here instead.

try:
    import MetaTrader5 as mt5

    _repo_root = Path(__file__).resolve().parent.parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from algo.core.bar import Timeframe as _Mt5Timeframe
    from algo.core.errors import DataError as _Mt5DataError
    from algo.data.mt5_feed import Mt5BarFeed as _Mt5BarFeed
    from algo.data.mt5_feed import measure_server_offset as _measure_server_offset

    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

#: MT5 timeframe minutes Mt5BarFeed supports (algo/data/mt5_feed.py's
#: _TIMEFRAME_MINUTES). A `TIMEFRAME` that does not divide evenly into one of
#: these cannot be served by MT5 - not fatal, just means MT5 symbols are
#: unavailable for this run (crypto symbols are unaffected).
MT5_TIMEFRAME_MINUTES = frozenset({1, 5, 15, 30, 60, 240})


class Mt5Source:
    """MT5-backed candles for any symbol the terminal can select - not fixed
    to one, so it can serve whatever `/watch` sends its way. Connects and
    disconnects per call, matching this repo's own convention
    (scripts/measure_macd_xauusd.py): simpler than holding a session open,
    and a poll only happens once a timeframe period.
    """

    def __init__(
        self,
        timeframe_minutes: int,
        *,
        candle_limit: int,
        max_retries: int,
        retry_base_delay: float,
        poll_buffer_seconds: float,
        broker_label: str,
    ) -> None:
        self._timeframe = _Mt5Timeframe(minutes=timeframe_minutes)
        self.timeframe_seconds = timeframe_minutes * 60
        self._candle_limit = candle_limit
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._poll_buffer_seconds = poll_buffer_seconds
        self._broker_label = broker_label

    def exchange_label(self, symbol: str) -> str:
        del symbol
        return self._broker_label

    def is_valid_symbol(self, symbol: str) -> bool:
        if not mt5.initialize():
            return False
        try:
            return bool(mt5.symbol_select(symbol, True))
        finally:
            mt5.shutdown()

    def closed_candles(self, symbol: str) -> pd.DataFrame:
        return with_retries(
            lambda: self._fetch(symbol),
            what=f"MT5 closed_bars({symbol} {self._timeframe.label})",
            attempts=self._max_retries,
            base_delay=self._retry_base_delay,
            retry_on=(_Mt5DataError,),
        )

    def _fetch(self, symbol: str) -> pd.DataFrame:
        if not mt5.initialize():
            raise _Mt5DataError(f"could not attach to MT5: {mt5.last_error()}")
        try:
            if not mt5.symbol_select(symbol, True):
                raise _Mt5DataError(f"could not select {symbol}: {mt5.last_error()}")
            offset = _measure_server_offset(mt5, symbol)
            feed = _Mt5BarFeed(
                terminal=mt5, symbol=symbol, timeframe=self._timeframe, server_offset=offset
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


class MultiBackendMarket:
    """Routes each symbol to ccxt or MT5 by shape: a "/" is what every ccxt
    symbol has and no MT5 symbol does (BTC/USDT vs XAUUSD), so the format
    IS the routing decision - no separate registry to keep in sync, and a
    typo just fails validation on whichever side it happens to resemble.

    One timeframe for the whole watch list, not one per symbol or per
    backend: `Monitor` schedules a single poll cadence, and `build_message`
    reports a single period, so both backends are constructed from the same
    resolved `Config.timeframe`. If MT5 cannot serve that period (a ccxt-only
    timeframe like `3m`), MT5 symbols are simply unavailable for this run -
    crypto symbols still work, and `/watch` on an MT5 symbol explains why.
    """

    def __init__(self, ccxt_market: MarketData, mt5_source: Mt5Source | None) -> None:
        self._ccxt = ccxt_market
        self._mt5 = mt5_source
        self.timeframe_seconds = ccxt_market.timeframe_seconds

    @staticmethod
    def is_ccxt_symbol(symbol: str) -> bool:
        return "/" in symbol

    def _backend_for(self, symbol: str) -> MarketData | Mt5Source | None:
        return self._ccxt if self.is_ccxt_symbol(symbol) else self._mt5

    def load_markets(self) -> None:
        self._ccxt.load_markets()
        if self._mt5 is None:
            return
        if mt5.initialize():
            mt5.shutdown()
            LOG.info("MT5 terminal reachable - symbols without a \"/\" route there.")
        else:
            LOG.warning(
                "MT5 configured but the terminal is not reachable right now "
                "(%s). Crypto symbols are unaffected; an MT5 /watch will retry "
                "when it is next polled.",
                mt5.last_error(),
            )

    def closed_candles(self, symbol: str) -> pd.DataFrame:
        backend = self._backend_for(symbol)
        if backend is None:
            # Not `_Mt5DataError`: that name only exists when MT5_AVAILABLE,
            # which is exactly the case that can put us here.
            raise RuntimeError(f"{symbol}: MT5 is not available in this run.")
        return backend.closed_candles(symbol)

    def format_price(self, symbol: str, price: float) -> str:
        backend = self._backend_for(symbol)
        if backend is None:
            return f"{price:,.2f}"
        return backend.format_price(symbol, price)

    def exchange_label(self, symbol: str) -> str:
        backend = self._backend_for(symbol)
        if backend is None:
            return "MT5 (unavailable)"
        return backend.exchange_label(symbol)

    def is_valid_symbol(self, symbol: str) -> bool:
        backend = self._backend_for(symbol)
        if backend is None:
            return False
        return backend.is_valid_symbol(symbol)

    def seconds_until_next_close(self) -> float:
        # One shared schedule (see the class docstring) - the ccxt side's is
        # as good as either, since both resolve from the same Config.timeframe.
        return self._ccxt.seconds_until_next_close()


# --------------------------------------------------------------------------- telegram


class TelegramNotifier:
    """Bot API sender. Plain `requests` - no event loop to babysit."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.url = f"https://api.telegram.org/bot{config.telegram_token}/sendMessage"
        self.session = requests.Session()

    def send(self, text: str) -> bool:
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        def _post() -> bool:
            response = self.session.post(self.url, data=payload, timeout=20)
            if response.status_code == 429:
                retry_after = 5
                # JSONDecodeError subclasses ValueError; a non-object body
                # reaches .get as an AttributeError.
                with contextlib.suppress(ValueError, AttributeError, TypeError):
                    retry_after = int(
                        response.json().get("parameters", {}).get("retry_after", 5)
                    )
                LOG.warning("Telegram rate limited, sleeping %ss", retry_after)
                time.sleep(retry_after)
                raise requests.RequestException("Telegram 429")
            if response.status_code >= 500:
                raise requests.RequestException(
                    f"Telegram {response.status_code}: {response.text[:200]}"
                )
            if not response.ok:
                # A 4xx other than 429 is a config problem; retrying will not fix it.
                LOG.error(
                    "Telegram rejected the message (%s): %s",
                    response.status_code,
                    response.text[:300],
                )
                return False
            return True

        try:
            return with_retries(
                _post,
                what="telegram sendMessage",
                attempts=self.config.max_retries,
                base_delay=self.config.retry_base_delay,
                retry_on=(requests.RequestException,),
            )
        except RetriesExhausted:
            LOG.error("Giving up on this Telegram message after all retries.")
            return False


# --------------------------------------------------------------------------- state


class StateStore:
    """Remembers the last alerted candle so a restart does not re-fire it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = {
                str(k): int(v) for k, v in json.loads(self.path.read_text()).items()
            }
            LOG.info(
                "Loaded alert state for %d series from %s", len(self._data), self.path
            )
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            LOG.warning("Ignoring unreadable state file %s: %s", self.path, exc)
            self._data = {}

    def already_alerted(self, key: str, candle_open_ms: int) -> bool:
        return self._data.get(key, 0) >= candle_open_ms

    def record(self, key: str, candle_open_ms: int) -> None:
        self._data[key] = candle_open_ms
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)
        except OSError as exc:
            LOG.warning("Could not persist state to %s: %s", self.path, exc)


# --------------------------------------------------------------------------- watch list


class WatchList:
    """The symbols currently being polled - mutable at runtime via Telegram
    commands, unlike the rest of `Config`. Persisted so a restart keeps
    whatever the owner last set rather than reverting to `TRADING_PAIR`, which
    becomes only the first-ever bootstrap.

    One lock, `list` in and out through it: contention is a handful of
    Telegram commands, never a hot path, so simplicity wins over anything
    fancier.
    """

    def __init__(self, path: Path, initial: Sequence[str]) -> None:
        self._path = path
        self._lock = threading.Lock()
        loaded = self._load()
        self._symbols: list[str] = loaded if loaded is not None else list(initial)
        if loaded is None:
            self._save()

    def _load(self) -> list[str] | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("Ignoring unreadable watch list %s: %s", self._path, exc)
            return None
        if isinstance(data, list) and all(isinstance(s, str) for s in data):
            return data
        LOG.warning("Ignoring malformed watch list %s (expected a list of strings).", self._path)
        return None

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._symbols, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            LOG.warning("Could not persist watch list to %s: %s", self._path, exc)

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    def add(self, symbol: str) -> bool:
        """Returns False if `symbol` was already being watched."""
        with self._lock:
            if symbol in self._symbols:
                return False
            self._symbols.append(symbol)
            self._save()
            return True

    def remove(self, symbol: str) -> bool:
        """Returns False if `symbol` was not being watched."""
        with self._lock:
            if symbol not in self._symbols:
                return False
            self._symbols.remove(symbol)
            self._save()
            return True


# --------------------------------------------------------------------------- command panel


class CommandPanel:
    """Telegram commands that change what the live monitor watches, with no
    restart. Locked to `TELEGRAM_CHAT_ID`: a command from any other chat is
    read - so its `update_id` still advances the offset and is never
    reprocessed - but never acted on. The bot's username being discoverable
    must not be enough for a stranger to redirect what it watches.
    """

    #: Telegram holds the connection open until a message arrives or this
    #: elapses, so an idle bot costs one held connection, not one request a
    #: second the way a short-poll loop would.
    POLL_TIMEOUT_S = 25

    def __init__(
        self,
        config: Config,
        notifier: TelegramNotifier,
        watchlist: WatchList,
        market: MarketData | MultiBackendMarket,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._watchlist = watchlist
        self._market = market
        self._offset: int | None = None
        self._session = requests.Session()

    def poll_once(self) -> None:
        """One `getUpdates` call - blocks up to `POLL_TIMEOUT_S` if nothing is
        waiting. Meant to be the thing the command-panel thread spends its
        life inside."""
        params: dict[str, int] = {"timeout": self.POLL_TIMEOUT_S}
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            response = self._session.get(
                f"https://api.telegram.org/bot{self._config.telegram_token}/getUpdates",
                params=params,
                timeout=self.POLL_TIMEOUT_S + 10,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Command panel: getUpdates failed: %s", exc)
            time.sleep(5)
            return

        if not data.get("ok"):
            LOG.warning("Command panel: getUpdates rejected: %s", data)
            time.sleep(5)
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            self._handle(update)

    def _handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        if str(chat.get("id", "")) != str(self._config.telegram_chat_id):
            LOG.warning(
                "Command panel: ignoring %r from chat %s (not the owner).",
                text.split()[0],
                chat.get("id"),
            )
            return

        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()  # strip a /cmd@botname suffix
        args = parts[1:]

        handlers = {
            "/watch": self._cmd_watch,
            "/unwatch": self._cmd_unwatch,
            "/list": lambda _args: self._cmd_list(),
            "/status": lambda _args: self._cmd_list(),
            "/help": lambda _args: self._cmd_help(),
            "/start": lambda _args: self._cmd_help(),
        }
        handler = handlers.get(command)
        if handler is None:
            self._reply(f"Unknown command: {html.escape(command)}. Try /help.")
            return
        handler(args)

    def _cmd_watch(self, args: list[str]) -> None:
        if not args:
            self._reply("Usage: /watch SYMBOL (e.g. /watch ETH/USDT or /watch XAUUSD)")
            return
        symbol = args[0].upper()
        if not self._market.is_valid_symbol(symbol):
            self._reply(
                f"{html.escape(symbol)} is not available on "
                f"{html.escape(self._market.exchange_label(symbol))}."
            )
            return
        if self._watchlist.add(symbol):
            self._reply(f"Now watching {html.escape(symbol)}.")
        else:
            self._reply(f"Already watching {html.escape(symbol)}.")

    def _cmd_unwatch(self, args: list[str]) -> None:
        if not args:
            self._reply("Usage: /unwatch SYMBOL")
            return
        symbol = args[0].upper()
        if self._watchlist.remove(symbol):
            self._reply(f"Stopped watching {html.escape(symbol)}.")
        else:
            self._reply(f"Wasn't watching {html.escape(symbol)}.")

    def _cmd_list(self) -> None:
        symbols = self._watchlist.symbols()
        if not symbols:
            self._reply("Not watching anything. /watch SYMBOL to start.")
            return
        body = "\n".join(f"- {html.escape(s)}" for s in symbols)
        self._reply(f"<b>Watching</b> ({html.escape(self._config.timeframe)}):\n{body}")

    def _cmd_help(self) -> None:
        self._reply(
            "<b>Commands</b>\n"
            "/watch SYMBOL - add a pair, e.g. /watch ETH/USDT\n"
            "/unwatch SYMBOL - remove a pair\n"
            "/list - show what's being watched\n"
            "/help - this message"
        )

    def _reply(self, text: str) -> None:
        self._notifier.send(text)


# --------------------------------------------------------------------------- formatting


def resolve_timezone(name: str) -> tzinfo:
    if name.upper() == "UTC":
        return UTC
    try:
        # ZoneInfoNotFoundError subclasses KeyError, and is what a Windows box
        # without the tzdata package raises for every name.
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        LOG.warning("Unknown DISPLAY_TIMEZONE %r, falling back to UTC", name)
        return UTC


def stamp(ms: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_num(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 100:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def build_message(
    cross: Crossover,
    *,
    symbol: str,
    exchange_label: str,
    price_text: str,
    config: Config,
    period_seconds: int,
    tz,
) -> str:
    bullish = cross.direction == "bullish"
    icon = "\U0001f7e2" if bullish else "\U0001f534"
    headline = "BULLISH" if bullish else "BEARISH"
    relation = "crossed ABOVE" if bullish else "crossed BELOW"
    close_ms = cross.candle_open_ms + period_seconds * 1000

    lines = [
        f"{icon} <b>{headline} MACD crossover</b>",
        "",
        f"<b>Pair:</b> {html.escape(symbol)} ({html.escape(exchange_label)})",
        f"<b>Timeframe:</b> {html.escape(config.timeframe)}",
        f"<b>Crossover price:</b> {html.escape(price_text)}",
        "",
        f"MACD line {relation} the signal line.",
        f"<b>MACD:</b> {fmt_num(cross.macd)}",
        f"<b>Signal:</b> {fmt_num(cross.signal)}",
        f"<b>Histogram:</b> {fmt_num(cross.prev_histogram)} → {fmt_num(cross.histogram)}",
        "",
        f"<b>Candle close:</b> {html.escape(stamp(close_ms, tz))}",
        f"<b>Detected:</b> {html.escape(stamp(int(time.time() * 1000), tz))}",
        "",
        f"<i>MACD({config.fast_period}, {config.slow_period}, {config.signal_period}) "
        "on confirmed closes.</i>",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- monitor


@dataclass
class Monitor:
    config: Config
    market: MarketData | MultiBackendMarket
    notifier: TelegramNotifier
    state: StateStore
    watchlist: WatchList
    command_panel: CommandPanel | None = None
    tz: object = field(init=False)
    _stop: bool = field(default=False, init=False)
    _panel_thread: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.tz = resolve_timezone(self.config.display_timezone)

    def request_stop(self, *_args) -> None:
        LOG.info("Shutdown requested - finishing the current cycle.")
        self._stop = True

    def _run_command_panel(self) -> None:
        assert self.command_panel is not None
        while not self._stop:
            try:
                self.command_panel.poll_once()
            except Exception:  # the panel is a convenience; it must not take the monitor down
                LOG.exception("Command panel: unexpected error - retrying.")
                time.sleep(5)

    def check_symbol(self, symbol: str) -> None:
        candles = self.market.closed_candles(symbol)
        if len(candles) < self.config.warmup_needed:
            LOG.warning(
                "%s %s: only %d closed candles, need %d - skipping this cycle.",
                symbol,
                self.config.timeframe,
                len(candles),
                self.config.warmup_needed,
            )
            return

        macd = compute_macd(
            candles["close"].astype(float),
            self.config.fast_period,
            self.config.slow_period,
            self.config.signal_period,
        )
        latest = macd.iloc[-1]
        LOG.info(
            "%s %s | close=%s macd=%s signal=%s hist=%s (candle %s)",
            symbol,
            self.config.timeframe,
            self.market.format_price(symbol, float(candles.iloc[-1]["close"])),
            fmt_num(float(latest["macd"])),
            fmt_num(float(latest["signal"])),
            fmt_num(float(latest["histogram"])),
            stamp(int(candles.iloc[-1]["timestamp"]), self.tz),
        )

        cross = detect_crossover(candles, macd)
        if cross is None:
            return

        exchange_label = self.market.exchange_label(symbol)
        key = f"{exchange_label}:{symbol}:{self.config.timeframe}"
        if self.state.already_alerted(key, cross.candle_open_ms):
            LOG.debug("%s: crossover already alerted for this candle.", symbol)
            return

        message = build_message(
            cross,
            symbol=symbol,
            exchange_label=exchange_label,
            price_text=self.market.format_price(symbol, cross.price),
            config=self.config,
            period_seconds=self.market.timeframe_seconds,
            tz=self.tz,
        )
        LOG.info(
            "%s: %s crossover detected - alerting.", symbol, cross.direction.upper()
        )
        if self.notifier.send(message):
            self.state.record(key, cross.candle_open_ms)

    def _pairs_with_labels(self) -> list[str]:
        return [f"{s} ({self.market.exchange_label(s)})" for s in self.watchlist.symbols()]

    def run(self) -> None:
        self.market.load_markets()
        LOG.info(
            "Watching %s (%s), MACD(%d, %d, %d), closed candles only.",
            ", ".join(self._pairs_with_labels()) or "nothing yet",
            self.config.timeframe,
            self.config.fast_period,
            self.config.slow_period,
            self.config.signal_period,
        )

        if self.config.send_startup_message:
            panel_line = (
                "\nSend /help to add or remove pairs." if self.command_panel is not None else ""
            )
            pairs = html.escape(", ".join(self._pairs_with_labels()) or "(none yet)")
            self.notifier.send(
                "⚙️ <b>MACD monitor started</b>\n"
                f"<b>Pairs:</b> {pairs}\n"
                f"<b>Timeframe:</b> {html.escape(self.config.timeframe)}\n"
                f"<b>Settings:</b> MACD({self.config.fast_period}, "
                f"{self.config.slow_period}, {self.config.signal_period})"
                f"{panel_line}"
            )

        if self.command_panel is not None:
            self._panel_thread = threading.Thread(
                target=self._run_command_panel, name="telegram-commands", daemon=True
            )
            self._panel_thread.start()
            LOG.info("Command panel listening (owner chat only) - try /help.")

        while not self._stop:
            symbols = self.watchlist.symbols()
            if not symbols:
                LOG.info("Watch list is empty - waiting for /watch.")
            for symbol in symbols:
                if self._stop:
                    break
                try:
                    self.check_symbol(symbol)
                except RetriesExhausted as exc:
                    LOG.error("%s: %s - will try again next cycle.", symbol, exc)
                except ccxt.BaseError as exc:
                    LOG.error("%s: exchange error: %s", symbol, exc)
                except Exception:  # keep the loop alive; a bad cycle is not fatal
                    LOG.exception("%s: unexpected error during check.", symbol)

            if self._stop:
                break

            sleep_for = self.market.seconds_until_next_close()
            LOG.info(
                "Next check in %.0fs (just after the next %s close).",
                sleep_for,
                self.config.timeframe,
            )
            # Sleep in slices so Ctrl-C / SIGTERM is honoured promptly, and so a
            # /watch command lands in the very next cycle rather than after a
            # sleep already in progress when it arrived.
            deadline = time.monotonic() + sleep_for
            while not self._stop and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

        if self._panel_thread is not None:
            self._panel_thread.join(timeout=CommandPanel.POLL_TIMEOUT_S + 15)
        LOG.info("Monitor stopped.")


# --------------------------------------------------------------------------- backtest


@dataclass(frozen=True)
class Trade:
    """One flip of an always-in-market position, entered on the next open."""

    direction: str  # "long" | "short"
    signal_ms: int
    entry_ms: int
    entry_price: float
    exit_ms: int | None
    exit_price: float | None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def return_pct(self, mark_price: float | None = None) -> float | None:
        close_price = self.exit_price if self.exit_price is not None else mark_price
        if close_price is None or not self.entry_price:
            return None
        if self.direction == "long":
            return (close_price / self.entry_price - 1) * 100
        return (self.entry_price / close_price - 1) * 100


def simulate_flips(candles: pd.DataFrame, crossovers: Sequence[Crossover]) -> list[Trade]:
    """Walk the crossovers as a long/short flip, entering at the next open.

    The signal is only known once its candle has closed, so the earliest
    tradeable price is the *next* candle's open. Using the crossover candle's
    own close here would be look-ahead bias - the same mistake the live loop
    avoids by refusing to read the forming bar.
    """
    position_of = {int(ts): i for i, ts in enumerate(candles["timestamp"])}
    trades: list[Trade] = []

    for index, cross in enumerate(crossovers):
        entry_row = position_of.get(cross.candle_open_ms, -1) + 1
        if entry_row <= 0 or entry_row >= len(candles):
            continue  # signalled on the last closed candle: nothing to enter on yet

        exit_ms: int | None = None
        exit_price: float | None = None
        if index + 1 < len(crossovers):
            exit_row = position_of.get(crossovers[index + 1].candle_open_ms, -1) + 1
            if 0 < exit_row < len(candles):
                exit_ms = int(candles.iloc[exit_row]["timestamp"])
                exit_price = float(candles.iloc[exit_row]["open"])

        trades.append(
            Trade(
                direction="long" if cross.direction == "bullish" else "short",
                signal_ms=cross.candle_open_ms,
                entry_ms=int(candles.iloc[entry_row]["timestamp"]),
                entry_price=float(candles.iloc[entry_row]["open"]),
                exit_ms=exit_ms,
                exit_price=exit_price,
            )
        )

    return trades


@dataclass
class Backtester:
    """Replays history and prints every crossover the live loop would alert on."""

    config: Config
    market: MarketData
    notifier: TelegramNotifier | None = None
    tz: object = field(init=False)

    def __post_init__(self) -> None:
        self.tz = resolve_timezone(self.config.display_timezone)

    def run(
        self,
        *,
        since: str | None = None,
        candles: int = 500,
        csv_path: Path | None = None,
        notify: bool = False,
    ) -> int:
        self.market.load_markets()

        period_ms = self.market.timeframe_seconds * 1000
        now_ms = self.market.exchange.milliseconds()
        report_from = (
            self.market.parse_since(since) if since else now_ms - candles * period_ms
        )
        # Fetch extra bars before the window so the EMAs are settled by the time
        # the first reported candle arrives.
        warmup_bars = max(self.config.warmup_needed * 3, 100)
        fetch_from = report_from - warmup_bars * period_ms

        LOG.info(
            "Backtest %s on %s (%s), MACD(%d, %d, %d), window from %s (+%d warmup bars).",
            ", ".join(self.config.symbols),
            self.config.exchange_id,
            self.config.timeframe,
            self.config.fast_period,
            self.config.slow_period,
            self.config.signal_period,
            stamp(report_from, self.tz),
            warmup_bars,
        )

        rows_for_csv: list[dict] = []
        total = 0

        for symbol in self.config.symbols:
            frame = self.market.fetch_history(symbol, fetch_from)
            if len(frame) < self.config.warmup_needed:
                LOG.error(
                    "%s: only %d closed candles available, need %d - skipping.",
                    symbol,
                    len(frame),
                    self.config.warmup_needed,
                )
                continue

            macd = compute_macd(
                frame["close"].astype(float),
                self.config.fast_period,
                self.config.slow_period,
                self.config.signal_period,
            )
            in_window = [
                cross
                for cross in find_crossovers(frame, macd)
                if cross.candle_open_ms >= report_from
            ]
            total += len(in_window)

            self._print_report(symbol, frame, macd, in_window, report_from)
            rows_for_csv.extend(self._csv_rows(symbol, in_window))

            if notify and in_window:
                self._replay_alerts(symbol, in_window)

        if csv_path is not None:
            self._write_csv(csv_path, rows_for_csv)

        return total

    # -- output ------------------------------------------------------------

    def _print_report(
        self,
        symbol: str,
        frame: pd.DataFrame,
        macd: pd.DataFrame,
        crossovers: Sequence[Crossover],
        report_from: int,
    ) -> None:
        period_ms = self.market.timeframe_seconds * 1000
        window = frame[frame["timestamp"] >= report_from]
        header = f"{symbol}  {self.config.timeframe}  {self.config.exchange_id}"

        print()
        print("=" * len(header))
        print(header)
        print("=" * len(header))

        if window.empty:
            print("No closed candles in the requested window.")
            return

        print(
            f"{len(window)} closed candles, "
            f"{stamp(int(window.iloc[0]['timestamp']), self.tz)} -> "
            f"{stamp(int(window.iloc[-1]['timestamp']) + period_ms, self.tz)}"
        )

        if not crossovers:
            print("No crossovers in this window.")
            return

        bullish = sum(1 for c in crossovers if c.direction == "bullish")
        print(
            f"{len(crossovers)} crossovers "
            f"({bullish} bullish, {len(crossovers) - bullish} bearish)"
        )
        print()
        print(
            f"{'#':>4}  {'SIGNAL':<8}  {'CANDLE CLOSE':<26}  "
            f"{'PRICE':>14}  {'MACD':>12}  {'SIGNAL':>12}  {'HIST':>12}"
        )
        for number, cross in enumerate(crossovers, start=1):
            print(
                f"{number:>4}  "
                f"{('BULL' if cross.direction == 'bullish' else 'BEAR'):<8}  "
                f"{stamp(cross.candle_open_ms + period_ms, self.tz):<26}  "
                f"{self.market.format_price(symbol, cross.price):>14}  "
                f"{fmt_num(cross.macd):>12}  "
                f"{fmt_num(cross.signal):>12}  "
                f"{fmt_num(cross.histogram):>12}"
            )

        self._print_flip_summary(symbol, frame, crossovers)

    def _print_flip_summary(
        self, symbol: str, frame: pd.DataFrame, crossovers: Sequence[Crossover]
    ) -> None:
        trades = simulate_flips(frame, crossovers)
        if not trades:
            return

        last_close = float(frame.iloc[-1]["close"])
        closed = [t for t in trades if not t.is_open]
        returns = [t.return_pct() for t in closed]
        returns = [r for r in returns if r is not None]

        print()
        print(
            "Always-in-market flip (long on bullish, short on bearish), entered at "
            "the next candle open."
        )
        print(
            "Gross arithmetic only - no fees, slippage, funding, or position sizing."
        )

        if returns:
            wins = [r for r in returns if r > 0]
            equity = 1.0
            for r in returns:
                equity *= 1 + r / 100
            print(f"  closed trades   {len(returns)}")
            print(
                f"  win rate        {len(wins) / len(returns) * 100:.1f}% "
                f"({len(wins)}W / {len(returns) - len(wins)}L)"
            )
            print(
                f"  compounded      {(equity - 1) * 100:+.2f}%    "
                f"avg per trade {sum(returns) / len(returns):+.2f}%"
            )
            print(f"  best {max(returns):+.2f}%    worst {min(returns):+.2f}%")
        else:
            print("  no completed round trips in this window")

        open_trade = next((t for t in trades if t.is_open), None)
        if open_trade is not None:
            mark = open_trade.return_pct(last_close)
            print(
                f"  open position   {open_trade.direction.upper()} from "
                f"{stamp(open_trade.entry_ms, self.tz)} @ "
                f"{self.market.format_price(symbol, open_trade.entry_price)}"
                + (f" (mark {mark:+.2f}%)" if mark is not None else "")
            )

    def _csv_rows(self, symbol: str, crossovers: Sequence[Crossover]) -> list[dict]:
        period_ms = self.market.timeframe_seconds * 1000
        return [
            {
                "exchange": self.config.exchange_id,
                "symbol": symbol,
                "timeframe": self.config.timeframe,
                "direction": cross.direction,
                "candle_open_utc": stamp(cross.candle_open_ms, UTC),
                "candle_close_utc": stamp(cross.candle_open_ms + period_ms, UTC),
                "candle_open_ms": cross.candle_open_ms,
                "price": cross.price,
                "macd": cross.macd,
                "signal": cross.signal,
                "histogram": cross.histogram,
                "prev_histogram": cross.prev_histogram,
            }
            for cross in crossovers
        ]

    def _write_csv(self, path: Path, rows: Sequence[dict]) -> None:
        if not rows:
            LOG.warning("Nothing to write to %s - no crossovers found.", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        LOG.info("Wrote %d crossovers to %s", len(rows), path)

    def _replay_alerts(self, symbol: str, crossovers: Sequence[Crossover]) -> None:
        if self.notifier is None:
            LOG.error("--notify needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
            return
        LOG.warning(
            "Replaying %d historical alerts for %s to Telegram.",
            len(crossovers),
            symbol,
        )
        for cross in crossovers:
            message = "🕗 <b>[BACKTEST REPLAY]</b>\n\n" + build_message(
                cross,
                symbol=symbol,
                exchange_label=self.market.exchange_label(symbol),
                price_text=self.market.format_price(symbol, cross.price),
                config=self.config,
                period_seconds=self.market.timeframe_seconds,
                tz=self.tz,
            )
            self.notifier.send(message)
            time.sleep(0.5)  # stay well under Telegram's burst limit


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macd_alert.py",
        description="MACD crossover monitor with Telegram alerts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  macd_alert.py                              live monitor\n"
            "  macd_alert.py eth.env                      live, alternate env file\n"
            "  macd_alert.py --backtest                   replay the last 500 candles\n"
            "  macd_alert.py --backtest --since 2026-01-01 --csv out.csv\n"
            "  macd_alert.py --backtest --since 90d --symbol ETH/USDT --timeframe 4h\n"
        ),
    )
    parser.add_argument(
        "env_file",
        nargs="?",
        help="path to the .env file (default: .env next to this script)",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="replay historical crossovers instead of monitoring live",
    )
    parser.add_argument(
        "--since",
        help="backtest window start: 2026-01-01, an ISO8601 timestamp, "
        "or a lookback like 90d / 36h / 4w",
    )
    parser.add_argument(
        "--candles",
        type=int,
        default=500,
        help="backtest window length in candles when --since is not given (default: 500)",
    )
    parser.add_argument(
        "--symbol",
        help="override TRADING_PAIR (comma-separated for several)",
    )
    parser.add_argument("--timeframe", help="override TIMEFRAME")
    parser.add_argument(
        "--csv", type=Path, help="write the backtest crossovers to this CSV file"
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="also send each historical crossover to Telegram (off by default, "
        "so a replay does not spam the chat)",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(
        list(argv) if argv is not None else sys.argv[1:]
    )

    # A backtest that sends nothing has no use for a bot token, and --symbol
    # makes TRADING_PAIR redundant.
    config = load_config(
        args.env_file,
        require_telegram=(not args.backtest) or args.notify,
        require_pair=not args.symbol,
    )
    setup_logging(config.log_level)

    if args.symbol:
        symbols = [s.strip() for s in args.symbol.split(",") if s.strip()]
        if not symbols:
            raise SystemExit("--symbol resolved to an empty list.")
        config = replace(config, symbols=symbols)
    if args.timeframe:
        config = replace(config, timeframe=args.timeframe)
    # A backtest and a panel-less monitor have no other way to learn a pair; a
    # monitor with the command panel on can start empty and bootstrap via
    # /watch, or pick up whatever watchlist.json already has from last time.
    if not config.symbols and (args.backtest or not config.enable_command_panel):
        raise SystemExit(
            "No trading pair: set TRADING_PAIR in .env, pass --symbol, or enable "
            "ENABLE_COMMAND_PANEL and /watch one once it's running."
        )

    if not args.backtest:
        for flag in ("since", "csv", "notify"):
            if getattr(args, flag):
                LOG.warning("--%s only applies to --backtest; ignoring it.", flag)

    if args.backtest and args.candles <= 0:
        raise SystemExit("--candles must be a positive number.")

    try:
        if args.backtest:
            backtester = Backtester(
                config=config,
                market=MarketData(config),
                notifier=TelegramNotifier(config) if args.notify else None,
            )
            found = backtester.run(
                since=args.since,
                candles=args.candles,
                csv_path=args.csv,
                notify=args.notify,
            )
            print()
            LOG.info("Backtest complete: %d crossovers across all pairs.", found)
            return 0

        ccxt_market = MarketData(config)
        mt5_source: Mt5Source | None = None
        if MT5_AVAILABLE:
            mt5_minutes = ccxt_market.timeframe_seconds // 60
            if mt5_minutes in MT5_TIMEFRAME_MINUTES:
                mt5_source = Mt5Source(
                    mt5_minutes,
                    candle_limit=config.candle_limit,
                    max_retries=config.max_retries,
                    retry_base_delay=config.retry_base_delay,
                    poll_buffer_seconds=config.poll_buffer_seconds,
                    broker_label=config.mt5_broker_label,
                )
            else:
                LOG.warning(
                    "TIMEFRAME %s (%d min) is not one MT5 offers %s - MT5 symbols "
                    "(anything without a \"/\") will not be watchable this run.",
                    config.timeframe,
                    mt5_minutes,
                    sorted(MT5_TIMEFRAME_MINUTES),
                )
        market = MultiBackendMarket(ccxt_market, mt5_source)
        notifier = TelegramNotifier(config)
        watchlist = WatchList(config.watchlist_path, initial=config.symbols)
        monitor = Monitor(
            config=config,
            market=market,
            notifier=notifier,
            state=StateStore(config.state_path),
            watchlist=watchlist,
            command_panel=(
                CommandPanel(config, notifier, watchlist, market)
                if config.enable_command_panel
                else None
            ),
        )
        signal.signal(signal.SIGINT, monitor.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, monitor.request_stop)
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
