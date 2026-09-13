"""The study's funnel: it must describe a funnel.

`scripts/measure_pivot_ema_cascade_xauusd.py` reports, per direction, how many
armed cascades reached each of the rule's six steps. The counts are cumulative
("reached at least this step"), so they can only fall from left to right. When
they rise, the diagnostic is miscounting rather than the market being strange -
and a reader has no way to tell those apart from the table alone.

That is not hypothetical: the column for entries was first credited from the
signals while the earlier columns were credited from the strategy's published
stage, and a cascade that arms and completes inside ONE bar - the rule's own
"or on the same candle" case - is never observed in a non-zero stage at all. It
was counted as an entry having never been counted as an attempt, and the first
run on real bars printed 12 attempts reaching the 100 EMA and 15 entries past
the 200.

The script is not a package, so its directory goes on the path the same way
`tests/test_cascade_alert.py` reaches the alert tool.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import measure_pivot_ema_cascade_xauusd as study  # noqa: E402

from algo.core.bar import Bar, Timeframe  # noqa: E402

TF = Timeframe(minutes=5)
START = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def _bar(close: float, ts: datetime) -> Bar:
    value = Decimal(str(close))
    return Bar(ts=ts, timeframe=TF, open=value, high=value, low=value, close=value, volume=0)


def _one_bar_collapse() -> list[Bar]:
    """A series whose only entry arms and completes on a single bar.

    Three quiet sessions give the pivots a range and the 200 EMA its warmup;
    the fourth climbs above R3 and then drops far enough in one bar to close
    below every EMA at once. That bar is the case the funnel used to lose.
    """
    days = [
        [4000.0] * 200,
        [4000.0] * 200,
        [4000.0 + (10.0 if i % 2 else -10.0) for i in range(200)],
        [4000.0 + 0.4 * i for i in range(199)] + [3500.0],
    ]
    return [
        _bar(close, START + timedelta(days=day, minutes=5 * index))
        for day, closes in enumerate(days)
        for index, close in enumerate(closes)
    ]


def test_the_funnel_never_rises_from_left_to_right() -> None:
    reached = study.cascade_funnel(_one_bar_collapse(), TF)
    for side in ("short", "long"):
        counts = reached[side][1:]
        assert counts == sorted(counts, reverse=True), (
            f"{side} funnel rises: {counts} - an attempt is being counted at one "
            "step without being counted at the steps before it"
        )


def test_a_one_bar_cascade_is_counted_as_an_attempt_at_every_step() -> None:
    """The specific regression: the entry exists, so every column must see it."""
    reached = study.cascade_funnel(_one_bar_collapse(), TF)
    entries = reached["short"][len(study.FUNNEL_STEPS)]
    assert entries >= 1, "the fixture is meant to produce a short entry"
    for step, name in enumerate(study.FUNNEL_STEPS, start=1):
        assert reached["short"][step] >= entries, (
            f"{entries} entries but only {reached['short'][step]} attempts reached "
            f"the {name} - an entry reached every step by definition"
        )


def test_an_attempt_that_never_arms_is_not_counted() -> None:
    """A flat series arms nothing, so every column is zero.

    Without this the two tests above would pass on a funnel that counted every
    bar as an attempt.
    """
    flat = [_bar(4000.0, START + timedelta(minutes=5 * i)) for i in range(800)]
    reached = study.cascade_funnel(flat, TF)
    assert reached["short"][1:] == [0] * len(study.FUNNEL_STEPS)
    assert reached["long"][1:] == [0] * len(study.FUNNEL_STEPS)
