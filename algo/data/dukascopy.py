"""XAUUSD tick history from Dukascopy's public archive, as `Bar`s.

`Mt5BarFeed` and `mt5_history` serve what the trading account's broker retains,
and on this Vantage account that is M1 back to 2026-05-26 and nothing earlier
(checked, not assumed). D-146 could therefore reach 74 sessions of a once-a-day
setup and could not tell "no edge" from "sample too small" - it said so, and
`backtest-history.md` ranks fixing that above every strategy question. This
module is the fix: Dukascopy publishes tick data for XAUUSD going back years,
free, one file per hour, and it is the only source here that can build a
fifteen-minute volume profile over more than a quarter.

## What this is not

It is **not** a second price feed for trading, and nothing in `algo/live/` may
import it. It is a research archive. The live loop and the broker adapter stay
on MT5, because the account that fills an order is the account whose prices
matter.

## The format, including the trap

    http://datafeed.dukascopy.com/datafeed/XAUUSD/{year}/{month}/{day}/{hour}h_ticks.bi5

**The month is zero-indexed.** January is `00` and December is `11`; the day and
hour are not. Getting this wrong silently fetches a real file from the wrong
month, which is worse than a 404, so `hour_url` is the only place the arithmetic
happens and `test_dukascopy.py` pins it.

Each file is LZMA-compressed. Decompressed it is a flat array of 20-byte
big-endian records - `>3I2f`: milliseconds since the hour, ask, bid, ask volume,
bid volume. Prices are integers scaled by `PRICE_SCALE`; XAUUSD quotes to three
decimals, so that is 1000. An empty body means the hour had no ticks - a
weekend, a holiday, or the daily break - and is cached as empty so a resumed
download does not ask again.

## Mid, not bid

Bars are built from `(ask + bid) / 2`. The cost model charges `half_spread` on
each leg on top of the bar price (`CfdCosts`, D-121), which only makes sense if
the bar price is the mid - taking bid here would charge the sell side of the
spread twice. Dukascopy's own spread is wider than Vantage's measured $0.22
(the sample hour quoted $0.49-0.56), and that difference is deliberately
discarded: this module supplies the **price path**, the Vantage cost stack
supplies the **costs**, and mixing a second broker's spreads into a study of the
first broker's account would answer a question nobody asked.

## Plain HTTP, and why that is defensible here

`datafeed.dukascopy.com` does not answer on 443 from this machine; port 80 does.
Unencrypted transport for a public archive risks corruption rather than
disclosure - there are no credentials involved - and corruption is detectable:
2026-06-02 10:00 UTC decoded to 6,978 ticks whose mid OHLC matched the MT5 feed
for the same hour to within $0.13 on every value, with the shape identical to
the cent. `verify_against` exists so that check is repeatable rather than a
claim made once in a docstring.

## The cache is the download

Raw `.bi5` bodies are written under `data/dukascopy/` exactly as fetched and
never rewritten. A bulk pull is therefore resumable, re-parsing costs no
network, and the bytes on disk are the ones the server sent - so a decoding
change can be re-run against the original data rather than requiring a
re-download.
"""

from __future__ import annotations

import lzma
import struct
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError

BASE_URL = "http://datafeed.dukascopy.com/datafeed"

DEFAULT_CACHE = Path("data/dukascopy")

#: XAUUSD quotes to three decimals, so the integers in the file are thousandths
#: of a dollar. This is per-instrument on Dukascopy; only XAUUSD is verified
#: here, and `bars_from_ticks` is given the scale rather than assuming it.
PRICE_SCALE = Decimal("1000")

#: One tick record: ms into the hour, ask, bid, ask volume, bid volume.
_RECORD = struct.Struct(">3I2f")

#: Sent because the archive refuses a bare urllib agent string.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; algo-research/1.0)"}


@dataclass(frozen=True, slots=True)
class Tick:
    """One quote update. Volumes are Dukascopy's own and are carried unused."""

    ts: datetime
    ask: Decimal
    bid: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.ask + self.bid) / 2


def hour_url(symbol: str, hour: datetime) -> str:
    """The archive path for one UTC hour.

    The month is zero-indexed and the day and hour are not; that asymmetry is
    the single most likely thing to get wrong here, so it lives in one function.
    """
    if hour.tzinfo is None:
        raise DataError(f"hour must be timezone-aware, got {hour!r}")
    at = hour.astimezone(UTC)
    return (
        f"{BASE_URL}/{symbol}/{at.year:04d}/{at.month - 1:02d}/"
        f"{at.day:02d}/{at.hour:02d}h_ticks.bi5"
    )


def cache_path(symbol: str, hour: datetime, root: Path = DEFAULT_CACHE) -> Path:
    """Where one hour's raw body is kept. Mirrors the URL, month included, so a
    file on disk can be matched to the request that produced it by eye."""
    at = hour.astimezone(UTC)
    return (
        root
        / symbol
        / f"{at.year:04d}"
        / f"{at.month - 1:02d}"
        / f"{at.day:02d}"
        / f"{at.hour:02d}h_ticks.bi5"
    )


