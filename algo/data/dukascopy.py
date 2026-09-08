"""Dukascopy tick archive: bid/ask ticks, and the bars and spread built from them.

`mt5_feed` is the project's usual source of XAUUSD history and stays so for
anything live. It cannot serve this study: the terminal holds M5 back to
2025-12-19 and no further, which is eight months - not enough to say anything
about a five-minute strategy across regimes. The tick archive already on disk
(`data/dukascopy/XAUUSD/`) covers 2024-01 onward, and it carries **bid and ask**
on every tick rather than a single price.

That second point matters more than the length. `CfdCosts.half_spread` has been
a constant since D-121 and `mt5_spread` samples the live terminal to replace it;
neither can say what the spread was on a Tuesday morning in March 2024. Ticks
can, exactly, and `spread_profile()` below builds the same `SpreadProfile` type
`mt5_spread` produces so the runner does not care which measured it.

## The file format, since nothing here documents it

One file per hour: `{YEAR}/{MONTH-1:02d}/{DAY:02d}/{HOUR:02d}h_ticks.bi5`. The
month is **zero-indexed** in the path and the day is not, which is Dukascopy's
convention and a reliable way to be off by a month if assumed rather than
checked (`decode_path` is tested against a known gold price). The body is raw
LZMA (`FORMAT_ALONE`, no container) over fixed 20-byte big-endian records:

    uint32  milliseconds since the hour
    uint32  ask, in points
    uint32  bid, in points
    float32 ask volume
    float32 bid volume

XAUUSD quotes to three decimals, so a point is 1/1000 of a dollar. A zero-length
file is a market-closed hour, not a corrupt one.

## Bars are mid, deliberately

Every bar built here is the **mid** of bid and ask. A bid-based bar (what MT5
serves) silently embeds half the spread in every long entry and hides it from
the cost model, which is precisely the accounting the runner is trying to keep
visible: mid bars plus a measured half-spread charged per fill means the spread
appears once, as a number, rather than twice - once in the price and once in the
charge - or not at all.

## The grid

Bars are close-labelled on the UTC wall clock: a five-minute bar stamped 10:05
covers `(10:00, 10:05]`. That is `algo.core.bar`'s convention throughout, and on
a continuously-traded CFD the wall clock *is* the grid - there is no session
anchor to resample against, which is why `algo.data.resample` (anchored to an
MCX session via `ist_date`) does not apply and is not used.
"""

from __future__ import annotations

import lzma
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.data.mt5_spread import SpreadProfile, _hour_of_week, _quantile

#: Bytes per tick record.
RECORD = 20
_LAYOUT = struct.Struct(">IIIff")

#: XAUUSD is quoted to three decimals, so one point is a tenth of a cent.
XAUUSD_POINT = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class Tick:
    ts: datetime
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


def hour_files(root: Path) -> list[tuple[datetime, Path]]:
    """Every hourly file under `root`, as (hour start in UTC, path), in order.

    `root` is the symbol directory - `data/dukascopy/XAUUSD`. Files that do not
    match the archive's own naming are ignored rather than guessed at.
    """
    if not root.exists():
        raise DataError(f"tick archive not found: {root}")
    out: list[tuple[datetime, Path]] = []
    for path in root.glob("*/*/*/*h_ticks.bi5"):
        try:
            out.append((decode_path(path), path))
        except DataError:
            continue
    out.sort()
    return out


def decode_path(path: Path) -> datetime:
    """The UTC hour a tick file covers, from its own path.

    The month segment is zero-indexed; `01` is February. Getting that wrong
    shifts every bar by a month and produces a series that still looks like
    gold, which is why this is a named function with a test rather than an
    expression inline.
    """
    parts = path.parts
    if len(parts) < 4:
        raise DataError(f"not a Dukascopy tick path: {path}")
    year_s, month_s, day_s, name = parts[-4], parts[-3], parts[-2], parts[-1]
    if not name.endswith("h_ticks.bi5"):
        raise DataError(f"not a Dukascopy tick file: {path}")
    try:
        return datetime(
            int(year_s), int(month_s) + 1, int(day_s), int(name[:2]), tzinfo=UTC
        )
    except ValueError as exc:
        raise DataError(f"cannot read a UTC hour out of {path}: {exc}") from exc


