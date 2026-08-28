"""A percentage price stop, shared by the CFD strategies that have one.

Neither `MacdCrossover` nor `TrendlineBreakout` originally carried a stop -
each said so plainly in its own docstring, alongside why: the alert tool this
mirrors has none, and the Donchian channel's own opposing-breakout exit was
judged sufficient on its own. Both statements stay true; a stop was requested
afterward; this is the shared, tested piece that adds it identically to both
rather than two copies that could quietly drift apart.

## The trigger checks the bar's range, not its close

`algo/risk/exits.py` already states the doctrine this follows: "the stop is
tested first. If a bar moved far enough to touch both [stop and target] - which
on a gap it can - the pessimistic reading is that the stop went first." That
module is written for the MCX bhavcopy path, which has no intrabar data to test
against and says so (Q15: bar-close evaluation only, "optimistic relative to
live on fast moves").

The MT5 bars this module actually runs against **do** have real highs and lows.
Checking only the close here would systematically *understate* how often the
stop fires - a bar that spikes through the level and closes back inside would
never trigger a close-only check, even though a real broker-side stop order
would have filled during that spike. Understating a safety feature's own
frequency is the wrong direction to be optimistic in, so this checks the bar's
low (for a long) or high (for a short) against the level, exactly as
`ExitLevels.check`'s own doctrine already prescribes for MCX - carried over
here because the data finally allows it to be checked properly rather than
approximated.
"""

from __future__ import annotations

from decimal import Decimal

from algo.core.bar import Bar
from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.core.position import Position


def stop_level(entry_price: Decimal, side: Side, stop_pct: Decimal) -> Decimal:
    """The absolute price at which a `stop_pct` adverse move from `entry_price`
    sits, for a position on `side`.

    A long's stop sits below entry; a short's sits above it - the move is
    against the position, not against the market's own direction.
    """
    if stop_pct < 0:
        raise DomainError(f"stop_pct cannot be negative, got {stop_pct}")
    move = entry_price * stop_pct / Decimal("100")
    return entry_price - move if side is Side.BUY else entry_price + move


def stop_touched(bar: Bar, held: Position, stop_pct: Decimal) -> bool:
    """Whether `bar`'s actual range crossed the stop for `held`'s position.

    `stop_pct <= 0` means no stop is configured; always False, so a caller does
    not need to branch on whether a stop exists before asking this.
    """
    if stop_pct <= 0 or held.is_flat:
        return False
    side = Side.BUY if held.qty > 0 else Side.SELL
    level = stop_level(held.average_price, side, stop_pct)
    if side is Side.BUY:
        return bar.low <= level
    return bar.high >= level


def stop_fill_price(bar: Bar, held: Position, stop_pct: Decimal) -> Decimal:
    """Where a stop-triggered exit fills: the stop level itself, or the bar's
    open if the bar gapped straight past it before the level was ever
    tradeable - the same `GAPPED_STOP` reasoning `algo/execution/fills.py`
    already applies to the MCX path (`ExitCheck`, price `bar.open` on a gap).

    Only meaningful when `stop_touched` is True; raises otherwise rather than
    silently returning a level nothing actually crossed.
    """
    if not stop_touched(bar, held, stop_pct):
        raise DomainError("stop_fill_price called for a bar the stop was not touched on")
    side = Side.BUY if held.qty > 0 else Side.SELL
    level = stop_level(held.average_price, side, stop_pct)
    if side is Side.BUY:
        return bar.open if bar.open <= level else level
    return bar.open if bar.open >= level else level
