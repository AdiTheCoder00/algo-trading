"""Historical MT5 bars, and the cached server clock that makes them readable.

Two things here can corrupt every result quietly, and both are about the offset
rather than the prices.

**A reused offset that should have been refused.** The whole point of this module
is that research runs at the weekend, when no fresh tick exists and the offset has
to come from disk. The module's own docstring bounds that with `CACHE_MAX_AGE`,
because a cached offset that straddles a daylight-saving transition shifts every
bar by an hour - and an hour is a session boundary. The tests below pin the limit
from both sides, and pin that a corrupt or foreign-symbol cache is treated as
absent rather than as an answer.

**The forming bar.** `copy_rates_from_pos` from position 0 includes the bar still
being built, whose high, low and close can all still change. `fetch_history` reads
from position 1 for the same reason `Mt5BarFeed` does, and that is asserted on the
call itself rather than inferred from the output - a returned bar count cannot
distinguish "excluded the forming bar" from "the terminal had one fewer".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from algo.core.bar import Timeframe
from algo.core.errors import DataError
from algo.data.mt5_history import (
    CACHE_MAX_AGE,
    ResolvedOffset,
    fetch_history,
    resolve_server_offset,
)

TF = Timeframe(minutes=30)
#: A fixed UTC instant, so nothing here depends on the wall clock.
NOW = datetime(2026, 8, 28, 19, 24, tzinfo=UTC)
SERVER_OFFSET = timedelta(hours=3)
SYMBOL = "XAUUSD"

_DTYPE = np.dtype(
    [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "i8"),
    ]
)


class FakeTick:
    def __init__(self, ts: datetime) -> None:
        # MT5 hands back server-local seconds, so this is the naive server wall
        # clock read as if it were UTC - exactly what the real API does.
        self.time = int(ts.replace(tzinfo=UTC).timestamp())


class FakeTerminal:
    """Stands in for the MetaTrader5 module. Server time is +3h by default.

    Implements the whole `Mt5Terminal` protocol, including the four members this
    module never calls, so it can be passed where one is expected without a
    `type: ignore` at every call site. That is what the protocol is for; the
    alternative used elsewhere in the suite is a dozen suppressions that would
    also hide a genuinely wrong argument.
    """

    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(self, *, tick: bool = True, bars: int = 3) -> None:
        self.offset = SERVER_OFFSET
        self._tick = tick
        self._bars = bars
        #: Every (start_pos, count) asked for, so the forming-bar rule is testable.
        self.calls: list[tuple[int, int]] = []

    # -- the protocol members this module never reaches, present so the fake
    #    satisfies `Mt5Terminal` structurally.
    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def symbol_info(self, symbol: str) -> Any:
        return None

    def last_error(self) -> tuple[int, str]:
        return (-10005, "IPC timeout")

    def symbol_info_tick(self, symbol: str) -> FakeTick | None:
        return FakeTick(NOW + self.offset) if self._tick else None

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> NDArray[np.void] | None:
        self.calls.append((start_pos, count))
        if self._bars == 0:
            return None
        rows = []
        for i in range(self._bars):
            server_close = NOW + self.offset - timedelta(minutes=30 * (self._bars - 1 - i))
            rows.append(
                (
                    int(server_close.replace(tzinfo=UTC).timestamp()),
                    4450.5 + i,
                    4460.5 + i,
                    4440.5 + i,
                    4455.5 + i,
                    1000 + i,
                )
            )
        return np.array(rows, dtype=_DTYPE)


def _cache_file(path: Path, symbol: str, *, measured_at: datetime, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {symbol: {"measured_at": measured_at.isoformat(), "offset_seconds": seconds}}
        ),
        encoding="utf-8",
    )
    return path


# ------------------------------------------------- resolve_server_offset
class TestMeasuringLive:
    def test_a_fresh_tick_is_measured_and_reported_as_measured(
        self, tmp_path: Path
    ) -> None:
        resolved = resolve_server_offset(
            FakeTerminal(), SYMBOL, cache_path=tmp_path / "offset.json", now=NOW
        )

        assert resolved.offset == SERVER_OFFSET
        assert resolved.measured_now is True
        assert resolved.measured_at == NOW

    def test_a_measurement_is_cached_for_the_weekend(self, tmp_path: Path) -> None:
        """The write is the whole point: without it the Saturday run has nothing
        to fall back on, which is the case this module exists for."""
        cache = tmp_path / "offset.json"
        resolve_server_offset(FakeTerminal(), SYMBOL, cache_path=cache, now=NOW)

        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert stored[SYMBOL]["offset_seconds"] == SERVER_OFFSET.total_seconds()
        assert datetime.fromisoformat(stored[SYMBOL]["measured_at"]) == NOW

    def test_caching_one_symbol_preserves_the_others(self, tmp_path: Path) -> None:
        """One file holds every symbol, so a write must merge. Clobbering would
        silently cost the other symbols their weekend fallback."""
        cache = _cache_file(
            tmp_path / "offset.json", "EURUSD", measured_at=NOW, seconds=7200.0
        )
        resolve_server_offset(FakeTerminal(), SYMBOL, cache_path=cache, now=NOW)

        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert set(stored) == {"EURUSD", SYMBOL}
        assert stored["EURUSD"]["offset_seconds"] == 7200.0

    def test_an_unwritable_cache_does_not_fail_the_measurement(
        self, tmp_path: Path
    ) -> None:
        """The offset the caller asked for was measured successfully. Failing to
        record it for next time is not a reason to refuse it now."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")

        resolved = resolve_server_offset(
            FakeTerminal(), SYMBOL, cache_path=blocker / "offset.json", now=NOW
        )

        assert resolved.measured_now is True
        assert resolved.offset == SERVER_OFFSET

    def test_a_corrupt_cache_is_overwritten_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / "offset.json"
        cache.write_text("{not json", encoding="utf-8")

        resolve_server_offset(FakeTerminal(), SYMBOL, cache_path=cache, now=NOW)

        assert json.loads(cache.read_text(encoding="utf-8"))[SYMBOL]["offset_seconds"] == (
            SERVER_OFFSET.total_seconds()
        )


