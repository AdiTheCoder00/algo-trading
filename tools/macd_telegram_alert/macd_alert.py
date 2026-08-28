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
import csv
import html
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, TypeVar

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

try:  # stdlib on 3.9+, but the IANA database itself may be missing on Windows.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

LOG = logging.getLogger("macd_alert")

T = TypeVar("T")


# --------------------------------------------------------------------------- config


def _env_str(name: str, default: Optional[str] = None, *, required: bool = False) -> str:
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

    @property
    def warmup_needed(self) -> int:
        # Two closed candles are compared, and the EMAs need room to settle.
        return self.slow_period + self.signal_period + 2


def load_config(
    env_file: Optional[str] = None,
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

    last_error: Optional[BaseException] = None
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


def classify(prev_histogram: float, histogram: float) -> Optional[str]:
    """The one and only definition of a crossover, shared by live and backtest."""
    if prev_histogram <= 0 < histogram:
        return "bullish"
    if prev_histogram >= 0 > histogram:
        return "bearish"
    return None


def _crossover_at(
    candles: pd.DataFrame, macd: pd.DataFrame, position: int
) -> Optional[Crossover]:
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


def detect_crossover(candles: pd.DataFrame, macd: pd.DataFrame) -> Optional[Crossover]:
    """Compare the last two *closed* candles and report a sign change, if any."""
    if len(macd) < 2:
        return None
    return _crossover_at(candles, macd, len(macd) - 1)


def find_crossovers(candles: pd.DataFrame, macd: pd.DataFrame) -> List[Crossover]:
    """Every crossover in the series, oldest first.

    Walks the same comparison the live loop makes, one candle at a time, so a
    replay reports exactly what the monitor would have alerted on.
    """
    found: List[Crossover] = []
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
        missing = [s for s in self.config.symbols if s not in self.exchange.markets]
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
            at = cursor
            page = with_retries(
                lambda: self.exchange.fetch_ohlcv(
                    symbol,
                    timeframe=self.config.timeframe,
                    since=at,
                    limit=self.config.candle_limit,
                ),
                what=f"fetch_ohlcv({symbol} {self.config.timeframe} @ {at})",
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
                stamp(rows[-1][0], timezone.utc),
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
        except Exception:  # precision metadata is best-effort
            return f"{price:,.8f}".rstrip("0").rstrip(".")

    def seconds_until_next_close(self) -> float:
        period = self.timeframe_seconds
        now = time.time()
        return (period - (now % period)) + self.config.poll_buffer_seconds


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
                try:
                    retry_after = int(
                        response.json().get("parameters", {}).get("retry_after", 5)
                    )
                except ValueError:
                    pass
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


# --------------------------------------------------------------------------- formatting


def resolve_timezone(name: str):
    if ZoneInfo is None or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        LOG.warning("Unknown DISPLAY_TIMEZONE %r, falling back to UTC", name)
        return timezone.utc


def stamp(ms: int, tz) -> str:
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
        f"<b>Pair:</b> {html.escape(symbol)} ({html.escape(config.exchange_id)})",
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
    market: MarketData
    notifier: TelegramNotifier
    state: StateStore
    tz: object = field(init=False)
    _stop: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.tz = resolve_timezone(self.config.display_timezone)

    def request_stop(self, *_args) -> None:
        LOG.info("Shutdown requested - finishing the current cycle.")
        self._stop = True

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

        key = f"{self.config.exchange_id}:{symbol}:{self.config.timeframe}"
        if self.state.already_alerted(key, cross.candle_open_ms):
            LOG.debug("%s: crossover already alerted for this candle.", symbol)
            return

        message = build_message(
            cross,
            symbol=symbol,
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

    def run(self) -> None:
        self.market.load_markets()
        LOG.info(
            "Watching %s on %s (%s), MACD(%d, %d, %d), closed candles only.",
            ", ".join(self.config.symbols),
            self.config.exchange_id,
            self.config.timeframe,
            self.config.fast_period,
            self.config.slow_period,
            self.config.signal_period,
        )

        if self.config.send_startup_message:
            self.notifier.send(
                "⚙️ <b>MACD monitor started</b>\n"
                f"<b>Exchange:</b> {html.escape(self.config.exchange_id)}\n"
                f"<b>Pairs:</b> {html.escape(', '.join(self.config.symbols))}\n"
                f"<b>Timeframe:</b> {html.escape(self.config.timeframe)}\n"
                f"<b>Settings:</b> MACD({self.config.fast_period}, "
                f"{self.config.slow_period}, {self.config.signal_period})"
            )

        while not self._stop:
            for symbol in self.config.symbols:
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
            # Sleep in slices so Ctrl-C / SIGTERM is honoured promptly.
            deadline = time.monotonic() + sleep_for
            while not self._stop and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

        LOG.info("Monitor stopped.")


# --------------------------------------------------------------------------- backtest


@dataclass(frozen=True)
class Trade:
    """One flip of an always-in-market position, entered on the next open."""

    direction: str  # "long" | "short"
    signal_ms: int
    entry_ms: int
    entry_price: float
    exit_ms: Optional[int]
    exit_price: Optional[float]

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def return_pct(self, mark_price: Optional[float] = None) -> Optional[float]:
        close_price = self.exit_price if self.exit_price is not None else mark_price
        if close_price is None or not self.entry_price:
            return None
        if self.direction == "long":
            return (close_price / self.entry_price - 1) * 100
        return (self.entry_price / close_price - 1) * 100


def simulate_flips(candles: pd.DataFrame, crossovers: Sequence[Crossover]) -> List[Trade]:
    """Walk the crossovers as a long/short flip, entering at the next open.

    The signal is only known once its candle has closed, so the earliest
    tradeable price is the *next* candle's open. Using the crossover candle's
    own close here would be look-ahead bias - the same mistake the live loop
    avoids by refusing to read the forming bar.
    """
    position_of = {int(ts): i for i, ts in enumerate(candles["timestamp"])}
    trades: List[Trade] = []

    for index, cross in enumerate(crossovers):
        entry_row = position_of.get(cross.candle_open_ms, -1) + 1
        if entry_row <= 0 or entry_row >= len(candles):
            continue  # signalled on the last closed candle: nothing to enter on yet

        exit_ms: Optional[int] = None
        exit_price: Optional[float] = None
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
    notifier: Optional[TelegramNotifier] = None
    tz: object = field(init=False)

    def __post_init__(self) -> None:
        self.tz = resolve_timezone(self.config.display_timezone)

    def run(
        self,
        *,
        since: Optional[str] = None,
        candles: int = 500,
        csv_path: Optional[Path] = None,
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

        rows_for_csv: List[dict] = []
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

    def _csv_rows(self, symbol: str, crossovers: Sequence[Crossover]) -> List[dict]:
        period_ms = self.market.timeframe_seconds * 1000
        return [
            {
                "exchange": self.config.exchange_id,
                "symbol": symbol,
                "timeframe": self.config.timeframe,
                "direction": cross.direction,
                "candle_open_utc": stamp(cross.candle_open_ms, timezone.utc),
                "candle_close_utc": stamp(cross.candle_open_ms + period_ms, timezone.utc),
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


def main(argv: Optional[Iterable[str]] = None) -> int:
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
    if not config.symbols:
        raise SystemExit("No trading pair: set TRADING_PAIR in .env or pass --symbol.")

    if not args.backtest:
        for flag in ("since", "csv", "notify"):
            if getattr(args, flag):
                LOG.warning("--%s only applies to --backtest; ignoring it.", flag)

    try:
        if args.backtest:
            if args.candles <= 0:
                raise SystemExit("--candles must be a positive number.")
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

        monitor = Monitor(
            config=config,
            market=MarketData(config),
            notifier=TelegramNotifier(config),
            state=StateStore(config.state_path),
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
