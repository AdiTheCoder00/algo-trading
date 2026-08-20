"""Session-anchored bar resampling.

Brief §7.5: "Resampling higher timeframes from lower ones must use only completed
higher-TF bars — the current H4 bar is unusable until it closes." That rule is
enforced structurally here: a bar is emitted only once the source data proves its
close boundary has passed. An in-progress bar is never produced, so nothing
downstream has to remember not to use one.

Anchoring is to the **session open**, not to the wall clock. An MCX session runs
09:00–23:30 or 09:00–23:55 IST depending on US daylight saving, so the number of
30-minute bars in a day is 29 in one regime and 29-plus-a-stub in the other. Naive
`resample('30min')` on a UTC index gets both the boundaries and the count wrong.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from algo.core.bar import Bar, Timeframe
from algo.core.errors import DataError
from algo.core.timeutil import ist_date
from algo.exchange.calendar import BarBoundary, MarketCalendar


def check_source(bars: Sequence[Bar]) -> None:
    """Reject a source series that cannot be resampled correctly.

    Duplicates and out-of-order timestamps are checked here rather than tolerated,
    because both produce a plausible-looking output bar built from the wrong
    inputs — the worst kind of data bug, since nothing downstream will complain.
    """
    seen: set[object] = set()
    previous: Bar | None = None
    for bar in bars:
        if bar.ts in seen:
            raise DataError(f"duplicate source bar at {bar.ts}")
        seen.add(bar.ts)
        if previous is not None and bar.ts <= previous.ts:
            raise DataError(f"source bars out of order: {previous.ts} then {bar.ts}")
        previous = bar


def resample(
    source: Iterable[Bar],
    *,
    calendar: MarketCalendar,
    timeframe: Timeframe,
    keep_partial: bool = True,
    drop_out_of_session: bool = False,
) -> list[Bar]:
    """Aggregate finer bars into session-anchored bars of `timeframe`.

    Source bars are close-labelled, so a source bar belongs to the boundary whose
    half-open interval `(open_ts, close_ts]` contains its timestamp. That single
    convention decides everything else; getting it wrong by one bar is precisely
    the look-ahead the brief is written against.
    """
    materialised = list(source)
    check_source(materialised)
    if not materialised:
        return []

    if any(b.timeframe.minutes > timeframe.minutes for b in materialised):
        raise DataError("cannot resample to a timeframe finer than the source")

    by_session: dict[date, list[Bar]] = {}
    stray: list[Bar] = []
    for bar in materialised:
        session_day = ist_date(bar.ts)
        if not calendar.is_trading_day(session_day):
            stray.append(bar)
            continue
        by_session.setdefault(session_day, []).append(bar)

    if stray and not drop_out_of_session:
        sample = ", ".join(str(b.ts) for b in stray[:3])
        raise DataError(
            f"{len(stray)} source bars fall on non-trading days (e.g. {sample}). "
            "Pass drop_out_of_session=True only if that is genuinely expected."
        )

    output: list[Bar] = []
    last_ts = materialised[-1].ts

    for session_day in sorted(by_session):
        session_bars = by_session[session_day]
        boundaries = calendar.bar_boundaries(session_day, timeframe)
        out_of_session = [
            b
            for b in session_bars
            if b.ts <= boundaries[0].open_ts or b.ts > boundaries[-1].close_ts
        ]
        if out_of_session and not drop_out_of_session:
            sample = ", ".join(str(b.ts) for b in out_of_session[:3])
            raise DataError(
                f"{len(out_of_session)} bars on {session_day} fall outside the session "
                f"window (e.g. {sample})"
            )

        for boundary in boundaries:
            if boundary.is_partial and not keep_partial:
                continue
            # §7.5 — only emit once the source proves this boundary has passed.
            if last_ts < boundary.close_ts:
                continue
            members = [b for b in session_bars if boundary.open_ts < b.ts <= boundary.close_ts]
            if not members:
                continue
            output.append(_aggregate(members, boundary, timeframe))

    return output


def _aggregate(members: list[Bar], boundary: BarBoundary, timeframe: Timeframe) -> Bar:
    volume = sum(b.volume for b in members)
    return Bar(
        ts=boundary.close_ts,
        timeframe=timeframe,
        open=members[0].open,
        high=max(b.high for b in members),
        low=min(b.low for b in members),
        close=members[-1].close,
        volume=volume,
        open_interest=members[-1].open_interest,
        is_partial=boundary.is_partial,
    )


def expected_bar_count(calendar: MarketCalendar, on: date, timeframe: Timeframe) -> int:
    """How many bars a complete session should produce. Used by coverage checks."""
    return len(calendar.bar_boundaries(on, timeframe))
