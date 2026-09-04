"""The 4-EMA trend-pullback strategy, as rules and a state machine.

Same split as `rsi_stoch_reversal.py`: this module decides what the rules say
about a set of numbers, and `algo/backtest/xauusd_runner.py` decides what that
costs and when it fills. Every function here is pure, so each rule is tested
against a handful of floats rather than against a hundred thousand bars.

## Why this is a state machine and not a per-bar predicate

The specification is explicit that a pullback belongs to a setup and that one
pullback may produce at most one entry. A per-bar predicate cannot express
either: it would re-fire on every candle that happens to satisfy the
confirmation test, and it would have no memory of the pullback's low, which is
where the stop goes. So the setup is a small explicit machine:

    NO_SETUP -> TREND -> PULLBACK -> (entry) -> back to TREND

and the transitions out of it - trend invalid, the 50 EMA broken, the setup gone
stale - are as much a part of the rule as the entries.

## Three decisions the specification leaves open

Each is a named parameter with a stated default rather than a number buried in
an expression, because each changes which trades happen and a reader is
entitled to disagree.

**Can one candle be both the pullback and the confirmation?** "After a
qualifying pullback candle, wait for a bullish confirmation candle" reads as a
later candle, so `allow_same_candle_confirmation` defaults False. A candle that
dips into the zone and closes strongly above EMA9 is then a pullback, and the
next bullish candle above EMA9 confirms it.

**How long may a setup wait?** The specification says a setup may become "stale
according to the strategy implementation" and leaves it there. `stale_after`
defaults to 12 candles - one hour on this timeframe. Without a limit a pullback
can linger for days while its recorded low drifts further away, which quietly
turns into a wider and wider stop.

**How far does the stop reference reach?** `stop_price` is the lowest low from
the first pullback candle through the confirmation candle inclusive. The
confirmation candle is part of the setup, and its low is often the actual
turning point.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from algo.core.bar import Bar
from algo.core.enums import Side


class Phase(Enum):
    """Where a setup has got to. `NO_SETUP` and `TREND` differ only in whether
    the trend filter passes right now, which is worth keeping separate: a setup
    is destroyed by the trend failing, not merely paused."""

    NO_SETUP = "no setup"
    TREND = "trend"
    PULLBACK = "pullback"


@dataclass(frozen=True, slots=True)
class EmaPullbackParams:
    """Every number the rules use. Defaults are the specified baseline exactly."""

    ema_fast: int = 9
    ema_pullback: int = 20
    ema_trend: int = 50
    ema_major: int = 200

    #: Monetary risk per trade. Position size follows from this and the distance
    #: to the structural stop; it is not a lot count.
    risk_per_trade: Decimal = Decimal("10")

    #: Added beyond the pullback extreme before the stop is placed. Zero by
    #: default: the specification asks for the pullback's own low, and any
    #: buffer is an addition to the rule rather than part of it.
    stop_buffer: Decimal = Decimal("0")

    #: Candles a pullback may wait for its confirmation before the setup is
    #: abandoned. See the module docstring - this is a decision, not a reading.
    stale_after: int | None = 12

    #: Whether one candle may be both the pullback and the confirmation.
    allow_same_candle_confirmation: bool = False

    allow_long: bool = True
    allow_short: bool = True

    def label(self) -> str:
        directions = "long+short"
        if not self.allow_short:
            directions = "long only"
        elif not self.allow_long:
            directions = "short only"
        stale = "never" if self.stale_after is None else f"{self.stale_after} bars"
        return (
            f"EMA {self.ema_fast}/{self.ema_pullback}/{self.ema_trend}/{self.ema_major}, "
            f"risk ${self.risk_per_trade}, stale {stale}, {directions}"
        )


BASELINE = EmaPullbackParams()


@dataclass(frozen=True, slots=True)
class Emas:
    """The four averages on one candle, as the rules read them."""

    fast: float
    pullback: float
    trend: float
    major: float

    @property
    def usable(self) -> bool:
        """False while any of them is still warming up.

        NaN compares False against everything, so a warmup candle would fail
        each ordering test and silently look like "no trend" - which is the
        right answer for the wrong reason. Checking explicitly means the warmup
        is visible rather than accidental.
        """
        return all(v == v for v in (self.fast, self.pullback, self.trend, self.major))


def trend_side(emas: Emas, close: float, params: EmaPullbackParams = BASELINE) -> Side | None:
    """The specified trend filter: full EMA stack, and price the right side of
    the 200.

    Strict inequalities throughout. Two EMAs exactly equal is not a stacked
    trend, and on a five-minute chart of a quiet hour that happens.
    """
    if not emas.usable:
        return None
    if (
        params.allow_long
        and emas.fast > emas.pullback > emas.trend > emas.major
        and close > emas.major
    ):
        return Side.BUY
    if (
        params.allow_short
        and emas.fast < emas.pullback < emas.trend < emas.major
        and close < emas.major
    ):
        return Side.SELL
    return None


def is_pullback(bar: Bar, emas: Emas, side: Side) -> bool:
    """Does this candle's range reach into the EMA9-EMA20 zone?

    In a valid uptrend EMA9 sits above EMA20, so the zone is `[EMA20, EMA9]` and
    a candle intersects it when its low is at or below EMA9 and its high at or
    above EMA20 - which is the specification's own wording. The short case is
    the mirror, and the two are written out separately rather than collapsed
    into an absolute-value trick, because the asymmetry is the point.
    """
    if not emas.usable:
        return False
    if side is Side.BUY:
        return float(bar.low) <= emas.fast and float(bar.high) >= emas.pullback
    return float(bar.high) >= emas.fast and float(bar.low) <= emas.pullback


def holds_trend_ema(bar: Bar, emas: Emas, side: Side) -> bool:
    """The pullback must not decisively break the 50 EMA.

    "Decisively" is made objective as the specification directs: the close, not
    the wick. A candle may trade through the 50 and close back above it without
    invalidating anything, which is what a pullback in a trend looks like.
    """
    if not emas.usable:
        return False
    if side is Side.BUY:
        return float(bar.close) >= emas.trend
    return float(bar.close) <= emas.trend


def is_confirmation(bar: Bar, emas: Emas, side: Side) -> bool:
    """A candle in the setup's own direction that closes past the fast EMA."""
    if not emas.usable:
        return False
    if side is Side.BUY:
        return bar.close > bar.open and float(bar.close) > emas.fast
    return bar.close < bar.open and float(bar.close) < emas.fast