class TestFallingBackToTheCache:
    def test_no_tick_but_a_fresh_cache_reuses_it_and_says_it_is_cached(
        self, tmp_path: Path
    ) -> None:
        """The weekend case. Reported as cached, never dressed up as a live
        measurement - which is what `measured_now` exists to keep honest."""
        measured_at = NOW - timedelta(days=2)
        cache = _cache_file(
            tmp_path / "offset.json", SYMBOL, measured_at=measured_at, seconds=10800.0
        )

        resolved = resolve_server_offset(
            FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
        )

        assert resolved.offset == SERVER_OFFSET
        assert resolved.measured_now is False
        assert resolved.measured_at == measured_at
        assert "cached" in resolved.describe(now=NOW)

    def test_a_measurement_then_a_tickless_run_round_trips_through_disk(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / "offset.json"
        first = resolve_server_offset(FakeTerminal(), SYMBOL, cache_path=cache, now=NOW)
        second = resolve_server_offset(
            FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW + timedelta(days=1)
        )

        assert first.offset == second.offset
        assert first.measured_now and not second.measured_now


class TestRefusingRatherThanGuessing:
    def test_no_tick_and_no_cache_says_what_to_do_about_it(self, tmp_path: Path) -> None:
        cache = tmp_path / "offset.json"
        with pytest.raises(DataError) as exc:
            resolve_server_offset(
                FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
            )

        message = str(exc.value)
        assert SYMBOL in message
        assert str(cache) in message
        assert "while the market is open" in message

    def test_a_cache_past_the_age_limit_is_refused_over_daylight_saving(
        self, tmp_path: Path
    ) -> None:
        """The one case where reusing would be worse than failing: a cache from
        the previous DST regime shifts every bar by an hour, and an hour is a
        session boundary."""
        cache = _cache_file(
            tmp_path / "offset.json",
            SYMBOL,
            measured_at=NOW - CACHE_MAX_AGE - timedelta(seconds=1),
            seconds=10800.0,
        )

        with pytest.raises(DataError, match="daylight-saving"):
            resolve_server_offset(
                FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
            )

    def test_a_cache_exactly_at_the_limit_is_still_accepted(self, tmp_path: Path) -> None:
        """The boundary is `>`, not `>=`. Pinned because a limit that quietly
        moved by a day would be invisible either way round."""
        cache = _cache_file(
            tmp_path / "offset.json",
            SYMBOL,
            measured_at=NOW - CACHE_MAX_AGE,
            seconds=10800.0,
        )

        resolved = resolve_server_offset(
            FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
        )
        assert resolved.measured_now is False

    def test_a_corrupt_cache_is_treated_as_missing_never_as_an_offset(
        self, tmp_path: Path
    ) -> None:
        """A wrong offset is worse than no offset, so unreadable means absent."""
        cache = tmp_path / "offset.json"
        cache.write_text("{ this is not json", encoding="utf-8")

        with pytest.raises(DataError, match="No cached offset"):
            resolve_server_offset(
                FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
            )

    def test_a_cache_for_a_different_symbol_is_not_borrowed(self, tmp_path: Path) -> None:
        """Brokers can quote different instruments from different servers; one
        symbol's offset is not evidence about another's."""
        cache = _cache_file(
            tmp_path / "offset.json", "EURUSD", measured_at=NOW, seconds=10800.0
        )

        with pytest.raises(DataError, match="No cached offset"):
            resolve_server_offset(
                FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
            )

    def test_a_cache_entry_missing_its_fields_is_treated_as_missing(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / "offset.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({SYMBOL: {"measured_at": NOW.isoformat()}}), "utf-8")

        with pytest.raises(DataError, match="No cached offset"):
            resolve_server_offset(
                FakeTerminal(tick=False), SYMBOL, cache_path=cache, now=NOW
            )


# ------------------------------------------------------------ fetch_history
class TestFetchHistoryRefusals:
    def test_an_unlisted_timeframe_lists_the_ones_that_exist(self) -> None:
        """Refused rather than guessed at: MT5 has no 7-minute constant, and
        inventing one would mean requesting a timeframe nobody measured."""
        with pytest.raises(DataError, match="no 7-minute timeframe"):
            fetch_history(
                FakeTerminal(),
                symbol=SYMBOL,
                timeframe=Timeframe(minutes=7),
                count=10,
                offset=SERVER_OFFSET,
            )

    def test_a_terminal_without_the_constant_is_refused(self) -> None:
        class Older(FakeTerminal):
            TIMEFRAME_M30 = None  # type: ignore[assignment]

        with pytest.raises(DataError, match="no TIMEFRAME_M30"):
            fetch_history(
                Older(), symbol=SYMBOL, timeframe=TF, count=10, offset=SERVER_OFFSET
            )

    @pytest.mark.parametrize("count", [0, -1])
    def test_a_non_positive_count_is_refused(self, count: int) -> None:
        with pytest.raises(DataError, match="count must be at least 1"):
            fetch_history(
                FakeTerminal(), symbol=SYMBOL, timeframe=TF, count=count,
                offset=SERVER_OFFSET,
            )

    def test_no_bars_back_carries_the_terminal_error(self) -> None:
        """The terminal's own reason is the useful half of the message - "still
        downloading history" and "symbol not selected" need different fixes."""
        with pytest.raises(DataError, match="IPC timeout"):
            fetch_history(
                FakeTerminal(bars=0), symbol=SYMBOL, timeframe=TF, count=10,
                offset=SERVER_OFFSET,
            )


class TestFetchHistoryBars:
    def test_it_reads_from_position_one_so_the_forming_bar_is_excluded(self) -> None:
        """Asserted on the call, not the output: a bar count cannot distinguish
        "skipped the forming bar" from "the terminal had one fewer"."""
        terminal = FakeTerminal()
        fetch_history(
            terminal, symbol=SYMBOL, timeframe=TF, count=5, offset=SERVER_OFFSET
        )

        assert terminal.calls == [(1, 5)]

    def test_bars_are_stamped_in_utc_by_removing_the_server_offset(self) -> None:
        """The server stamps +3h; the engine works in UTC. Getting the sign wrong
        would move every bar six hours, not zero."""
        bars = fetch_history(
            FakeTerminal(bars=1), symbol=SYMBOL, timeframe=TF, count=5,
            offset=SERVER_OFFSET,
        )

        assert bars[0].ts == NOW
        assert bars[0].ts.tzinfo is not None

    def test_bars_come_back_oldest_first(self) -> None:
        bars = fetch_history(
            FakeTerminal(bars=3), symbol=SYMBOL, timeframe=TF, count=5,
            offset=SERVER_OFFSET,
        )

        assert [b.ts for b in bars] == sorted(b.ts for b in bars)
        assert bars[-1].ts - bars[0].ts == timedelta(minutes=60)

    def test_prices_survive_as_exact_decimals(self) -> None:
        """Through `str`, never `Decimal(float)`: 4450.5 must not arrive as
        4450.50000000000018189894035458564758300781250."""
        bars = fetch_history(
            FakeTerminal(bars=1), symbol=SYMBOL, timeframe=TF, count=5,
            offset=SERVER_OFFSET,
        )

        assert bars[0].open == Decimal("4450.5")
        assert bars[0].close == Decimal("4455.5")
        assert bars[0].volume == 1000

    def test_the_timeframe_is_carried_onto_every_bar(self) -> None:
        bars = fetch_history(
            FakeTerminal(bars=3), symbol=SYMBOL, timeframe=TF, count=5,
            offset=SERVER_OFFSET,
        )

        assert all(b.timeframe == TF for b in bars)


# ------------------------------------------------------ ResolvedOffset.describe
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
    assert _cached().describe(now=MEASURED_AT + timedelta(hours=30)) == (
        "3:00:00 (cached 30h ago)"
    )


def test_the_age_floors_to_whole_hours() -> None:
    """59 minutes is "0h ago", not "1h ago". Rounding up would let a nearly-fresh
    offset read as an hour old."""
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
