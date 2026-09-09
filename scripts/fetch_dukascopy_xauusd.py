"""Bulk-download the Dukascopy XAUUSD tick archive into the local cache.

The deliberate act `algo/data/dukascopy.py` refuses to perform on its own. One
file per UTC hour; 2024-01-01 to now is roughly 23,000 files and a few
gigabytes, so this is resumable by construction - every body is written to the
cache as it arrives, an hour already on disk is skipped without a request, and
killing this and starting it again loses only the hour in flight.

## Politeness, and why the concurrency is low

Dukascopy publishes this archive free and asks nothing for it. `--workers`
defaults to 6, which keeps a home connection busy without behaving like a
scraper, and every failure backs off rather than retrying immediately. Raising
it is possible and is not encouraged.

## What "missing" means

A 404 is a real answer - weekends, holidays and the daily break have no file -
and is cached as an empty body so a resumed run does not ask twice. Anything
else raises and is counted as a failure; failures are reported at the end with
their hours, because a study built on a range with silent holes in it is worse
than one that refused to start.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algo.core.errors import DataError
from algo.data.dukascopy import DEFAULT_CACHE, cache_path, fetch_hour, hours_between

SYMBOL = "XAUUSD"

#: Attempts per hour before it is counted as a failure, and the pause between
#: them. Linear rather than exponential: the archive's failures here are
#: transient network ones, not rate limiting.
ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def pull(hour: datetime, root: Path) -> tuple[datetime, int, str | None]:
    """Fetch one hour, retrying transient failures. Returns (hour, bytes, error)."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return hour, len(fetch_hour(SYMBOL, hour, root=root)), None
        except (DataError, OSError) as exc:
            if attempt == ATTEMPTS:
                return hour, 0, f"{type(exc).__name__}: {exc}"
            time.sleep(BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser(description="download Dukascopy XAUUSD ticks")
    parser.add_argument("--start", default="2024-01-01", help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD, exclusive (default: today)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--root", default=str(DEFAULT_CACHE))
    args = parser.parse_args()

    root = Path(args.root)
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = (
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC)
        if args.end
        else datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    )

    every = list(hours_between(start, end))
    todo = [h for h in every if not cache_path(SYMBOL, h, root).exists()]
    print(f"{SYMBOL}  {start:%Y-%m-%d} .. {end:%Y-%m-%d}")
    print(f"{len(every):,} hours in range, {len(every) - len(todo):,} already cached, "
          f"{len(todo):,} to fetch, {args.workers} workers")
    if not todo:
        print("nothing to do")
        return

    began = time.monotonic()
    downloaded = empty = failed = 0
    total_bytes = 0
    failures: list[tuple[datetime, str]] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(pull, hour, root): hour for hour in todo}
        for done, future in enumerate(as_completed(futures), start=1):
            hour, size, error = future.result()
            if error is not None:
                failed += 1
                failures.append((hour, error))
            elif size == 0:
                empty += 1
            else:
                downloaded += 1
                total_bytes += size
            if done % 250 == 0 or done == len(todo):
                elapsed = time.monotonic() - began
                rate = done / elapsed if elapsed else 0
                left = (len(todo) - done) / rate if rate else 0
                print(
                    f"  {done:,}/{len(todo):,}  {total_bytes / 1e6:,.0f} MB  "
                    f"{rate:.1f}/s  ~{left / 60:.0f} min left  "
                    f"(empty {empty:,}, failed {failed:,})",
                    flush=True,
                )

    print()
    print(f"downloaded {downloaded:,} hours, {total_bytes / 1e6:,.0f} MB")
    print(f"empty (weekend/holiday/no ticks): {empty:,}")
    print(f"failed: {failed:,}")
    for hour, error in failures[:20]:
        print(f"  {hour:%Y-%m-%d %H}h  {error}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20:,} more")
    if failures:
        print()
        print("Re-run to retry only the failures - cached hours are skipped.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
