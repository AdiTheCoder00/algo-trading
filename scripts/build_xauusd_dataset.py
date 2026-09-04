"""Decode the Dukascopy tick archive once into M5 bars, H1 bars and a spread profile.

Reading 11,817 LZMA files takes minutes; the backtest that uses them runs in
seconds and is run many times over (baseline, then a dozen controlled variants).
So the decode happens here, once, and everything downstream loads parquet.

Bars are written through `algo.data.parquet_feed.write_parquet_bars`, which
stores prices as **strings** for the reason that module states: a float64 cannot
hold every tick-grid price exactly, and a backtest that loses a cent a bar to
binary rounding lies quietly.

    python scripts/build_xauusd_dataset.py --ticks "F:/algo trading/data/dukascopy/XAUUSD"

The H1 series is regridded from the M5 series rather than decoded separately.
They are identical either way - five divides sixty - and one pass over the
archive is enough.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from algo.core.bar import Timeframe
from algo.data.dukascopy import (
    SpreadReservoir,
    bars_with_spread,
    hour_files,
    iter_ticks,
    regrid_bars,
    save_spread_series,
)
from algo.data.mt5_spread import save_profile
from algo.data.parquet_feed import write_parquet_bars

M5 = Timeframe(minutes=5)
H1 = Timeframe(minutes=60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=Path, required=True, help="the symbol directory")
    parser.add_argument("--out", type=Path, default=Path("state/xauusd"))
    parser.add_argument("--symbol", default="XAUUSD")
    args = parser.parse_args()

    files = hour_files(args.ticks)
    if not files:
        raise SystemExit(f"no tick files under {args.ticks}")
    print(f"{len(files):,} hourly files, {files[0][0]:%Y-%m-%d} .. {files[-1][0]:%Y-%m-%d}")

    reservoir = SpreadReservoir()

    def stream():
        """One pass: every tick feeds the bars and the spread reservoir alike.

        Bars are built from the whole stream rather than file by file because a
        tick at exactly 10:00:00.000 lives in the 10h file but closes the bar
        stamped 10:00 - aggregating per file would emit that bar twice, once
        from each side of the boundary.
        """
        for seen, tick in enumerate(iter_ticks(args.ticks), start=1):
            if seen % 5_000_000 == 0:
                print(f"  {seen:,} ticks, at {tick.ts:%Y-%m-%d}")
            reservoir.add(tick)
            yield tick

    m5, m5_spreads = bars_with_spread(stream(), M5)

    if not m5:
        raise SystemExit("the archive decoded to no bars at all")

    # A five-minute bar never straddles an hour boundary, so a duplicate stamp
    # here would mean the stream was not ordered. Asserted rather than assumed.
    stamps = [b.ts for b in m5]
    if len(set(stamps)) != len(stamps):
        raise SystemExit("duplicate M5 timestamps - the tick stream is out of order")

    h1 = regrid_bars(m5, H1)
    profile = reservoir.profile(symbol=args.symbol, measured_at=datetime.now(UTC))

    args.out.mkdir(parents=True, exist_ok=True)
    write_parquet_bars(m5, args.out / "m5.parquet")
    write_parquet_bars(h1, args.out / "h1.parquet")
    save_profile(profile, args.out / "spread.json")
    save_spread_series(m5, m5_spreads, args.out / "m5_spread.csv")

    print()
    print(f"M5   {len(m5):,} bars  {m5[0].ts} .. {m5[-1].ts}")
    print(f"H1   {len(h1):,} bars  {h1[0].ts} .. {h1[-1].ts}")
    print(f"ticks {reservoir.ticks:,}")
    print(f"spread {profile.describe()}")
    tight, wide = profile.tightest_hour, profile.widest_hour
    if tight and wide:
        print(f"  tightest hour-of-week {tight[1]}  widest {wide[1]}")
    ordered = sorted(m5_spreads)
    print(
        f"per-bar spread  median {ordered[len(ordered) // 2]}  "
        f"p10 {ordered[len(ordered) // 10]}  p90 {ordered[len(ordered) * 9 // 10]}"
    )
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
