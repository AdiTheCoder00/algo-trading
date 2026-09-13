#!/usr/bin/env python3
"""Telegram alerts for the Fibonacci-pivot / five-EMA cascade, on M5.

Watches a list of symbols on the 5-minute chart and sends a Telegram message
the moment the rule `algo/strategy/pivot_ema_cascade.py` implements fires:

    SELL  a candle closes down through R3, R2, R1 or the pivot, then down
          through the 10, 20, 50, 100 and finally the 200 EMA - after, or on
          the same candle - and the close beyond the 200 is the entry.
    EXIT  the first candle that closes back above BOTH the 10 and the 20 EMA.
    BUY   the mirror, arming on S3, S2, S1 or the pivot.

## It runs the strategy itself. That is the whole design.

The alert does not reimplement the rule. Every poll builds the same
`PivotEmaCascade` the backtest scores and feeds it the same `BarContext` shape
`algo/backtest/cfd_runner.py` builds, then reports the signal the strategy
returned on the newest closed bar. An alert that disagreed with the backtest
would be worse than no alert - you would be trading a rule nobody measured -
and the only way to guarantee they agree is for there to be one implementation.

`algo/backtest/signal_replay.py` holds that replay, rather than this file: it is
the part that decides whether a message is sent, so it belongs where `mypy` and
the test suite cover it. It is deliberately NOT `run_cfd_backtest` - that
function exists to answer "what did this earn after costs", and it fills, charges
spread and swap, and reports trades. This one answers "what did the strategy
say", which needs no fills and must not invent any. The position it carries is
a bookkeeping stand-in so the exit rule has something to exit, priced at the
signal bar's close and never charged - see `signal_replay.PaperBook`.

## Stateless per poll, on purpose

Each poll re-feeds the whole warmup window from scratch rather than carrying a
strategy instance between polls. That costs a few hundred bars of arithmetic per
symbol per five minutes, which is nothing, and it buys the property that matters
for something left running for weeks: there is no long-lived indicator state to
drift, no restart that resumes from a half-remembered cascade, and a missed poll
(laptop asleep, terminal restarted) simply re-derives the truth from the bars.

What IS persisted is only which bar was last alerted on, per symbol, so a
restart inside the same 5-minute period does not send the same message twice.

## What it does not do

It places no orders and sizes nothing. It says a rule fired. Whether that rule
is worth trading is the open question D-155 records: the strategy has been built
and tested but **never measured on real bars**, and the four rules measured
before it in this repo all failed. Treat every message as "the chart did this",
not as advice.

Usage:
    python tools/pivot_ema_telegram_alert/cascade_alert.py            # live
    python tools/pivot_ema_telegram_alert/cascade_alert.py --once     # one pass
    python tools/pivot_ema_telegram_alert/cascade_alert.py --dry-run  # no sending
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# And this directory, so `nifty50` resolves whether the file is run as a script
# (where Python adds it already) or imported by a test (where it does not).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from algo.backtest.signal_replay import Fired, forex_session_of, replay_signals  # noqa: E402
from algo.core.bar import Bar, Timeframe  # noqa: E402
from algo.core.enums import Exchange, Side  # noqa: E402
from algo.core.instrument import CfdId, InstrumentId  # noqa: E402
from algo.core.timeutil import ist_date  # noqa: E402
from algo.strategy.pivot_ema_cascade import PivotEmaCascade  # noqa: E402

LOG = logging.getLogger("cascade_alert")

#: The rule names five minutes. It is not a knob here: the pivot lines, the
#: EMA periods and this interval are one setup, and a "same rule on M15" run is
#: a different study (the measurement script sweeps that; an alert should not).
TIMEFRAME = Timeframe(minutes=5)

#: Bars fed before the newest one: the 200 EMA's seed-shedding length (285),
#: the bar a crossing compares against, and one M5 session (288) so the pivots
#: are drawn from a session the strategy watched end to end. Matching
#: `scripts/measure_pivot_ema_cascade_xauusd.py`, which explains both terms.
WARMUP_BARS = 286 + 288


# --------------------------------------------------------------------- config


def _env(name: str, default: str = "", *, required: bool = False) -> str:
    value = (os.getenv(name) or "").strip()
    if not value and required:
        raise SystemExit(
            f"Missing required environment variable {name}. "
            "See tools/pivot_ema_telegram_alert/README.md."
        )
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_list(name: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return tuple(default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    """Everything the loop reads, resolved once at startup."""

    telegram_token: str
    telegram_chat_id: str
    #: Symbols the MT5 terminal can select, exactly as its Market Watch spells
    #: them. XAUUSD and BTCUSD are the broker's own strings; the volatility
    #: index is whatever your broker calls it, which is why it is configured
    #: rather than hard-coded - a guess here would watch nothing and say so
    #: only in a log line.
    mt5_symbols: tuple[str, ...]
    #: NSE trading symbols, Angel One's spelling (`RELIANCE-EQ`). Empty unless
    #: `ALERT_NSE_SYMBOLS` is set or `--nifty50` is passed.
    nse_symbols: tuple[str, ...]
    poll_seconds: int
    state_path: Path
    dry_run: bool

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.mt5_symbols + self.nse_symbols


def load_config(*, dry_run: bool, nifty50: bool) -> Config:
    from nifty50 import NIFTY_50  # sibling module, see README

    nse = _env_list("ALERT_NSE_SYMBOLS")
    if nifty50:
        nse = tuple(dict.fromkeys(nse + NIFTY_50))
    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN", required=not dry_run),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID", required=not dry_run),
        mt5_symbols=_env_list("ALERT_MT5_SYMBOLS", ("XAUUSD",)),
        nse_symbols=nse,
        # A quarter of the bar period. The rule acts on closed bars, so polling
        # faster only shortens the wait between a bar closing and the message;
        # polling slower risks a whole bar of delay on a five-minute signal.
        poll_seconds=_env_int("ALERT_POLL_SECONDS", 75),
        state_path=Path(_env("ALERT_STATE_PATH", "cascade_alert_state.json")),
        dry_run=dry_run,
    )


# ----------------------------------------------------------------- market data


class Mt5Bars:
    """Closed M5 bars for any symbol the terminal can select.

    Connects and disconnects per call, the convention every MT5 caller in this
    repo follows: a poll happens once a minute at most, and a session held open
    for weeks is a thing that can be silently dead.
    """

    def __init__(self, symbols: Sequence[str]) -> None:
        self._symbols = tuple(symbols)

    def fetch(self, symbol: str, count: int) -> list[Bar]:
        import MetaTrader5 as mt5  # Windows-only optional extra

        from algo.data.mt5_feed import measure_server_offset

        if not mt5.initialize():
            raise RuntimeError(f"MT5 terminal not reachable: {mt5.last_error()}")
        try:
            if not mt5.symbol_select(symbol, True):
                raise RuntimeError(f"MT5 cannot select {symbol}: {mt5.last_error()}")
            offset = measure_server_offset(mt5, symbol)
            # Position 1, not 0: the forming bar's close can still change, and
            # a rule tested on closed bars must not be alerted on an open one.
            raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 1, count)
        finally:
            mt5.shutdown()
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"MT5 returned no bars for {symbol}")
        bars = [
            Bar(
                ts=datetime.fromtimestamp(int(row["time"]), UTC) - offset,
                timeframe=TIMEFRAME,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["tick_volume"]),
            )
            for row in raw
        ]
        bars.sort(key=lambda b: b.ts)
        return bars


class NseBars:
    """Closed M5 bars for NSE equities, over Angel One's SmartAPI.

    One session is opened per run and reused: unlike MT5 this is a remote login
    with a TOTP, and reconnecting per symbol would burn the rate limit and the
    one-time code. The instrument master is fetched once for the token lookup -
    a symbol's token is what the candle API actually takes.
    """

    def __init__(self) -> None:
        self._transport: object | None = None
        self._master: object | None = None

    def _connect(self) -> tuple[object, object]:
        if self._transport is not None and self._master is not None:
            return self._transport, self._master

        import pyotp  # only needed on the NSE path

        from algo.data.smartapi_feed import (
            SmartConnectTransport,
            credentials_from_env,
        )
        from algo.exchange.master import HttpMasterSource, InstrumentMaster

        creds = credentials_from_env()
        if not creds.has_all():
            raise RuntimeError(
                "SmartAPI credentials incomplete; missing "
                f"{', '.join(creds.missing())}. NSE symbols cannot be polled."
            )
        transport = SmartConnectTransport(creds.api_key)
        transport.connect(creds.client_id, creds.password, pyotp.TOTP(creds.totp).now())
        rows = HttpMasterSource().fetch_master()
        master = InstrumentMaster(rows, fetched_at=datetime.now(UTC))
        self._transport, self._master = transport, master
        return transport, master

    def fetch(self, symbol: str, count: int) -> list[Bar]:
        from algo.data.smartapi_feed import fetch_equity_bars

        transport, master = self._connect()
        until = datetime.now(UTC)
        # Calendar days, not bar counts: the candle API takes a window, and an
        # Indian equity trades 75 M5 bars a day, so `count` bars is at least
        # `count / 75` sessions - rounded up generously because holidays and
        # half-days make that a floor, never a ceiling.
        since = until - timedelta(days=max(7, count // 60))
        return fetch_equity_bars(
            transport,
            master,  # type: ignore[arg-type]
            symbol,
            timeframe=TIMEFRAME,
            since=since,
            until=until,
        )


# -------------------------------------------------------------------- routing


def instrument_for(symbol: str, exchange: Exchange) -> InstrumentId:
    """The identity token the strategy keys its position on.

    A `CfdId` for an NSE equity is a type-level compromise and is stated as
    one: the alert path never prices, sizes, fills or charges anything, so the
    only thing the strategy does with this value is use `key` to look itself up
    in a position view it was handed. Adding an equity member to the engine's
    discriminated `InstrumentId` union - which the specs, position and costs
    layers all match on - to satisfy a read-only monitor would be a much larger
    change with much more to get wrong.
    """
    return CfdId(symbol=symbol, exchange=exchange)


@dataclass(frozen=True)
class Market:
    """One watched symbol and everything needed to evaluate it."""

    symbol: str
    exchange: Exchange
    fetch: Callable[[str, int], list[Bar]]
    session_of: Callable[[datetime], date]

    @property
    def instrument(self) -> InstrumentId:
        return instrument_for(self.symbol, self.exchange)


def build_markets(config: Config) -> list[Market]:
    markets: list[Market] = []
    if config.mt5_symbols:
        mt5_bars = Mt5Bars(config.mt5_symbols)
        markets.extend(
            Market(
                symbol=symbol,
                exchange=Exchange.OTC,
                fetch=mt5_bars.fetch,
                session_of=forex_session_of,
            )
            for symbol in config.mt5_symbols
        )
    if config.nse_symbols:
        nse_bars = NseBars()
        markets.extend(
            Market(
                symbol=symbol,
                exchange=Exchange.NSE,
                fetch=nse_bars.fetch,
                # An equity's pivots roll on the IST calendar date, which is
                # also the only date its session ever spans.
                session_of=ist_date,
            )
            for symbol in config.nse_symbols
        )
    return markets


# --------------------------------------------------------------------- output


class TelegramNotifier:
    """Bot API sender. Plain `requests`, no event loop.

    Deliberately not imported from `tools/macd_telegram_alert/macd_alert.py`:
    that module imports ccxt at module scope, and this tool has no crypto path
    at all - taking a hard ccxt dependency so as to share forty lines would
    make the MT5-only install heavier for no gain.
    """

    def __init__(self, config: Config) -> None:
        self._url = f"https://api.telegram.org/bot{config.telegram_token}/sendMessage"
        self._chat_id = config.telegram_chat_id
        self._session = requests.Session()

    def send(self, text: str) -> bool:
        try:
            response = self._session.post(
                self._url,
                data={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            LOG.error("Telegram send failed: %s", exc)
            return False
        if not response.ok:
            LOG.error("Telegram rejected (%s): %s", response.status_code, response.text[:300])
            return False
        return True


def format_message(market: Market, fired: Fired) -> str:
    """The message. States the rule that fired, not what to do about it."""
    signal = fired.signal
    action = "ENTRY" if fired.is_entry else "EXIT"
    arrow = "🔻 SELL" if fired.side is Side.SELL else "🔺 BUY"
    if not fired.is_entry:
        arrow = "⬜ CLOSE " + ("SHORT" if fired.side is Side.BUY else "LONG")
    # By period, not by key: sorting the strings puts the 100 EMA between the
    # 10 and the 20, which reads as a data error on a phone screen.
    periods = sorted(
        (int(key.removeprefix("ema_")), value)
        for key, value in signal.context.items()
        if key.startswith("ema_")
    )
    emas = " · ".join(f"{period}: {value}" for period, value in periods)
    return (
        f"<b>{html.escape(market.symbol)} — {action} {arrow}</b>\n"
        f"M5 close <b>{fired.bar.close}</b> at {fired.bar.ts:%Y-%m-%d %H:%M} UTC\n"
        f"\n{html.escape(signal.reason)}\n"
        f"\n<code>{html.escape(emas)}</code>\n"
        f"\n<i>Pivot/EMA cascade — signal only, not advice. This rule has not "
        f"been measured on real bars (D-155).</i>"
    )


class StateStore:
    """Remembers the last alerted bar per symbol, so a restart does not re-fire.

    Keyed by symbol and holding an ISO timestamp: the question it answers is
    "have I already spoken about this bar", and a bar is identified by when it
    closed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, ValueError) as exc:
                LOG.warning("Ignoring unreadable state file %s: %s", path, exc)

    def already_sent(self, symbol: str, bar_ts: datetime) -> bool:
        return self._data.get(symbol) == bar_ts.isoformat()

    def record(self, symbol: str, bar_ts: datetime) -> None:
        self._data[symbol] = bar_ts.isoformat()
        try:
            self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError as exc:
            LOG.warning("Could not write state file %s: %s", self._path, exc)


