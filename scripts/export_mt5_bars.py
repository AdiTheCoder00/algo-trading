"""Export bars from a MetaTrader 5 terminal to CSV, so a study can run anywhere.

Every measurement script here reads the terminal directly, which means they only
run on the Windows machine the terminal is on. That is fine when the person
running the study and the person holding the terminal are the same. When they
are not - a session in a container, a laptop without MT5, a second opinion from
someone else - the bars have to leave the terminal first, and this is the one
step that has to happen on the broker's machine.

The output is exactly what `algo/data/csv_feed.read_csv_bars` reads, written by
`write_csv_bars`, so a `--csv` run of a measurement script scores the same bars
the terminal holds. Prices go out as their exact decimal strings; nothing is
rounded on the way through.

## The timestamps are converted, not copied

MT5 stamps bars in the broker's server time, which is neither UTC nor the
machine's timezone - Vantage runs GMT+2/+3, and it changes with DST. The offset
is measured against a real clock by `measure_server_offset` and subtracted, so
what lands in the file is UTC. This matters more than it sounds: the pivot rules
draw their lines per session, so a series that is an hour out draws different R
and S lines and can flip a result's sign (D-157 measured exactly that).

## Only closed bars

Position 1, not 0. The forming bar's close still moves, and a study that scored
it would be scoring a number that was not final - the same exclusion
`Mt5BarFeed.closed_bars` makes.

Usage, on the machine with the terminal open and logged in:

    pip install -e ".[dev,mt5]"
    python scripts/export_mt5_bars.py
    python scripts/export_mt5_bars.py --symbol XAUUSD --symbol BTCUSD
    python scripts/export_mt5_bars.py --symbol "Volatility 100 Index" --bars 60000

Then hand over whatever lands in `bars/`.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from algo.core.bar import Bar, Timeframe
from algo.data.csv_feed import write_csv_bars

#: The three the cascade study wants: M5 is the rule, M15 and M30 the
#: robustness check. The names are the CSV suffix and the `--csv TF=PATH` key.
TIMEFRAMES = {
    "M5": (Timeframe(minutes=5), "TIMEFRAME_M5"),
    "M15": (Timeframe(minutes=15), "TIMEFRAME_M15"),
    "M30": (Timeframe(minutes=30), "TIMEFRAME_M30"),
}

#: Enough M5 bars to cover D-140's oldest window plus its warmup, with room to
#: spare. MT5 returns what it has, so asking for too many costs nothing but a
#: shorter file; asking for too few silently truncates the oldest window.
DEFAULT_BARS = 200_000

DEFAULT_SYMBOLS = ("XAUUSD",)
DEFAULT_OUT = Path("bars")


def export(symbol: str, *, count: int, out_dir: Path) -> list[Path]:
    """Write one CSV per timeframe for `symbol`. Returns the paths written.

    The MT5 import is inside the function because the package is a Windows-only
    extra and this module is imported by nothing else; the failure should be
    "you are not on the terminal's machine", not an ImportError at startup.
    """
    import MetaTrader5 as mt5

    from algo.data.mt5_feed import measure_server_offset

    if not mt5.initialize():
        raise SystemExit(
            f"could not attach to MetaTrader 5: {mt5.last_error()}. The terminal "
            "has to be open and logged in, and this has to be the same Windows "
            "user that runs it."
        )
    try:
        if not mt5.symbol_select(symbol, True):
            raise SystemExit(
                f"could not select {symbol!r}: {mt5.last_error()}. Spell it exactly "
                "as your Market Watch spells it - brokers differ, and a volatility "
                "index in particular is named several ways."
            )
        offset = measure_server_offset(mt5, symbol)
        print(f"{symbol}: server clock is UTC{offset.total_seconds() / 3600:+.1f}h")

        written: list[Path] = []
        for name, (timeframe, constant) in TIMEFRAMES.items():
            raw = mt5.copy_rates_from_pos(symbol, getattr(mt5, constant), 1, count)
            if raw is None or len(raw) == 0:
                print(f"  {name}: no bars returned - skipped")
                continue
            bars = [
                Bar(
                    ts=datetime.fromtimestamp(int(row["time"]), UTC) - offset,
                    timeframe=timeframe,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["tick_volume"]),
                )
                for row in raw
            ]
            bars.sort(key=lambda b: b.ts)
            # A filename safe on Windows: a volatility index has spaces in it.
            stem = "".join(c if c.isalnum() else "_" for c in symbol).strip("_").lower()
            path = out_dir / f"{stem}_{name.lower()}.csv"
            write_csv_bars(bars, path)
            written.append(path)
            print(
                f"  {name}: {len(bars):,} bars, "
                f"{bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d} -> {path}"
            )
        return written
    finally:
        # Always, so a failure part-way through does not leave the terminal
        # holding a connection this process no longer owns.
        mt5.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help=(
            "a symbol as Market Watch spells it. Repeatable. "
            f"Default: {', '.join(DEFAULT_SYMBOLS)}"
        ),
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help=f"bars to request per timeframe (default {DEFAULT_BARS:,})",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default {DEFAULT_OUT})"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = tuple(args.symbol) or DEFAULT_SYMBOLS
    args.out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for symbol in symbols:
        written.extend(export(symbol, count=args.bars, out_dir=args.out))

    if not written:
        print("\nNothing was written.")
        return 1
    print(f"\n{len(written)} files in {args.out.resolve()}. To score XAUUSD here:")
    print(
        "  python scripts/measure_pivot_ema_cascade_xauusd.py "
        f"--csv M5={args.out}/xauusd_m5.csv "
        f"--csv M15={args.out}/xauusd_m15.csv --csv M30={args.out}/xauusd_m30.csv"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