def read_ticks(path: Path, hour: datetime, *, point: Decimal = XAUUSD_POINT) -> list[Tick]:
    """Decode one hourly file. An empty file is a closed hour, and yields none."""
    raw = path.read_bytes()
    if not raw:
        return []
    try:
        body = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw)
    except lzma.LZMAError as exc:
        raise DataError(f"{path} is not readable as Dukascopy LZMA: {exc}") from exc
    if len(body) % RECORD:
        raise DataError(
            f"{path} decoded to {len(body)} bytes, not a multiple of {RECORD} - "
            "the record layout does not match this file"
        )

    ticks: list[Tick] = []
    for offset in range(0, len(body), RECORD):
        ms, ask, bid, _av, _bv = _LAYOUT.unpack_from(body, offset)
        # A zero or inverted quote is a bad tick, not a free trade - the same
        # judgement `mt5_spread.measure_spread_profile` makes.
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        ticks.append(
            Tick(
                ts=hour + timedelta(milliseconds=ms),
                bid=Decimal(bid) * point,
                ask=Decimal(ask) * point,
            )
        )
    return ticks


def iter_ticks(
    root: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    point: Decimal = XAUUSD_POINT,
) -> Iterator[Tick]:
    """Every tick in `[start, end)`, in time order, one hourly file at a time."""
    for hour, path in hour_files(root):
        if start is not None and hour + timedelta(hours=1) <= start:
            continue
        if end is not None and hour >= end:
            continue
        for tick in read_ticks(path, hour, point=point):
            if start is not None and tick.ts < start:
                continue
            if end is not None and tick.ts >= end:
                continue
            yield tick


def bars_from_ticks(ticks: Sequence[Tick] | Iterator[Tick], timeframe: Timeframe) -> list[Bar]:
    """Aggregate ticks onto the UTC grid, close-labelled.

    A tick at exactly 10:00:00.000 closes the bar stamped 10:00; it does not
    open the next one. That is `(open, close]`, the engine's convention, and the
    one place an off-by-one here would become look-ahead somewhere else.

    `volume` carries the **tick count**, not traded volume: a CFD feed has none.
    The distinction is the same one `measure_asia_value_area_xauusd` states -
    a count of quote updates wearing the name volume.
    """
    step = timedelta(minutes=timeframe.minutes)
    bars: list[Bar] = []

    bucket_ts: datetime | None = None
    o = hi = lo = c = None
    count = 0

    for tick in ticks:
        mid = tick.mid
        close_ts = _bucket_close(tick.ts, step)
        if bucket_ts is None or close_ts != bucket_ts:
            if bucket_ts is not None:
                bars.append(
                    Bar(
                        ts=bucket_ts,
                        timeframe=timeframe,
                        open=o,  # type: ignore[arg-type]
                        high=hi,  # type: ignore[arg-type]
                        low=lo,  # type: ignore[arg-type]
                        close=c,  # type: ignore[arg-type]
                        volume=count,
                    )
                )
            bucket_ts, o, hi, lo, c, count = close_ts, mid, mid, mid, mid, 0
        hi = max(hi, mid)  # type: ignore[type-var]
        lo = min(lo, mid)  # type: ignore[type-var]
        c = mid
        count += 1

    if bucket_ts is not None:
        bars.append(
            Bar(
                ts=bucket_ts,
                timeframe=timeframe,
                open=o,  # type: ignore[arg-type]
                high=hi,  # type: ignore[arg-type]
                low=lo,  # type: ignore[arg-type]
                close=c,  # type: ignore[arg-type]
                volume=count,
            )
        )
    return bars