# -------------------------------------------------------------------- monitor


@dataclass
class Monitor:
    config: Config
    markets: list[Market]
    notifier: TelegramNotifier | None
    state: StateStore
    #: Symbols whose last poll raised, so a repeated failure is logged once at
    #: warning and then stays quiet - a terminal that is closed overnight
    #: should not produce 700 identical lines.
    _failing: set[str] = field(default_factory=set)

    def poll_once(self) -> list[tuple[Market, Fired]]:
        sent: list[tuple[Market, Fired]] = []
        for market in self.markets:
            try:
                bars = market.fetch(market.symbol, WARMUP_BARS + 2)
            except Exception as exc:  # noqa: BLE001 - one symbol must not stop the loop
                if market.symbol not in self._failing:
                    LOG.warning("%s: %s", market.symbol, exc)
                    self._failing.add(market.symbol)
                continue
            self._failing.discard(market.symbol)

            if len(bars) < WARMUP_BARS:
                LOG.info(
                    "%s: %d bars, need %d before the 200 EMA is warm",
                    market.symbol,
                    len(bars),
                    WARMUP_BARS,
                )
                continue

            latest = bars[-1]
            if self.state.already_sent(market.symbol, latest.ts):
                continue

            instrument = market.instrument
            fired = replay_signals(
                bars,
                strategy_factory=lambda inst=instrument: PivotEmaCascade(instrument=inst),
                instrument=instrument,
                timeframe=TIMEFRAME,
                session_of=market.session_of,
                exchange=market.exchange,
            )
            # Only the newest bar is news. Everything earlier is history the
            # replay had to walk through to get the indicators right, and was
            # either alerted on at the time or happened before this run began.
            on_latest = [f for f in fired if f.bar.ts == latest.ts]
            self.state.record(market.symbol, latest.ts)
            for hit in on_latest:
                message = format_message(market, hit)
                LOG.info("%s %s", market.symbol, hit.signal.reason)
                if self.notifier is not None:
                    self.notifier.send(message)
                else:
                    print(message)  # noqa: T201 - --dry-run IS the output
                sent.append((market, hit))
        return sent

    def run_forever(self) -> int:
        LOG.info(
            "Watching %d symbol(s) on M5: %s",
            len(self.markets),
            ", ".join(m.symbol for m in self.markets),
        )
        while True:
            self.poll_once()
            time.sleep(self.config.poll_seconds)


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print messages instead of sending them; needs no Telegram token",
    )
    parser.add_argument(
        "--nifty50",
        action="store_true",
        help="add all NIFTY 50 constituents to the watch list (needs SmartAPI credentials)",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(dry_run=args.dry_run, nifty50=args.nifty50)
    markets = build_markets(config)
    if not markets:
        raise SystemExit("Nothing to watch: set ALERT_MT5_SYMBOLS or pass --nifty50.")

    monitor = Monitor(
        config=config,
        markets=markets,
        notifier=None if config.dry_run else TelegramNotifier(config),
        state=StateStore(config.state_path),
    )
    if args.once:
        monitor.poll_once()
        return 0
    try:
        return monitor.run_forever()
    except KeyboardInterrupt:
        LOG.info("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