@dataclass(frozen=True, slots=True)
class Setup:
    """A live setup. Frozen and replaced rather than mutated, so a state
    transition is always a visible assignment rather than a field changing
    somewhere in a branch."""

    phase: Phase
    side: Side | None = None
    #: Extreme of the pullback so far - the lowest low for a long, the highest
    #: high for a short. This is where the stop goes.
    extreme: Decimal | None = None
    #: Candles since the pullback began, for the staleness rule.
    age: int = 0

    @property
    def armed(self) -> bool:
        return self.phase is Phase.PULLBACK and self.side is not None


NOTHING = Setup(phase=Phase.NO_SETUP)


@dataclass(frozen=True, slots=True)
class Decision:
    """What one candle did to the setup, and whether it produced an entry."""

    setup: Setup
    entry: Side | None = None
    stop_price: Decimal | None = None
    #: Why a setup was abandoned, for the signal log. Empty when nothing was.
    invalidated: str = ""


def advance(
    setup: Setup,
    bar: Bar,
    emas: Emas,
    params: EmaPullbackParams = BASELINE,
) -> Decision:
    """Feed one closed candle to the machine and get the next state.

    Reads this candle and the setup carried in from the last one, and nothing
    else. That is the whole look-ahead argument for the signal side of this
    strategy: there is no series to index into, so there is no index to get
    wrong.
    """
    side = trend_side(emas, float(bar.close), params)

    # --- a live setup, tested for the things that destroy it ----------------
    if setup.armed:
        assert setup.side is not None
        if side is not setup.side:
            # The trend that justified the setup is gone. Not "paused" - the
            # specification lists an invalid trend as an invalidation, and a
            # pullback whose trend has failed is just a downtrend.
            return Decision(setup=_start(side), invalidated="trend no longer valid")
        if not holds_trend_ema(bar, emas, setup.side):
            return Decision(setup=_start(side), invalidated="closed through the 50 EMA")

        extreme = _extend(setup.extreme, bar, setup.side)
        age = setup.age + 1
        if params.stale_after is not None and age > params.stale_after:
            return Decision(setup=_start(side), invalidated="setup went stale")

        if is_confirmation(bar, emas, setup.side):
            buffered = (
                extreme - params.stop_buffer
                if setup.side is Side.BUY
                else extreme + params.stop_buffer
            )
            # Back to TREND, not to PULLBACK: one pullback produces one entry,
            # and a fresh pullback candle is required before another.
            return Decision(
                setup=Setup(phase=Phase.TREND, side=side),
                entry=setup.side,
                stop_price=buffered,
            )

        return Decision(setup=replace(setup, extreme=extreme, age=age))

    # --- no live setup: can this candle start one? --------------------------
    if side is None:
        return Decision(setup=NOTHING)

    if is_pullback(bar, emas, side) and holds_trend_ema(bar, emas, side):
        started = Setup(
            phase=Phase.PULLBACK,
            side=side,
            extreme=bar.low if side is Side.BUY else bar.high,
            age=0,
        )
        if params.allow_same_candle_confirmation and is_confirmation(bar, emas, side):
            buffered = (
                started.extreme - params.stop_buffer  # type: ignore[operator]
                if side is Side.BUY
                else started.extreme + params.stop_buffer  # type: ignore[operator]
            )
            return Decision(
                setup=Setup(phase=Phase.TREND, side=side),
                entry=side,
                stop_price=buffered,
            )
        return Decision(setup=started)

    return Decision(setup=Setup(phase=Phase.TREND, side=side))


def _start(side: Side | None) -> Setup:
    return Setup(phase=Phase.TREND, side=side) if side is not None else NOTHING


def _extend(extreme: Decimal | None, bar: Bar, side: Side) -> Decimal:
    """Carry the pullback's extreme forward across a candle."""
    if extreme is None:
        return bar.low if side is Side.BUY else bar.high
    return min(extreme, bar.low) if side is Side.BUY else max(extreme, bar.high)
