"""`ResolvedOffset.describe` - the one line a person reads to decide whether the
MT5 server clock they are trading against was measured or remembered.

Narrow on purpose. `describe` gained an injectable `now` when the four wall-clock
reads in the MT5 modules moved behind the clock module (D-015), and that parameter
arrived untested; these cover it. The rest of `algo/data/mt5_history.py` -
`resolve_server_offset`, the cache read and write, the staleness refusal - still
has no tests at all, which is worth knowing when reading this file and finding it
short.

The distinction being pinned is not cosmetic. "measured just now" and "cached 30h
ago" are the difference between an offset that reflects the broker's clock and one
that reflects it as of yesterday, and every bar timestamp is shifted by it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from algo.data.mt5_history import ResolvedOffset

OFFSET = timedelta(hours=3)
MEASURED_AT = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


def _cached(measured_at: datetime = MEASURED_AT) -> ResolvedOffset:
    return ResolvedOffset(offset=OFFSET, measured_now=False, measured_at=measured_at)


def test_a_fresh_measurement_says_so_and_never_consults_a_clock() -> None:
    """`measured_now` short-circuits before any clock is read, which is what makes
    the wording trustworthy: it cannot drift into "cached 0h ago"."""
    resolved = ResolvedOffset(offset=OFFSET, measured_now=True, measured_at=MEASURED_AT)

    assert resolved.describe() == "3:00:00 (measured just now)"
    # A wildly wrong `now` changes nothing, because it is never reached.
    assert resolved.describe(now=datetime(1999, 1, 1, tzinfo=UTC)) == (
        "3:00:00 (measured just now)"
    )


def test_a_cached_offset_reports_its_age_against_the_injected_now() -> None:
    """The point of the parameter: the age comes from `now`, not the wall clock.

    30 hours is chosen to be far from any plausible real elapsed time, so a
    version that ignored `now` and read the system clock could not produce it.
    """
    resolved = _cached()
    later = MEASURED_AT + timedelta(hours=30)

    assert resolved.describe(now=later) == "3:00:00 (cached 30h ago)"


def test_the_age_floors_to_whole_hours() -> None:
    """59 minutes is "0h ago", not "1h ago". Rounding up would let a nearly-stale
    offset read as staler than it is, and a nearly-fresh one as an hour old."""
    resolved = _cached()

    assert "cached 0h ago" in resolved.describe(now=MEASURED_AT + timedelta(minutes=59))
    assert "cached 1h ago" in resolved.describe(now=MEASURED_AT + timedelta(minutes=60))
    assert "cached 1h ago" in resolved.describe(now=MEASURED_AT + timedelta(minutes=119))


def test_the_offset_itself_is_always_shown() -> None:
    """Whichever branch runs, the number that shifts every bar is on screen."""
    fresh = ResolvedOffset(offset=OFFSET, measured_now=True, measured_at=MEASURED_AT)

    assert str(OFFSET) in fresh.describe()
    assert str(OFFSET) in _cached().describe(now=MEASURED_AT + timedelta(hours=2))


def test_a_negative_offset_is_rendered_rather_than_swallowed() -> None:
    """Brokers west of UTC produce one, and a sign lost here is a sign lost in
    every timestamp derived from it."""
    behind = ResolvedOffset(
        offset=timedelta(hours=-5), measured_now=True, measured_at=MEASURED_AT
    )

    assert behind.describe().startswith("-1 day, 19:00:00")