def fetch_hour(
    symbol: str,
    hour: datetime,
    *,
    root: Path = DEFAULT_CACHE,
    timeout: int = 60,
) -> bytes:
    """One hour's raw body, from the cache if present and the archive if not.

    A 404 means the archive has no file for that hour - which for a weekend or a
    holiday is the correct answer, not an error - and is cached as an empty body
    so a resumed pull does not ask a second time. Any other HTTP failure raises,
    because silently treating a 500 as "no ticks" would put a hole in a study
    and call it a market closure.
    """
    path = cache_path(symbol, hour, root)
    if path.exists():
        return path.read_bytes()

    request = urllib.request.Request(hour_url(symbol, hour), headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = bytes(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise DataError(
                f"dukascopy returned HTTP {exc.code} for {hour:%Y-%m-%d %H}h "
                f"({symbol}); refusing to record that as an empty hour"
            ) from exc
        body = b""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


def decode(body: bytes, hour: datetime, *, scale: Decimal = PRICE_SCALE) -> list[Tick]:
    """Raw `.bi5` body into ticks. An empty body is an hour with no quotes."""
    if not body:
        return []
    try:
        raw = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO).decompress(body)
    except lzma.LZMAError as exc:
        raise DataError(
            f"{hour:%Y-%m-%d %H}h did not decompress ({exc}); the cached body is "
            "truncated or is not a .bi5 - delete it and refetch"
        ) from exc
    if len(raw) % _RECORD.size:
        raise DataError(
            f"{hour:%Y-%m-%d %H}h decoded to {len(raw)} bytes, not a multiple of "
            f"{_RECORD.size} - the record layout does not match this file"
        )
    start = hour.astimezone(UTC)
    return [
        Tick(
            ts=start + timedelta(milliseconds=millis),
            ask=Decimal(ask) / scale,
            bid=Decimal(bid) / scale,
        )
        for millis, ask, bid, _av, _bv in _RECORD.iter_unpack(raw)
    ]


def bars_from_ticks(ticks: list[Tick], timeframe: Timeframe) -> list[Bar]:
    """Ticks aggregated into bars, stamped by the bar's **open** instant.

    Open-stamped rather than close-stamped because that is what `mt5_history`
    and `Mt5BarFeed` already produce, and one convention that is wrong in the
    same way everywhere is far safer than two that disagree. `volume` is the
    tick count, which is the same quantity MT5 calls `tick_volume` - so a study
    that builds a volume profile gets the same kind of number from either
    source.
    """
    if not ticks:
        return []
    span = timedelta(minutes=timeframe.minutes)
    bars: list[Bar] = []
    bucket: list[Decimal] = []
    opened: datetime | None = None

    def flush() -> None:
        if opened is None or not bucket:
            return
        bars.append(
            Bar(
                ts=opened,
                timeframe=timeframe,
                open=bucket[0],
                high=max(bucket),
                low=min(bucket),
                close=bucket[-1],
                volume=len(bucket),
            )
        )

    for tick in ticks:
        epoch = int(tick.ts.timestamp())
        floor = datetime.fromtimestamp(epoch - epoch % int(span.total_seconds()), UTC)
        if opened is None:
            opened = floor
        elif floor != opened:
            flush()
            bucket = []
            opened = floor
        bucket.append(tick.mid)
    flush()
    return bars


def hours_between(start: datetime, end: datetime) -> Iterator[datetime]:
    """Every UTC hour in `[start, end)`, oldest first."""
    if end <= start:
        raise DataError(f"end {end} must be after start {start}")
    at = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    while at < end:
        yield at
        at += timedelta(hours=1)


def load_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    timeframe: Timeframe,
    root: Path = DEFAULT_CACHE,
    cached_only: bool = True,
) -> list[Bar]:
    """Bars over a range, built from whatever the cache holds.

    `cached_only` defaults True so a study never silently turns into a download
    - the bulk pull is a separate, deliberate act
    (`scripts/fetch_dukascopy_xauusd.py`). An hour missing from the cache is
    skipped, and a study that cares how much it got should compare the bar count
    against the calendar rather than assuming the range was complete.
    """
    ticks: list[Tick] = []
    for hour in hours_between(start, end):
        path = cache_path(symbol, hour, root)
        if not path.exists():
            if cached_only:
                continue
            fetch_hour(symbol, hour, root=root)
        ticks.extend(decode(path.read_bytes(), hour))
    ticks.sort(key=lambda t: t.ts)
    return [b for b in bars_from_ticks(ticks, timeframe) if start <= b.ts < end]


def verify_against(
    mt5_bars: list[Bar], dukascopy_bars: list[Bar]
) -> tuple[int, Decimal, Decimal]:
    """Compare two feeds over the same instants: (count, max |diff|, mean diff).

    Kept in the module rather than in a script because the honesty of every
    study built on this archive rests on the two feeds agreeing, and a check
    that is repeatable is worth more than a number quoted once in a docstring.
    The mean difference is signed - a constant offset means the feeds quote
    different sides or a different venue, which is expected and harmless; a
    large *max* means something is wrong.
    """
    by_ts = {bar.ts: bar for bar in dukascopy_bars}
    diffs = [bar.close - by_ts[bar.ts].close for bar in mt5_bars if bar.ts in by_ts]
    if not diffs:
        raise DataError("the two feeds share no bar timestamps")
    largest = max(abs(d) for d in diffs)
    mean = sum(diffs, Decimal("0")) / Decimal(len(diffs))
    return len(diffs), largest, mean
