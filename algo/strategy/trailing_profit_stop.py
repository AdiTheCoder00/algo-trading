"""A trailing stop that only arms once a minimum favourable move is banked.

Distinct from the flat, entry-anchored stop in `price_stop.py`: that one
bounds the downside from bar one. This one does nothing until a position has
moved `activation_pct` in its favour, then follows the best price seen since
entry at a fixed `trail_pct` distance - giving a winning trade room to run
while locking in an increasing share of it as the peak advances. This module
makes no claim about downside protection before the activation threshold, and
does not need to - a caller that wants both runs them side by side; see
`MacdCrossover`'s and `TrendlineBreakout`'s own docstrings for how each one
combines the two.

## The peak updates before the trail is checked, in the same bar

A bar's OHLC does not say whether the high or the low printed first. This
module resolves that ambiguity the same direction every time: the peak is
advanced to the bar's best-case price first, then the trail level - now
anchored to that new peak - is checked against the bar's worst-case price.
That is the standard convention most bar-granularity backtesters use for a
trailing stop, and it matches what a real trailing-stop order actually does:
it moves the moment a new best price prints, mid-bar, same as this.

## Percent of the peak, not of entry

"0.5% trailing" is read the way most retail platforms present it: 0.5% below
(long) or above (short) the *current* best price, not a fixed distance fixed
at entry. The gap in absolute terms therefore widens as the peak advances -
the position is given more room the further it has already run.

## The level never gives back past entry - "cost to cost"

Once a trail has anything to trail (the peak has moved at all past entry), its
level is clamped so it can never sit worse than `entry_price` - the common
"move the stop to cost" trade-management rule. Without this, a large enough
`trail_pct` relative to `activation_pct` could let an armed trail give back
*more* than the entire banked move, closing what started as a genuine winner
at a loss - defeating the purpose of a *profit* trail, which is to lock in
gains, not to relabel a loss as delayed. With the clamp, the worst outcome
once a trail level is ever computed is a scratch at entry, never worse -
independent of which `activation_pct`/`trail_pct` combination a caller picks,
rather than relying on the two happening to be sized so the clamp is never
actually reached.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from algo.core.bar import Bar
from algo.core.enums import Side


@dataclass(frozen=True)
class TrailState:
    """The running state a trailing stop needs: which side, what it entered
    at, and the best (most favourable) price seen since."""

    side: Side
    entry_price: Decimal
    peak: Decimal


def start_trail(entry_price: Decimal, side: Side) -> TrailState:
    """A fresh trail, peak seeded at entry - nothing banked yet."""
    return TrailState(side=side, entry_price=entry_price, peak=entry_price)


def advance_trail(state: TrailState, bar: Bar) -> TrailState:
    """Extend the peak to this bar's best-case favourable price. The peak
    only ever moves further favourable - it never retreats just because the
    bar's own close came back in."""
    peak = max(state.peak, bar.high) if state.side is Side.BUY else min(state.peak, bar.low)
    return replace(state, peak=peak)


def _favourable_move_pct(state: TrailState) -> Decimal:
    move = (
        state.peak - state.entry_price
        if state.side is Side.BUY
        else state.entry_price - state.peak
    )
    return move / state.entry_price * Decimal("100")


def is_armed(state: TrailState, activation_pct: Decimal) -> bool:
    """Whether the peak has ever reached `activation_pct` in the position's
    favour. Once true it stays true for this trail - the peak only advances,
    never retreats, so an activation that has fired cannot un-fire."""
    return _favourable_move_pct(state) >= activation_pct


def trail_level(state: TrailState, trail_pct: Decimal) -> Decimal:
    """Where the trailing stop currently sits, `trail_pct` behind the peak -
    clamped so it never sits worse than `entry_price` (the "cost to cost"
    invariant; see the module docstring)."""
    give_back = state.peak * trail_pct / Decimal("100")
    if state.side is Side.BUY:
        return max(state.peak - give_back, state.entry_price)
    return min(state.peak + give_back, state.entry_price)


def trail_touched(
    state: TrailState, bar: Bar, activation_pct: Decimal, trail_pct: Decimal
) -> bool:
    """Whether the trail is armed *and* this bar's range crossed it.

    `trail_pct <= 0` means no trail is configured; always False, mirroring
    `price_stop.stop_touched`'s convention so a caller need not branch on
    whether trailing is enabled before asking this.
    """
    if trail_pct <= 0 or not is_armed(state, activation_pct):
        return False
    level = trail_level(state, trail_pct)
    if state.side is Side.BUY:
        return bar.low <= level
    return bar.high >= level


def trail_fill_price(state: TrailState, bar: Bar, trail_pct: Decimal) -> Decimal:
    """Where a trail-triggered exit fills: the trail level itself, or the
    bar's open on a gap through it - the same `GAPPED_STOP` reasoning
    `price_stop.stop_fill_price` already applies.

    Only meaningful when `trail_touched` is True; does not itself re-check
    that, since a caller only reaches here after already confirming it.
    """
    level = trail_level(state, trail_pct)
    if state.side is Side.BUY:
        return bar.open if bar.open <= level else level
    return bar.open if bar.open >= level else level