def _bucket_close(ts: datetime, step: timedelta) -> datetime:
    """The close timestamp of the bar `ts` belongs to, under `(open, close]`."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = ts - epoch
    periods = elapsed // step
    boundary = epoch + step * periods
    return boundary if boundary == ts else boundary + step


class SpreadReservoir:
    """Collects tick spreads by hour of the trading week, with bounded memory.

    Split out from a plain function so the archive can be read **once**: the
    build script taps the same tick stream that is being folded into bars, and
    decoding eight thousand LZMA files twice to get the spread separately would
    double the only slow part of the pipeline.

    `cap` bounds how many spreads are kept per hour-of-week bucket. Past the
    cap the bucket is halved and the sampling stride doubled, which keeps a
    uniform sample of the entire stream rather than of its tail, and does so
    deterministically: the same archive must produce the same profile on every
    run, or a backtest stops being reproducible for a reason nobody would think
    to look for in a spread model.
    """

    __slots__ = ("_by_hour", "_cap", "_seen", "_stride", "_total")

    def __init__(self, cap: int = 20_000) -> None:
        self._cap = cap
        self._by_hour: dict[int, list[Decimal]] = {}
        self._seen: dict[int, int] = {}
        self._stride: dict[int, int] = {}
        self._total = 0

    def add(self, tick: Tick) -> None:
        hour = _hour_of_week(tick.ts)
        self._total += 1
        count = self._seen.get(hour, 0) + 1
        self._seen[hour] = count
        bucket = self._by_hour.setdefault(hour, [])
        stride = self._stride.get(hour, 1)
        if count % stride:
            return
        bucket.append(tick.spread)
        if len(bucket) > self._cap:
            # Halve by keeping every second sample and take twice as long a
            # stride from here on. The bucket stays a *uniform* sample of the
            # whole stream at every point, which the obvious alternative -
            # overwriting once full - does not: that keeps only the most recent
            # ticks, and this archive's spread doubles between 2024 and 2025,
            # so "most recent" and "typical" are different numbers.
            self._by_hour[hour] = bucket[::2]
            self._stride[hour] = stride * 2

    @property
    def ticks(self) -> int:
        return self._total

    def profile(
        self, *, symbol: str = "XAUUSD", measured_at: datetime | None = None
    ) -> SpreadProfile:
        """The same `SpreadProfile` type `mt5_spread` produces, so
        `CfdCosts.half_spread_at` accepts either without knowing which measured
        it - median and 90th percentile per hour-of-week, with a global median
        as the fallback for an hour nothing was sampled in."""
        populated = {hour: v for hour, v in self._by_hour.items() if v}
        if not populated:
            raise DataError("no usable ticks to build a spread profile from")
        every = [value for values in populated.values() for value in values]
        return SpreadProfile(
            symbol=symbol,
            median_by_hour={hour: _quantile(v, 0.5) for hour, v in populated.items()},
            p90_by_hour={hour: _quantile(v, 0.9) for hour, v in populated.items()},
            samples=len(populated),
            ticks=self._total,
            measured_at=measured_at or datetime.now(UTC),
            fallback=_quantile(every, 0.5),
        )


def spread_profile(
    ticks: Sequence[Tick] | Iterator[Tick],
    *,
    symbol: str = "XAUUSD",
    measured_at: datetime | None = None,
) -> SpreadProfile:
    """The real spread, by hour of the trading week, from the archive itself.

    The difference from `mt5_spread.measure_spread_profile` is what stands
    behind it. That one samples three-minute windows at points across the
    history, because pulling a hundred million ticks from a terminal to price a
    few hundred fills would be absurd. Here every tick is already being read for
    the bars, so every one of them is seen.
    """
    reservoir = SpreadReservoir()
    for tick in ticks:
        reservoir.add(tick)
    return reservoir.profile(symbol=symbol, measured_at=measured_at)


def regrid_bars(bars: Sequence[Bar], timeframe: Timeframe) -> list[Bar]:
    """Aggregate finer close-labelled bars onto a coarser UTC grid.

    `algo.data.resample` does this for MCX, anchored to a session via
    `ist_date` and a `MarketCalendar`. A continuously-traded CFD has no session
    to anchor to - the wall clock is the grid - so that function's whole
    premise is absent here and forcing it would mean inventing a session
    boundary the instrument does not have.

    The source bar's own `(open, close]` label decides its bucket, so an M5 bar
    stamped 11:00 belongs to the H1 bar stamped 11:00, not to the next one.
    Only buckets with at least one source bar are emitted; a market-closed hour
    produces nothing rather than a flat bar that never traded.
    """
    step = timedelta(minutes=timeframe.minutes)
    for bar in bars:
        if bar.timeframe.minutes > timeframe.minutes:
            raise DataError(
                f"cannot regrid {bar.timeframe} bars onto a finer {timeframe} grid"
            )
        if timeframe.minutes % bar.timeframe.minutes:
            raise DataError(f"{bar.timeframe} does not divide {timeframe} evenly")

    out: list[Bar] = []
    members: list[Bar] = []
    bucket_ts: datetime | None = None

    for bar in bars:
        close_ts = _bucket_close(bar.ts, step)
        if bucket_ts is not None and close_ts != bucket_ts:
            out.append(_merge(members, bucket_ts, timeframe))
            members = []
        bucket_ts = close_ts
        members.append(bar)

    if bucket_ts is not None and members:
        out.append(_merge(members, bucket_ts, timeframe))
    return out


def _merge(members: list[Bar], ts: datetime, timeframe: Timeframe) -> Bar:
    return Bar(
        ts=ts,
        timeframe=timeframe,
        open=members[0].open,
        high=max(b.high for b in members),
        low=min(b.low for b in members),
        close=members[-1].close,
        volume=sum(b.volume for b in members),
    )


def bars_with_spread(
    ticks: Sequence[Tick] | Iterator[Tick], timeframe: Timeframe
) -> tuple[list[Bar], list[Decimal]]:
    """Bars, and the median quoted spread inside each of them.

    An hour-of-week profile is the right shape when all you can sample is a few
    thousand ticks (`mt5_spread`). With the whole archive in hand it is a
    needless approximation, and a harmful one here: the median XAUUSD spread in
    this data is 0.33 in March 2024 and 0.67 in April 2025. One profile spanning
    both charges 2024 too much and 2025 too little, which is exactly the kind of
    error that moves a marginal strategy across zero.

    So each bar carries the spread that was actually quoted while it formed, and
    a fill at its open is charged half of that. The median rather than the mean,
    for `mt5_spread`'s reason: one 40-tick spike during a news print would drag
    a mean far above what a typical fill pays.

    A bar built from a single tick has that tick's spread; there is no bar
    without at least one.
    """
    step = timedelta(minutes=timeframe.minutes)
    bars: list[Bar] = []
    spreads: list[Decimal] = []

    bucket_ts: datetime | None = None
    o = hi = lo = c = None
    count = 0
    inside: list[Decimal] = []

    def flush() -> None:
        bars.append(
            Bar(
                ts=bucket_ts,  # type: ignore[arg-type]
                timeframe=timeframe,
                open=o,  # type: ignore[arg-type]
                high=hi,  # type: ignore[arg-type]
                low=lo,  # type: ignore[arg-type]
                close=c,  # type: ignore[arg-type]
                volume=count,
            )
        )
        spreads.append(_median(inside))

    for tick in ticks:
        mid = tick.mid
        close_ts = _bucket_close(tick.ts, step)
        if bucket_ts is None or close_ts != bucket_ts:
            if bucket_ts is not None:
                flush()
            bucket_ts, o, hi, lo, c, count, inside = close_ts, mid, mid, mid, mid, 0, []
        hi = max(hi, mid)  # type: ignore[type-var]
        lo = min(lo, mid)  # type: ignore[type-var]
        c = mid
        count += 1
        inside.append(tick.spread)

    if bucket_ts is not None:
        flush()
    return bars, spreads


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise DataError("no spreads to take a median of")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


class MeasuredSpread:
    """`CfdCosts.half_spread_at`, backed by the spread each bar actually saw.

    Falls back to the hour-of-week profile for a timestamp with no bar of its
    own - a fill can only land on a bar that exists, so in practice that is a
    guard rather than a path, and it is a measured number either way rather than
    a constant pulled from outside the data.
    """

    __slots__ = ("_by_ts", "_multiplier", "_profile")

    def __init__(
        self,
        by_ts: dict[datetime, Decimal],
        profile: SpreadProfile,
        *,
        multiplier: Decimal = Decimal("1"),
    ) -> None:
        self._by_ts = by_ts
        self._profile = profile
        #: Scales every spread at once, for the higher-cost sensitivity runs.
        #: A multiplier rather than a second measurement, so the *shape* stays
        #: the measured one and only the level moves - which is the question
        #: being asked ("how much worse would a worse venue have to be").
        self._multiplier = multiplier

    def __call__(self, ts: datetime) -> Decimal:
        full = self._by_ts.get(ts)
        if full is None:
            return self._profile.half_spread_at(ts) * self._multiplier
        return full / 2 * self._multiplier

    @property
    def by_ts(self) -> dict[datetime, Decimal]:
        """The measurement itself, so a scaled copy re-uses it rather than
        re-reading the archive."""
        return self._by_ts

    @property
    def profile(self) -> SpreadProfile:
        return self._profile

    @property
    def covered(self) -> int:
        return len(self._by_ts)

    def describe(self) -> str:
        values = sorted(self._by_ts.values())
        if not values:
            return "no measured spreads"
        return (
            f"per-bar spread on {len(values):,} bars; "
            f"median {_median(values)}, "
            f"p10 {values[len(values) // 10]}, p90 {values[len(values) * 9 // 10]}"
        )


def save_spread_series(
    bars: Sequence[Bar], spreads: Sequence[Decimal], path: Path
) -> Path:
    """One row per bar: close timestamp and the spread quoted inside it."""
    if len(bars) != len(spreads):
        raise DataError(f"{len(bars)} bars but {len(spreads)} spreads")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ts,spread"]
    lines.extend(
        f"{bar.ts.isoformat()},{spread}"
        for bar, spread in zip(bars, spreads, strict=True)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_spread_series(path: Path) -> dict[datetime, Decimal]:
    if not path.exists():
        raise DataError(f"spread series not found: {path}")
    out: dict[datetime, Decimal] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if not line:
            continue
        ts, spread = line.split(",")
        out[datetime.fromisoformat(ts)] = Decimal(spread)
    return out


@dataclass(frozen=True, slots=True)
class Flow:
    """Tick-level activity inside one bar - what OHLC throws away.

    A bar says where price started and ended. The ticks say how it got there:
    how many quote updates it took, and how many of them moved the mid up
    against how many moved it down. That is the closest thing a CFD feed has to
    order flow - there is no traded volume and no side - and it is a genuinely
    different input from anything derived from the four prices, which is the
    only reason it is worth extracting.

    `upticks` and `downticks` count mid changes, not quotes: a tick that repeats
    the previous mid is neither, and on gold most ticks are neither.
    """

    ticks: int
    upticks: int
    downticks: int

    @property
    def imbalance(self) -> float:
        """(up - down) / (up + down), or 0 when the mid never moved."""
        moved = self.upticks + self.downticks
        return 0.0 if moved == 0 else (self.upticks - self.downticks) / moved


def bars_with_microstructure(
    ticks: Sequence[Tick] | Iterator[Tick], timeframe: Timeframe
) -> tuple[list[Bar], list[Decimal], list[Flow]]:
    """Bars, the median spread inside each, and each one's tick flow.

    One pass, because the archive takes minutes to decode and the three outputs
    all come from the same stream. See `bars_with_spread` for why the spread is
    per bar rather than a profile.
    """
    step = timedelta(minutes=timeframe.minutes)
    bars: list[Bar] = []
    spreads: list[Decimal] = []
    flows: list[Flow] = []

    bucket_ts: datetime | None = None
    o = hi = lo = c = None
    count = up = down = 0
    inside: list[Decimal] = []
    previous_mid: Decimal | None = None

    def flush() -> None:
        bars.append(
            Bar(
                ts=bucket_ts,  # type: ignore[arg-type]
                timeframe=timeframe,
                open=o,  # type: ignore[arg-type]
                high=hi,  # type: ignore[arg-type]
                low=lo,  # type: ignore[arg-type]
                close=c,  # type: ignore[arg-type]
                volume=count,
            )
        )
        spreads.append(_median(inside))
        flows.append(Flow(ticks=count, upticks=up, downticks=down))

    for tick in ticks:
        mid = tick.mid
        close_ts = _bucket_close(tick.ts, step)
        if bucket_ts is None or close_ts != bucket_ts:
            if bucket_ts is not None:
                flush()
            bucket_ts, o, hi, lo, c = close_ts, mid, mid, mid, mid
            count = up = down = 0
            inside = []
        hi = max(hi, mid)  # type: ignore[type-var]
        lo = min(lo, mid)  # type: ignore[type-var]
        c = mid
        count += 1
        inside.append(tick.spread)
        # Compared against the previous tick's mid across the whole stream, not
        # reset at the bar boundary: the first tick of a bar did move relative to
        # the last tick of the one before, and pretending otherwise would drop
        # one observation per bar and bias the count toward zero.
        if previous_mid is not None:
            if mid > previous_mid:
                up += 1
            elif mid < previous_mid:
                down += 1
        previous_mid = mid

    if bucket_ts is not None:
        flush()
    return bars, spreads, flows


def save_flow(bars: Sequence[Bar], flows: Sequence[Flow], path: Path) -> Path:
    if len(bars) != len(flows):
        raise DataError(f"{len(bars)} bars but {len(flows)} flow records")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ts,ticks,upticks,downticks"]
    lines.extend(
        f"{bar.ts.isoformat()},{flow.ticks},{flow.upticks},{flow.downticks}"
        for bar, flow in zip(bars, flows, strict=True)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_flow(path: Path) -> dict[datetime, Flow]:
    if not path.exists():
        raise DataError(f"flow series not found: {path}")
    out: dict[datetime, Flow] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if not line:
            continue
        ts, ticks, up, down = line.split(",")
        out[datetime.fromisoformat(ts)] = Flow(
            ticks=int(ticks), upticks=int(up), downticks=int(down)
        )
    return out
