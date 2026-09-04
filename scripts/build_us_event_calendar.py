"""Build `algo/data/data/us_high_impact_events.csv` from the published schedules.

The dates below are transcribed from the publishing agency's own release
calendar, and the URL each block came from is in the comment above it. Keeping
the transcription in a script rather than hand-editing the CSV means the
timezone arithmetic - 08:30 and 14:00 *Eastern*, which is UTC-5 or UTC-4
depending on the date - happens once, in code, against `zoneinfo`, instead of
being done in someone's head 300 times.

Run it after adding a year:

    python scripts/build_us_event_calendar.py
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
OUT = Path(__file__).resolve().parents[1] / "algo" / "data" / "data" / "us_high_impact_events.csv"

BLS = "bls.gov/schedule/{year}/home.htm"
FED = "federalreserve.gov/monetarypolicy/fomccalendars.htm"

# ---------------------------------------------------------------- BLS, 08:30 ET
# bls.gov/schedule/2024/home.htm, /2025/, /2026/. The 2025 list is short by two
# releases: October's Employment Situation was never published and November's
# CPI was not either, following that autumn's lapse in appropriations. Those are
# absent from the schedule because they did not happen.
EMPLOYMENT_SITUATION = """
2024-01-05 2024-02-02 2024-03-08 2024-04-05 2024-05-03 2024-06-07
2024-07-05 2024-08-02 2024-09-06 2024-10-04 2024-11-01 2024-12-06
2025-01-10 2025-02-07 2025-03-07 2025-04-04 2025-05-02 2025-06-06
2025-07-03 2025-08-01 2025-09-05 2025-11-20 2025-12-16
2026-01-09 2026-02-11 2026-03-06 2026-04-03 2026-05-08 2026-06-05
2026-07-02 2026-08-07 2026-09-04 2026-10-02 2026-11-06 2026-12-04
"""

CPI = """
2024-01-11 2024-02-13 2024-03-12 2024-04-10 2024-05-15 2024-06-12
2024-07-11 2024-08-14 2024-09-11 2024-10-10 2024-11-13 2024-12-11
2025-01-15 2025-02-12 2025-03-12 2025-04-10 2025-05-13 2025-06-11
2025-07-15 2025-08-12 2025-09-11 2025-10-24 2025-12-18
2026-01-13 2026-02-13 2026-03-11 2026-04-10 2026-05-12 2026-06-10
2026-07-14 2026-08-12 2026-09-11 2026-10-14 2026-11-10 2026-12-10
"""

PPI = """
2024-01-12 2024-02-16 2024-03-14 2024-04-11 2024-05-14 2024-06-13
2024-07-12 2024-08-13 2024-09-12 2024-10-11 2024-11-14 2024-12-12
2025-01-14 2025-02-13 2025-03-13 2025-04-11 2025-05-15 2025-06-12
2025-07-16 2025-08-14 2025-09-10 2025-11-25
2026-01-14 2026-01-30 2026-02-27 2026-03-18 2026-04-14 2026-05-13
2026-06-11 2026-07-15 2026-08-13 2026-09-10 2026-10-15 2026-11-13
2026-12-15
"""

# ------------------------------------------------------- FOMC, statement 14:00 ET
# federalreserve.gov/monetarypolicy/fomccalendars.htm - the second day of each
# scheduled two-day meeting, which is when the statement is issued. Every one of
# these was followed by a press conference at 14:30 ET.
FOMC = """
2024-01-31 2024-03-20 2024-05-01 2024-06-12 2024-07-31 2024-09-18
2024-11-07 2024-12-18
2025-01-29 2025-03-19 2025-05-07 2025-06-18 2025-07-30 2025-09-17
2025-10-29 2025-12-10
2026-01-28 2026-03-18 2026-04-29 2026-06-17 2026-07-29 2026-09-16
2026-10-28 2026-12-09
"""


def days(block: str) -> list[str]:
    return block.split()


def at(day: str, clock: time) -> datetime:
    """`day` at `clock` Eastern, as UTC. DST is resolved by the date itself."""
    naive = datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(), clock)
    return naive.replace(tzinfo=EASTERN).astimezone(UTC)


def main() -> None:
    rows: list[dict[str, str]] = []

    def add(day: str, clock: time, name: str, category: str, source: str) -> None:
        rows.append(
            {
                "ts_utc": at(day, clock).isoformat(),
                "name": name,
                "category": category,
                "impact": "high",
                "source": source,
            }
        )

    for day in days(EMPLOYMENT_SITUATION):
        add(day, time(8, 30), "Employment Situation (NFP)", "nfp", BLS.format(year=day[:4]))
    for day in days(CPI):
        add(day, time(8, 30), "Consumer Price Index", "cpi", BLS.format(year=day[:4]))
    for day in days(PPI):
        add(day, time(8, 30), "Producer Price Index", "ppi", BLS.format(year=day[:4]))
    for day in days(FOMC):
        add(day, time(14, 0), "FOMC rate decision", "fomc", FED)
        add(day, time(14, 30), "FOMC press conference", "fomc_press", FED)

    rows.sort(key=lambda r: (r["ts_utc"], r["name"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as handle:
        handle.write(
            "# High-impact scheduled US economic events, transcribed from the\n"
            "# publishing agency's own release calendar by\n"
            "# scripts/build_us_event_calendar.py. Times are UTC, converted from\n"
            "# the published Eastern time. NFP/CPI/PPI 08:30 ET; FOMC statement\n"
            "# 14:00 ET and press conference 14:30 ET.\n"
            "#\n"
            "# NOT covered, because no primary schedule was to hand: retail sales,\n"
            "# GDP, PCE, ISM, ADP, jobless claims, Fed speeches outside the\n"
            "# statement window, and every non-US release.\n"
        )
        writer = csv.DictWriter(
            handle,
            fieldnames=["ts_utc", "name", "category", "impact", "source"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} events to {OUT}")
    print(f"  span {rows[0]['ts_utc']} .. {rows[-1]['ts_utc']}")


if __name__ == "__main__":
    main()
