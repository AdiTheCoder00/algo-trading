"""Liquidity sweep -> reclaim -> displacement -> MSS -> FVG -> retracement entry.

The rule set in one sentence: price takes out a **known** level of resting
liquidity, closes back through it, moves away with a candle materially larger
than the recent average, breaks the last confirmed swing in that direction, and
leaves an imbalance behind on the way - and the trade is a resting limit order
back into that imbalance, stopped beyond the sweep and targeted at a fixed
multiple of the risk.

Every one of those words is a number here. "Takes out" is a low that trades at
least `min_sweep_distance` below the level; "closes back through" is a close on
the far side within `reclaim_max_bars`; "materially larger" is a body at least
`displacement_multiplier` times the mean of the last `displacement_period`
bodies; "breaks structure" is a **close** past a swing that was confirmed
before the break was looked for; "imbalance" is a three-candle gap wider than
`min_fvg_size`. Nothing in this file asks whether a chart looks strong.

## Why a state machine and not a stack of `if`s

A setup is a sequence, and each step is only meaningful *after* the previous
one. The same close that is a reclaim in one context is nothing in another, and
a flat conditional would have to re-derive which context it was in from the
bars every time. `_Setup` holds that context explicitly - which level was
swept, how far, when, what has been confirmed so far - so the question each bar
asks is "does this bar advance *this* setup", and the answer is one comparison.
It also means every abandoned setup can say exactly which step it died on,
which is what `SetupRecord` records and what the rejected-setup log is for.

## Incremental, because live and backtest must agree

ATR, the mean body, the swing buffer, the session levels and the hourly EMA are
all carried forward bar by bar rather than recomputed from a window. Partly
speed - a quarter of a million bars times a recomputed window is quadratic -
but mostly correctness: a Wilder average is path-dependent, so a window
recompute is a *different number*, and a strategy whose ATR depends on how much
history the caller happened to pass is not the same strategy in the live loop
as in the study. `WilderAtr` carries the running value and
`tests/test_liquidity_sweep.py` pins it bar for bar against
`algo.pricing.indicators.atr`.

## No look-ahead, structurally

Three separate guards, because this rule set has three ways to cheat:

1. **Swings are confirmed late on purpose.** A swing high at bar `i` needs
   `swing_lookback` bars *after* it to close before it is a swing at all, so it
   enters the level set at bar `i + lookback` and never earlier. The buffer
   here is filled from `_recent`, which only ever contains closed bars.
2. **The MSS level is frozen when displacement confirms**, not chosen later
   from whatever swing best fits the break. A level picked after the break
   would be a repaint.
3. **The entry is a resting limit order.** The strategy emits it on the close
   of the bar that identified the FVG and never revises it; the runner decides
   whether a *later* bar traded into it. There is no path by which the bar that
   fills the order can influence the order.

## Prices are mid, and the strategy never sees a spread

The bars this reads are mid (`algo.data.dukascopy`), so every level it computes
- the sweep threshold, the stop, the FVG edges, the target - is a mid price.
Crossing the spread is the runner's job and is charged there, once, visibly.
That is why `risk_distance` here is a mid-to-mid distance while the R actually
realised is smaller: the report states both rather than choosing.

## What this strategy deliberately does not do

No sizing (the base class's contract: "a strategy that computes lot size is a
bug"), no trailing, no break-even move, no partial exits, no averaging. The
exits travel with the signal - `stop_price`, one `TakeProfit`, and a maximum
hold in the context - and the runner enforces them. Adding a trail later means
adding it there and in `_target_price`, not unpicking this file.

`state()` is left empty. Everything here is a pure function of the bars already
seen, so a restart re-warms by replaying history rather than by restoring a
snapshot that could disagree with it; `warmup_bars()` says how many bars that
takes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from algo.core.bar import Bar
from algo.core.enums import Side, SignalAction
from algo.core.errors import DomainError
from algo.core.fill import Fill
from algo.core.ids import signal_id
from algo.core.instrument import CfdId, InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg, TakeProfit
from algo.core.timeutil import iso
from algo.pricing.indicators import WilderAtr
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext

# --------------------------------------------------------------- vocabularies


class LiquiditySource(StrEnum):
    """Where a level of resting liquidity comes from.

    The `_HIGH` sources are buy-side liquidity - stops of shorts and breakout
    buy orders sitting above - and are swept on the way *up*, which sets up a
    short. The `_LOW` sources are the mirror. `is_buy_side` is the only place
    that mapping is written down.
    """

    ASIAN_HIGH = "ASIAN_HIGH"
    ASIAN_LOW = "ASIAN_LOW"
    PREV_DAY_HIGH = "PREV_DAY_HIGH"
    PREV_DAY_LOW = "PREV_DAY_LOW"
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"

    @property
    def is_buy_side(self) -> bool:
        return self.value.endswith("_HIGH")

    @property
    def sets_up(self) -> Side:
        """The direction a sweep of this source sets up."""
        return Side.SELL if self.is_buy_side else Side.BUY


DEFAULT_SOURCES: tuple[LiquiditySource, ...] = (
    LiquiditySource.ASIAN_HIGH,
    LiquiditySource.ASIAN_LOW,
    LiquiditySource.PREV_DAY_HIGH,
    LiquiditySource.PREV_DAY_LOW,
    LiquiditySource.SWING_HIGH,
    LiquiditySource.SWING_LOW,
)


class SetupState(StrEnum):
    """Where one setup has got to. Every record carries the state it died in."""

    IDLE = "IDLE"
    LIQUIDITY_IDENTIFIED = "LIQUIDITY_IDENTIFIED"
    SWEEP_DETECTED = "SWEEP_DETECTED"
    RECLAIM_CONFIRMED = "RECLAIM_CONFIRMED"
    DISPLACEMENT_CONFIRMED = "DISPLACEMENT_CONFIRMED"
    MSS_CONFIRMED = "MSS_CONFIRMED"
    FVG_IDENTIFIED = "FVG_IDENTIFIED"
    WAITING_FOR_RETRACE = "WAITING_FOR_RETRACE"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_CLOSED = "POSITION_CLOSED"
    SETUP_EXPIRED = "SETUP_EXPIRED"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"


class InvalidationReason(StrEnum):
    """Why a setup stopped. One value per rule, so the log can be counted."""

    NO_RECLAIM = "NO_RECLAIM"
    NO_DISPLACEMENT = "NO_DISPLACEMENT"
    NO_MSS_LEVEL = "NO_MSS_LEVEL"
    NO_MSS = "NO_MSS"
    NO_FVG = "NO_FVG"
    STRUCTURE_BROKEN = "STRUCTURE_BROKEN"
    SETUP_AGED_OUT = "SETUP_AGED_OUT"
    FVG_EXPIRED = "FVG_EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    OUTSIDE_SESSION = "OUTSIDE_SESSION"
    DAILY_LIMIT = "DAILY_LIMIT"
    POSITION_OPEN = "POSITION_OPEN"
    ORDER_PENDING = "ORDER_PENDING"
    HTF_FILTER = "HTF_FILTER"
    NEWS_FILTER = "NEWS_FILTER"
    DEGENERATE_RISK = "DEGENERATE_RISK"


class DistanceMode(StrEnum):
    """How a distance parameter is read: as dollars, or as a fraction of ATR."""

    FIXED = "fixed"
    ATR = "atr"


class EntryMode(StrEnum):
    """Where in the fair-value gap the resting order sits."""

    FVG_MIDPOINT = "midpoint"
    FIRST_TOUCH = "first_touch"
    FVG_BOUNDARY = "boundary"
    CONFIRMATION_CANDLE = "confirmation"


class TpMode(StrEnum):
    FIXED_RR = "fixed_rr"
    OPPOSING_LIQUIDITY = "opposing_liquidity"
    HYBRID = "hybrid"


class MssConfirmation(StrEnum):
    """Only `CLOSE` exists, and the enum exists to say so.

    A wick-break variant is the obvious next knob and is deliberately absent:
    it would double the rule set's surface before the close version has been
    shown to have an edge. The parameter is here so that adding it later is an
    enum member and a branch, not a change to every caller.
    """

    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A trading window on the UTC wall clock, half-open on the bar's own span.

    A bar is *inside* the window when the bar **opened** inside it. Bars are
    close-labelled (`algo.core.bar`), so the 07:00 bar covers (06:55, 07:00] and
    belongs to whatever window 06:55 was in - not to the one starting at 07:00.
    Getting this backwards puts one bar of the Asian range into London on every
    single day.
    """

    name: str
    start: time
    end: time

    def contains(self, ts: datetime) -> bool:
        opened_at = (ts - timedelta(microseconds=1)).time()
        return self.start <= opened_at < self.end


LONDON = SessionWindow("LONDON", time(7, 0), time(10, 0))
NEW_YORK = SessionWindow("NEW_YORK", time(13, 0), time(16, 0))
ASIA = SessionWindow("ASIA", time(0, 0), time(6, 0))


def trading_day(ts: datetime) -> date:
    """The UTC date a close-labelled bar belongs to.

    The bar stamped 00:00 covers (23:55, 00:00] of the day before and is that
    day's last bar, not the new day's first. One line, stated once, because
    every daily level and every daily counter in this file depends on it.
    """
    return (ts - timedelta(microseconds=1)).date()


@dataclass(frozen=True, slots=True)
class LiquiditySweepParams:
    """The whole rule set as data. Section 25 of the brief, one field apiece."""

    swing_lookback: int = 2
    atr_period: int = 14

    sweep_distance_mode: DistanceMode = DistanceMode.ATR
    #: Fraction of ATR when the mode is ATR, dollars when it is FIXED.
    min_sweep_distance: Decimal = Decimal("0.10")
    #: The sweep candle itself is bar 0, so 3 allows the sweep candle and the
    #: two after it to be the one that closes back through the level.
    reclaim_max_bars: int = 3

    displacement_period: int = 10
    displacement_multiplier: Decimal = Decimal("1.5")
    displacement_max_bars: int = 6

    mss_confirmation: MssConfirmation = MssConfirmation.CLOSE

    fvg_size_mode: DistanceMode = DistanceMode.ATR
    min_fvg_size: Decimal = Decimal("0.05")

    entry_mode: EntryMode = EntryMode.FVG_MIDPOINT

    sl_buffer_mode: DistanceMode = DistanceMode.ATR
    sl_buffer: Decimal = Decimal("0.10")

    tp_mode: TpMode = TpMode.FIXED_RR
    rr_target: Decimal = Decimal("2.0")

    #: Bars a setup may live for, measured from the sweep bar, and again from
    #: the FVG for the resting order. Both clocks, one number: section 13 sets
    #: it for the order and section 27 for the setup, and running two different
    #: budgets would need a second justification nobody has.
    max_setup_bars: int = 24
    max_hold: timedelta = timedelta(hours=4)

    max_trades_per_day: int = 2
    one_position_at_a_time: bool = True
    #: When false, a level that has already produced an order today is not
    #: swept again until the next day. Section 20's duplicate rule applied at
    #: the level as well as at the event, which is what stops one Asian low
    #: producing a whole day's trades.
    retrade_same_level: bool = False

    sources: tuple[LiquiditySource, ...] = DEFAULT_SOURCES
    #: How many confirmed swings stay in the level set. One each way is the
    #: default: "the previous confirmed swing high" is a specific level, and
    #: keeping ten of them turns a level set into a fishing net.
    swing_levels: int = 1

    trading_windows: tuple[SessionWindow, ...] = (LONDON, NEW_YORK)
    asian_session: SessionWindow = ASIA

    htf_filter_enabled: bool = False
    htf_ema_period: int = 50

    #: The interface section 21 asks for and nothing more. There is no
    #: historical calendar wired in here, so turning this on raises rather than
    #: reporting zero blocked entries as though a filter had run.
    news_filter_enabled: bool = False

    def describe(self) -> dict[str, str]:
        """Every parameter as a string, for the report and the params hash."""
        return {
            "swing_lookback": str(self.swing_lookback),
            "atr_period": str(self.atr_period),
            "sweep_distance_mode": self.sweep_distance_mode.value,
            "min_sweep_distance": str(self.min_sweep_distance),
            "reclaim_max_bars": str(self.reclaim_max_bars),
            "displacement_period": str(self.displacement_period),
            "displacement_multiplier": str(self.displacement_multiplier),
            "displacement_max_bars": str(self.displacement_max_bars),
            "mss_confirmation": self.mss_confirmation.value,
            "fvg_size_mode": self.fvg_size_mode.value,
            "min_fvg_size": str(self.min_fvg_size),
            "entry_mode": self.entry_mode.value,
            "sl_buffer_mode": self.sl_buffer_mode.value,
            "sl_buffer": str(self.sl_buffer),
            "tp_mode": self.tp_mode.value,
            "rr_target": str(self.rr_target),
            "max_setup_bars": str(self.max_setup_bars),
            "max_hold_minutes": str(int(self.max_hold.total_seconds() // 60)),
            "max_trades_per_day": str(self.max_trades_per_day),
            "one_position_at_a_time": str(self.one_position_at_a_time),
            "retrade_same_level": str(self.retrade_same_level),
            "sources": ",".join(s.value for s in self.sources),
            "swing_levels": str(self.swing_levels),
            "windows": ",".join(
                f"{w.name}:{w.start:%H%M}-{w.end:%H%M}" for w in self.trading_windows
            ),
            "asian_session": (
                f"{self.asian_session.start:%H%M}-{self.asian_session.end:%H%M}"
            ),
            "htf_filter_enabled": str(self.htf_filter_enabled),
            "htf_ema_period": str(self.htf_ema_period),
            "news_filter_enabled": str(self.news_filter_enabled),
        }


BASELINE = LiquiditySweepParams()


# ------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class Fvg:
    """A three-candle imbalance. `low` and `high` are its edges, always sorted."""

    low: Decimal
    high: Decimal
    #: Close timestamp of the third candle - the bar that completed the gap.
    formed_at: datetime
    is_bullish: bool

    @property
    def size(self) -> Decimal:
        return self.high - self.low

    @property
    def midpoint(self) -> Decimal:
        return (self.high + self.low) / Decimal("2")


@dataclass(frozen=True, slots=True)
class SetupRecord:
    """One setup's whole life, as section 33 asks for it.

    Written for every setup, not only the ones that trade. A log that records
    only entries can say what happened and never why something did not, and
    "why did nothing fire in New York on the 3rd" is the question this exists
    to answer.
    """

    timestamp: datetime
    direction: Side
    session: str
    liquidity_type: LiquiditySource
    liquidity_level: Decimal
    sweep_price: Decimal
    sweep_distance: Decimal
    signal_status: SetupState
    #: The furthest stage this setup actually reached, which the terminal
    #: status alone does not say: SETUP_EXPIRED is the same word whether the
    #: setup died waiting for a reclaim or waiting for an imbalance, and those
    #: are different failures to read in the log.
    reached: SetupState = SetupState.SWEEP_DETECTED
    invalidation_reason: InvalidationReason | None = None
    reclaim_price: Decimal | None = None
    displacement_size: Decimal | None = None
    average_body: Decimal | None = None
    mss_level: Decimal | None = None
    fvg_high: Decimal | None = None
    fvg_low: Decimal | None = None
    fvg_size: Decimal | None = None
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    risk_distance: Decimal | None = None
    rr: Decimal | None = None
    atr: Decimal | None = None
    signal_ref: str = ""

    def to_row(self) -> dict[str, str]:
        """Flat, string-valued row for the CSV signal log."""

        def opt(value: object) -> str:
            return "" if value is None else str(value)

        return {
            "timestamp": iso(self.timestamp),
            "direction": self.direction.value,
            "session": self.session,
            "liquidity_type": self.liquidity_type.value,
            "liquidity_level": str(self.liquidity_level),
            "sweep_price": str(self.sweep_price),
            "sweep_distance": str(self.sweep_distance),
            "reclaim_price": opt(self.reclaim_price),
            "displacement_size": opt(self.displacement_size),
            "average_body": opt(self.average_body),
            "mss_level": opt(self.mss_level),
            "fvg_high": opt(self.fvg_high),
            "fvg_low": opt(self.fvg_low),
            "fvg_size": opt(self.fvg_size),
            "entry_price": opt(self.entry_price),
            "stop_price": opt(self.stop_price),
            "target_price": opt(self.target_price),
            "risk_distance": opt(self.risk_distance),
            "rr": opt(self.rr),
            "atr": opt(self.atr),
            "signal_status": self.signal_status.value,
            "reached": self.reached.value,
            "invalidation_reason": (
                "" if self.invalidation_reason is None else self.invalidation_reason.value
            ),
            "signal_ref": self.signal_ref,
        }


SIGNAL_LOG_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "direction",
    "session",
    "liquidity_type",
    "liquidity_level",
    "sweep_price",
    "sweep_distance",
    "reclaim_price",
    "displacement_size",
    "average_body",
    "mss_level",
    "fvg_high",
    "fvg_low",
    "fvg_size",
    "entry_price",
    "stop_price",
    "target_price",
    "risk_distance",
    "rr",
    "atr",
    "signal_status",
    "reached",
    "invalidation_reason",
    "signal_ref",
)


# --------------------------------------------------------------- the rules
# Each rule the brief states as a formula is one function here, taking bars and
# numbers and returning a verdict. They are module level and pure so a test can
# ask "is this a swing high" directly, rather than constructing a whole run and
# inferring the answer from what it traded.


def is_swing_high(window: Sequence[Bar], lookback: int) -> bool:
    """Is the middle bar of `window` a confirmed swing high?

    `window` is exactly `2 * lookback + 1` bars. Strictly higher than the bars
    before it and at least as high as the bars after: the asymmetry is the
    brief's, and it means a flat double top confirms on its first leg rather
    than on neither.
    """
    _require_window(window, lookback)
    candidate = window[lookback]
    before = window[:lookback]
    after = window[lookback + 1 :]
    return all(candidate.high > b.high for b in before) and all(
        candidate.high >= b.high for b in after
    )


def is_swing_low(window: Sequence[Bar], lookback: int) -> bool:
    """The mirror of `is_swing_high`."""
    _require_window(window, lookback)
    candidate = window[lookback]
    before = window[:lookback]
    after = window[lookback + 1 :]
    return all(candidate.low < b.low for b in before) and all(
        candidate.low <= b.low for b in after
    )


def _require_window(window: Sequence[Bar], lookback: int) -> None:
    if len(window) != 2 * lookback + 1:
        raise DomainError(
            f"a swing test needs exactly {2 * lookback + 1} bars for lookback "
            f"{lookback}, got {len(window)}"
        )


def is_sweep(bar: Bar, level: Decimal, threshold: Decimal, *, long_side: bool) -> bool:
    """Did this bar trade through `level` by at least `threshold`?

    A long setup sweeps *sell-side* liquidity - the low must trade below the
    level - and the short side is the mirror. The reclaim is a separate rule
    and deliberately not folded in here.
    """
    return bar.low <= level - threshold if long_side else bar.high >= level + threshold


def is_reclaim(bar: Bar, level: Decimal, *, long_side: bool) -> bool:
    """Did this bar **close** back on the correct side of the level?"""
    return bar.close > level if long_side else bar.close < level


def is_displacement(
    bar: Bar, average_body: Decimal, multiplier: Decimal, *, long_side: bool
) -> bool:
    """A directional candle whose body is `multiplier` times the recent mean."""
    body = abs(bar.close - bar.open)
    directional = bar.close > bar.open if long_side else bar.close < bar.open
    return directional and body >= multiplier * average_body


def is_mss(bar: Bar, level: Decimal, *, long_side: bool) -> bool:
    """A **close** through the frozen structure level. Wicks do not qualify."""
    return bar.close > level if long_side else bar.close < level


def gap_between(first: Bar, third: Bar, *, bullish: bool) -> Fvg | None:
    """The three-candle imbalance between two bars two apart, if there is one.

    The middle candle is not read at all, which is the definition: the gap is
    between candle one and candle three, and candle two is simply the bar that
    moved fast enough to leave it.
    """
    if bullish:
        if first.high >= third.low:
            return None
        return Fvg(low=first.high, high=third.low, formed_at=third.ts, is_bullish=True)
    if first.low <= third.high:
        return None
    return Fvg(low=third.high, high=first.low, formed_at=third.ts, is_bullish=False)


@dataclass(slots=True)
class _Setup:
    """A live setup, one per direction. Mutable because it is a state machine."""

    direction: Side
    source: LiquiditySource
    level: Decimal
    sweep_extreme: Decimal
    sweep_ts: datetime
    sweep_index: int
    state: SetupState = SetupState.SWEEP_DETECTED
    reclaim_price: Decimal | None = None
    reclaim_index: int | None = None
    displacement_size: Decimal | None = None
    average_body: Decimal | None = None
    mss_level: Decimal | None = None
    mss_index: int | None = None
    fvg: Fvg | None = None

    @property
    def sweep_distance(self) -> Decimal:
        return abs(self.level - self.sweep_extreme)


# ------------------------------------------------------------------ strategy


class LiquiditySweepStrategy(Strategy):
    """XAUUSD 5-minute liquidity sweep, market structure shift and fair value gap."""

    strategy_id = "xauusd_liquidity_sweep_v1"

    def __init__(
        self,
        *,
        instrument: InstrumentId | None = None,
        params: LiquiditySweepParams = BASELINE,
        config_hash: str = "",
    ) -> None:
        super().__init__()
        _validate(params)
        self._instrument: InstrumentId = instrument or CfdId(symbol="XAUUSD")
        self._p = params
        self._config_hash = config_hash

        self._atr = WilderAtr(params.atr_period)
        self._bodies: deque[Decimal] = deque(maxlen=params.displacement_period)
        # Long enough to hold a whole setup's worth of history: the FVG search
        # walks back to the sweep bar, and the swing buffer needs the lookback
        # on both sides of a candidate.
        self._recent: deque[Bar] = deque(
            maxlen=max(params.max_setup_bars + 4, 2 * params.swing_lookback + 3)
        )
        self._bar_index = -1

        self._swing_highs: deque[Decimal] = deque(maxlen=max(params.swing_levels, 1))
        self._swing_lows: deque[Decimal] = deque(maxlen=max(params.swing_levels, 1))
        #: Latest confirmed swings, kept apart from the level set because the
        #: MSS level is "the most recent confirmed swing", however many levels
        #: the sweep side is willing to look at.
        self._last_swing_high: Decimal | None = None
        self._last_swing_low: Decimal | None = None

        self._day: date | None = None
        self._day_high: Decimal | None = None
        self._day_low: Decimal | None = None
        self._prev_day_high: Decimal | None = None
        self._prev_day_low: Decimal | None = None
        self._asian_high: Decimal | None = None
        self._asian_low: Decimal | None = None
        self._asian_running_high: Decimal | None = None
        self._asian_running_low: Decimal | None = None

        self._h1_close: Decimal | None = None
        self._h1_ema: float | None = None
        self._h1_seen = 0

        self._long: _Setup | None = None
        self._short: _Setup | None = None
        self._pending_until: datetime | None = None
        self._trades_today = 0
        self._consumed: set[tuple[LiquiditySource, str]] = set()

        self._records: list[SetupRecord] = []

    # ------------------------------------------------------------- contract
    def warmup_bars(self) -> int:
        """Enough bars for ATR, the body average and one confirmed swing.

        The binding gate in practice is not this at all - it is the first
        completed Asian session and the first completed day, both of which the
        level set simply lacks until they have happened.
        """
        return max(
            self._p.atr_period + 1,
            self._p.displacement_period,
            2 * self._p.swing_lookback + 1,
        )

    def params(self) -> dict[str, str]:
        return {"instrument": self._instrument.key, **self._p.describe()}

    def drain_records(self) -> list[SetupRecord]:
        """Take the setup records written since the last call.

        The structured sibling of `drain_notes`: notes are prose for a human
        reading a run, records are rows for the signal log.
        """
        records, self._records = self._records, []
        return records

    def on_fill(self, fill: Fill) -> None:
        """Count an entry against the daily cap, and clear the resting order.

        The daily cap counts *entries*, not signals: a resting order that
        expired unfilled cost nothing and did not use up the day. Only the
        runner knows whether an order filled, so only the runner can tell this,
        which is exactly what the base class's hook is for.
        """
        del fill
        self._trades_today += 1
        self._pending_until = None

    # ----------------------------------------------------------------- bars
    def on_bar(self, ctx: BarContext) -> list[Signal]:
        bar = ctx.bar
        self._bar_index += 1

        self._roll_day(bar)
        self._update_levels(bar)
        atr = self._atr.update(float(bar.high), float(bar.low), float(bar.close))
        self._bodies.append(abs(bar.close - bar.open))
        self._recent.append(bar)
        self._confirm_swing()
        self._update_hourly(bar)

        if self._pending_until is not None and bar.ts > self._pending_until:
            # The resting order's own clock, read the same way the runner reads
            # it - strictly `>`, on the same timestamp, so the bar stamped at
            # the deadline is the last one that can fill and both sides agree
            # about which bar that is. One expiry, emitted with the signal.
            self._pending_until = None

        signals: list[Signal] = []
        for setup in (self._long, self._short):
            if setup is None:
                continue
            signal = self._advance(setup, bar, atr, ctx)
            if signal is not None:
                signals.append(signal)

        # A new sweep is looked for only after the existing ones have had this
        # bar, so a bar that completes one setup cannot also start its twin.
        self._detect_sweep(bar, atr)
        for setup in (self._long, self._short):
            if setup is not None and setup.sweep_index == self._bar_index:
                signal = self._advance(setup, bar, atr, ctx)
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------ daily bookkeeping
    def _roll_day(self, bar: Bar) -> None:
        day = trading_day(bar.ts)
        if day == self._day:
            return
        if self._day is not None:
            self._prev_day_high = self._day_high
            self._prev_day_low = self._day_low
        self._day = day
        self._day_high = None
        self._day_low = None
        self._asian_high = None
        self._asian_low = None
        self._asian_running_high = None
        self._asian_running_low = None
        self._trades_today = 0
        self._consumed.clear()

    def _update_levels(self, bar: Bar) -> None:
        """Roll today's extremes and the Asian range forward.

        Called *before* the bar is used for anything else, and deliberately
        never publishes today's own high or low as a liquidity level: a level
        that includes the current bar cannot be swept by it.
        """
        self._day_high = bar.high if self._day_high is None else max(self._day_high, bar.high)
        self._day_low = bar.low if self._day_low is None else min(self._day_low, bar.low)

        if self._p.asian_session.contains(bar.ts):
            self._asian_running_high = (
                bar.high
                if self._asian_running_high is None
                else max(self._asian_running_high, bar.high)
            )
            self._asian_running_low = (
                bar.low
                if self._asian_running_low is None
                else min(self._asian_running_low, bar.low)
            )
            # Still forming: not a level yet, and not offered as one.
            self._asian_high = None
            self._asian_low = None
        else:
            self._asian_high = self._asian_running_high
            self._asian_low = self._asian_running_low

    def _confirm_swing(self) -> None:
        """Confirm the swing that sits `swing_lookback` bars back, if any.

        The candidate is the bar with `lookback` closed bars either side of it.
        It could not have been confirmed before now, and it is never revisited
        after: that is the whole of the no-repaint guarantee for levels.
        """
        k = self._p.swing_lookback
        if len(self._recent) < 2 * k + 1:
            return
        window: Sequence[Bar] = list(self._recent)[-(2 * k + 1) :]
        candidate = window[k]

        if is_swing_high(window, k):
            self._swing_highs.append(candidate.high)
            self._last_swing_high = candidate.high
        if is_swing_low(window, k):
            self._swing_lows.append(candidate.low)
            self._last_swing_low = candidate.low

    def _update_hourly(self, bar: Bar) -> None:
        """Fold a completed hour into the higher-timeframe EMA.

        Only a bar that *closes* an hour advances it, so the EMA is always the
        last completed hourly candle and never the forming one.
        """
        if not self._p.htf_filter_enabled:
            return
        if bar.ts.minute != 0:
            return
        close = float(bar.close)
        period = self._p.htf_ema_period
        if self._h1_ema is None:
            self._h1_ema = close
        else:
            k = 2.0 / (period + 1)
            self._h1_ema = close * k + self._h1_ema * (1 - k)
        self._h1_close = bar.close
        self._h1_seen += 1

    # -------------------------------------------------------------- levels
    def _levels(self) -> list[tuple[LiquiditySource, Decimal]]:
        """The live level set, in the configured source order.

        Order is the tie-break when one bar sweeps two levels at once, so it is
        the configuration's order and not a dict's iteration order.
        """
        by_source: dict[LiquiditySource, list[Decimal]] = {
            LiquiditySource.ASIAN_HIGH: _one(self._asian_high),
            LiquiditySource.ASIAN_LOW: _one(self._asian_low),
            LiquiditySource.PREV_DAY_HIGH: _one(self._prev_day_high),
            LiquiditySource.PREV_DAY_LOW: _one(self._prev_day_low),
            LiquiditySource.SWING_HIGH: list(self._swing_highs),
            LiquiditySource.SWING_LOW: list(self._swing_lows),
        }
        out: list[tuple[LiquiditySource, Decimal]] = []
        for source in self._p.sources:
            out.extend((source, level) for level in by_source[source])
        return out

    # --------------------------------------------------------- state machine
    def _detect_sweep(self, bar: Bar, atr: float | None) -> None:
        """Start a setup where this bar traded through a level far enough.

        A sweep that is already live in this direction is *extended* rather
        than restarted - a second bar poking deeper below the same low is the
        same excursion, and the stop belongs beyond the deepest point of it.
        """
        if atr is None:
            return
        if not self._in_trading_window(bar.ts):
            return

        threshold = self._distance(
            self._p.sweep_distance_mode, self._p.min_sweep_distance, atr
        )
        for source, level in self._levels():
            if not self._p.retrade_same_level and (source, str(level)) in self._consumed:
                continue
            long_side = not source.is_buy_side
            if not is_sweep(bar, level, threshold, long_side=long_side):
                continue

            existing = self._long if long_side else self._short
            if existing is not None:
                if existing.source is source and existing.level == level:
                    if existing.state is SetupState.SWEEP_DETECTED:
                        existing.sweep_extreme = (
                            min(existing.sweep_extreme, bar.low)
                            if long_side
                            else max(existing.sweep_extreme, bar.high)
                        )
                    break
                if existing.state is not SetupState.SWEEP_DETECTED:
                    # A setup already past the sweep stage is not thrown away
                    # for a fresh sweep: it has done more work and its clock is
                    # already running.
                    break
                self._record(
                    existing, SetupState.SETUP_INVALIDATED, InvalidationReason.SUPERSEDED
                )

            setup = _Setup(
                direction=Side.BUY if long_side else Side.SELL,
                source=source,
                level=level,
                sweep_extreme=bar.low if long_side else bar.high,
                sweep_ts=bar.ts,
                sweep_index=self._bar_index,
            )
            if long_side:
                self._long = setup
            else:
                self._short = setup
            break

    def _advance(
        self, setup: _Setup, bar: Bar, atr: float | None, ctx: BarContext
    ) -> Signal | None:
        """Give one bar to one setup, and return a signal if it completed."""
        age = self._bar_index - setup.sweep_index
        long_side = setup.direction is Side.BUY

        if age > self._p.max_setup_bars:
            self._finish(setup, SetupState.SETUP_EXPIRED, InvalidationReason.SETUP_AGED_OUT)
            return None

        if setup.state is SetupState.SWEEP_DETECTED:
            if long_side:
                setup.sweep_extreme = min(setup.sweep_extreme, bar.low)
            else:
                setup.sweep_extreme = max(setup.sweep_extreme, bar.high)
            if is_reclaim(bar, setup.level, long_side=long_side):
                setup.state = SetupState.RECLAIM_CONFIRMED
                setup.reclaim_price = bar.close
                setup.reclaim_index = self._bar_index
            elif age >= self._p.reclaim_max_bars:
                self._finish(setup, SetupState.SETUP_INVALIDATED, InvalidationReason.NO_RECLAIM)
                return None
            else:
                return None

        if setup.state is SetupState.RECLAIM_CONFIRMED:
            average = self._average_body()
            if average is not None and is_displacement(
                bar, average, self._p.displacement_multiplier, long_side=long_side
            ):
                setup.state = SetupState.DISPLACEMENT_CONFIRMED
                setup.displacement_size = abs(bar.close - bar.open)
                setup.average_body = average
                # Frozen here, before the break is looked for. See the module
                # docstring: a level chosen after the break is a repaint.
                setup.mss_level = self._last_swing_high if long_side else self._last_swing_low
                if setup.mss_level is None:
                    self._finish(
                        setup, SetupState.SETUP_INVALIDATED, InvalidationReason.NO_MSS_LEVEL
                    )
                    return None
            else:
                reclaim_index = setup.reclaim_index or setup.sweep_index
                if self._bar_index - reclaim_index >= self._p.displacement_max_bars:
                    self._finish(
                        setup, SetupState.SETUP_INVALIDATED, InvalidationReason.NO_DISPLACEMENT
                    )
                return None

        if setup.state is SetupState.DISPLACEMENT_CONFIRMED:
            level = setup.mss_level
            if level is None:  # pragma: no cover - set with the state above
                raise DomainError("displacement confirmed without an MSS level")
            if is_mss(bar, level, long_side=long_side):
                setup.state = SetupState.MSS_CONFIRMED
                setup.mss_index = self._bar_index
            else:
                if self._structure_failed(setup, bar):
                    self._finish(
                        setup, SetupState.SETUP_INVALIDATED, InvalidationReason.STRUCTURE_BROKEN
                    )
                return None

        if setup.state is SetupState.MSS_CONFIRMED:
            if self._structure_failed(setup, bar):
                self._finish(
                    setup, SetupState.SETUP_INVALIDATED, InvalidationReason.STRUCTURE_BROKEN
                )
                return None
            if atr is None:
                return None
            gap = self._find_fvg(setup, atr)
            if gap is None:
                return None
            setup.fvg = gap
            setup.state = SetupState.FVG_IDENTIFIED
            return self._order(setup, bar, atr, ctx)

        return None

    def _structure_failed(self, setup: _Setup, bar: Bar) -> bool:
        """Has the setup's own premise been broken by a close?

        For a long, a close back below the sweep low says the excursion the
        trade is fading did not fail - it continued, and the stop this setup
        would have used is already behind price. Section 27's "bearish MSS
        before entry", written as the objective event rather than as a second
        structure engine running in the opposite direction.
        """
        if setup.direction is Side.BUY:
            return bar.close < setup.sweep_extreme
        return bar.close > setup.sweep_extreme

    def _find_fvg(self, setup: _Setup, atr: float) -> Fvg | None:
        """The earliest unfilled gap left by the move out of the sweep.

        Candidates run from the sweep bar to the current one. "Unfilled" is
        strict: no bar after the third candle may have traded into the gap at
        all, because an entry inside a gap price has already revisited is an
        entry at a level that has stopped being an imbalance.
        """
        minimum = self._distance(self._p.fvg_size_mode, self._p.min_fvg_size, atr)
        bars = list(self._recent)
        # `_recent` ends at the current bar, so the current bar's index in it is
        # `len(bars) - 1` and the sweep bar's is that minus its age.
        first = max(0, len(bars) - 1 - (self._bar_index - setup.sweep_index))
        long_side = setup.direction is Side.BUY

        for third in range(first + 2, len(bars)):
            gap = gap_between(bars[third - 2], bars[third], bullish=long_side)
            if gap is None:
                continue
            if gap.size < minimum:
                continue
            after = bars[third + 1 :]
            if long_side and any(b.low <= gap.high for b in after):
                continue
            if not long_side and any(b.high >= gap.low for b in after):
                continue
            return gap
        return None

    # ------------------------------------------------------------- the order
    def _order(self, setup: _Setup, bar: Bar, atr: float, ctx: BarContext) -> Signal | None:
        """Turn a completed setup into a resting limit order, or say why not.

        Every gate below writes a record and returns `None`. A gate that
        silently dropped the setup would make "the strategy saw nothing" and
        "the strategy saw a setup and was not allowed to take it" the same
        entry in the log, and they are completely different facts about a day.
        """
        gap = setup.fvg
        if gap is None:  # pragma: no cover - set by the caller
            raise DomainError("an order was asked for before an FVG was identified")
        long_side = setup.direction is Side.BUY

        entry = self._entry_price(gap, long_side)
        buffer = self._distance(self._p.sl_buffer_mode, self._p.sl_buffer, atr)
        stop = setup.sweep_extreme - buffer if long_side else setup.sweep_extreme + buffer
        risk = entry - stop if long_side else stop - entry
        target = self._target_price(entry, risk, long_side)

        def reject(reason: InvalidationReason) -> None:
            self._finish(
                setup,
                SetupState.SETUP_INVALIDATED,
                reason,
                entry=entry,
                stop=stop,
                target=target,
                risk=risk,
                atr=atr,
            )

        if risk <= 0:
            # The gap sits at or beyond the stop: nothing to risk, so nothing
            # to size against and no order that could be sane.
            reject(InvalidationReason.DEGENERATE_RISK)
            return None
        if not self._in_trading_window(bar.ts):
            reject(InvalidationReason.OUTSIDE_SESSION)
            return None
        if self._trades_today >= self._p.max_trades_per_day:
            reject(InvalidationReason.DAILY_LIMIT)
            return None
        if self._p.one_position_at_a_time and not ctx.positions().is_flat:
            reject(InvalidationReason.POSITION_OPEN)
            return None
        if self._pending_until is not None:
            reject(InvalidationReason.ORDER_PENDING)
            return None
        if not self._htf_allows(setup.direction):
            reject(InvalidationReason.HTF_FILTER)
            return None

        expires_at = bar.ts + timedelta(minutes=self._p.max_setup_bars * bar.timeframe.minutes)
        window_end = self._window_end(bar.ts)
        if window_end is not None:
            expires_at = min(expires_at, window_end)

        leg = SignalLeg(
            instrument=self._instrument,
            direction=setup.direction,
            entry=PriceIntent.limit(entry),
            stop_price=stop,
            take_profits=(TakeProfit(price=target),),
        )
        record = self._build_record(
            setup,
            SetupState.WAITING_FOR_RETRACE,
            None,
            entry=entry,
            stop=stop,
            target=target,
            risk=risk,
            atr=atr,
        )
        sid = signal_id(
            strategy_id=self.strategy_id,
            params_hash=self.params_hash(),
            bar_close_iso=iso(bar.ts),
            action=SignalAction.OPEN.value,
            leg_keys=(f"{self._instrument.key}:{setup.direction}",),
            config_hash=self._config_hash,
        )
        context = dict(record.to_row())
        context["expires_at"] = iso(expires_at)
        context["max_hold_minutes"] = str(int(self._p.max_hold.total_seconds() // 60))
        context["signal_ref"] = sid

        self._records.append(replace(record, signal_ref=sid))
        self._pending_until = expires_at
        self._consume(setup, gap)
        self._clear(setup)
        self.note(
            f"{setup.direction} setup: {setup.source} sweep of {setup.level} to "
            f"{setup.sweep_extreme}, MSS through {setup.mss_level}, limit {entry} "
            f"stop {stop} target {target}"
        )
        return Signal(
            signal_id=sid,
            strategy_id=self.strategy_id,
            ts=bar.ts,
            action=SignalAction.OPEN,
            legs=(leg,),
            reason=(
                f"liquidity sweep {setup.source.value} at {setup.level}: swept to "
                f"{setup.sweep_extreme}, reclaimed, displaced and closed through "
                f"{setup.mss_level}; limit into the {gap.low}-{gap.high} gap"
            ),
            context=context,
        )

    def _entry_price(self, gap: Fvg, long_side: bool) -> Decimal:
        """Where the resting order sits inside the gap.

        `FIRST_TOUCH` is the near edge - the first price of the gap a
        retracement reaches - and `FVG_BOUNDARY` the far one, which fills less
        often and risks less when it does. `CONFIRMATION_CANDLE` rests at the
        near edge too and differs in what the runner does with the touch.
        """
        match self._p.entry_mode:
            case EntryMode.FVG_MIDPOINT:
                return gap.midpoint
            case EntryMode.FIRST_TOUCH | EntryMode.CONFIRMATION_CANDLE:
                return gap.high if long_side else gap.low
            case EntryMode.FVG_BOUNDARY:
                return gap.low if long_side else gap.high

    def _target_price(self, entry: Decimal, risk: Decimal, long_side: bool) -> Decimal:
        fixed = (
            entry + self._p.rr_target * risk if long_side else entry - self._p.rr_target * risk
        )
        if self._p.tp_mode is TpMode.FIXED_RR:
            return fixed
        opposing = self._opposing_level(entry, long_side)
        if self._p.tp_mode is TpMode.OPPOSING_LIQUIDITY:
            return opposing if opposing is not None else fixed
        if opposing is None:
            return fixed
        return min(fixed, opposing) if long_side else max(fixed, opposing)

    def _opposing_level(self, entry: Decimal, long_side: bool) -> Decimal | None:
        """The nearest enabled level on the far side of the entry."""
        candidates = [
            level
            for _source, level in self._levels()
            if (level > entry if long_side else level < entry)
        ]
        if not candidates:
            return None
        return min(candidates) if long_side else max(candidates)

    def _htf_allows(self, side: Side) -> bool:
        if not self._p.htf_filter_enabled:
            return True
        if self._h1_ema is None or self._h1_close is None:
            return False
        if self._h1_seen < self._p.htf_ema_period:
            return False
        bullish = float(self._h1_close) > self._h1_ema
        return bullish if side is Side.BUY else not bullish

    # ------------------------------------------------------------- plumbing
    def _average_body(self) -> Decimal | None:
        """SMA of the last `displacement_period` bodies, current bar included.

        Inclusive because that is what `SMA(abs(Close - Open), 10)` says at bar
        `i`, and the alternative - excluding the candle being judged - is a
        different rule that would need its own justification. The effect of the
        choice is bounded and known: a displacement candle raises its own
        average by a tenth, so the effective multiplier is slightly stricter
        than the parameter, identically for every candle.
        """
        if len(self._bodies) < self._p.displacement_period:
            return None
        return sum(self._bodies, Decimal("0")) / Decimal(len(self._bodies))

    def _distance(self, mode: DistanceMode, value: Decimal, atr: float) -> Decimal:
        if mode is DistanceMode.FIXED:
            return value
        return value * _dec(atr)

    def _in_trading_window(self, ts: datetime) -> bool:
        return any(w.contains(ts) for w in self._p.trading_windows)

    def _window_end(self, ts: datetime) -> datetime | None:
        """When the window this bar sits in closes, in absolute time.

        A resting order must not fill after its session has ended, and the
        strategy is the one place that knows where the windows are - so it
        computes the deadline and the runner obeys it, rather than both owning
        a copy of the session table.
        """
        for window in self._p.trading_windows:
            if window.contains(ts):
                opened = ts - timedelta(microseconds=1)
                return datetime.combine(opened.date(), window.end, tzinfo=ts.tzinfo)
        return None

    def _consume(self, setup: _Setup, gap: Fvg) -> None:
        """Mark the sweep, its level and the gap as used up. Section 20."""
        self._consumed.add((setup.source, str(setup.level)))
        self._consumed.add((setup.source, f"fvg:{gap.low}-{gap.high}@{iso(gap.formed_at)}"))

    def _clear(self, setup: _Setup) -> None:
        if setup.direction is Side.BUY:
            self._long = None
        else:
            self._short = None

    def _finish(
        self,
        setup: _Setup,
        state: SetupState,
        reason: InvalidationReason,
        **extra: object,
    ) -> None:
        self._record(setup, state, reason, **extra)
        self._clear(setup)

    def _record(
        self,
        setup: _Setup,
        state: SetupState,
        reason: InvalidationReason | None,
        **extra: object,
    ) -> None:
        entry = extra.get("entry")
        stop = extra.get("stop")
        target = extra.get("target")
        risk = extra.get("risk")
        atr = extra.get("atr")
        self._records.append(
            self._build_record(
                setup,
                state,
                reason,
                entry=entry if isinstance(entry, Decimal) else None,
                stop=stop if isinstance(stop, Decimal) else None,
                target=target if isinstance(target, Decimal) else None,
                risk=risk if isinstance(risk, Decimal) else None,
                atr=atr if isinstance(atr, float) else None,
            )
        )

    def _build_record(
        self,
        setup: _Setup,
        state: SetupState,
        reason: InvalidationReason | None,
        *,
        entry: Decimal | None = None,
        stop: Decimal | None = None,
        target: Decimal | None = None,
        risk: Decimal | None = None,
        atr: float | None = None,
    ) -> SetupRecord:
        gap = setup.fvg
        ts = self._recent[-1].ts if self._recent else setup.sweep_ts
        rr: Decimal | None = None
        if entry is not None and target is not None and risk is not None and risk > 0:
            rr = abs(target - entry) / risk
        return SetupRecord(
            timestamp=ts,
            direction=setup.direction,
            session=self._session_name(ts),
            liquidity_type=setup.source,
            liquidity_level=setup.level,
            sweep_price=setup.sweep_extreme,
            sweep_distance=setup.sweep_distance,
            signal_status=state,
            reached=setup.state,
            invalidation_reason=reason,
            reclaim_price=setup.reclaim_price,
            displacement_size=setup.displacement_size,
            average_body=setup.average_body,
            mss_level=setup.mss_level,
            fvg_high=None if gap is None else gap.high,
            fvg_low=None if gap is None else gap.low,
            fvg_size=None if gap is None else gap.size,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk_distance=risk,
            rr=rr,
            atr=None if atr is None else _dec(atr),
        )

    def _session_name(self, ts: datetime) -> str:
        for window in self._p.trading_windows:
            if window.contains(ts):
                return window.name
        if self._p.asian_session.contains(ts):
            return self._p.asian_session.name
        return "OUTSIDE"


# ------------------------------------------------------------------ helpers


def _one(level: Decimal | None) -> list[Decimal]:
    return [] if level is None else [level]


def _dec(value: float) -> Decimal:
    """A float indicator reading as a price-comparable Decimal.

    Fixed to six places rather than passed to `Decimal(float)`, whose exact
    binary expansion would put a different number in the log than the one the
    comparison used.
    """
    return Decimal(f"{value:.6f}")


def _validate(params: LiquiditySweepParams) -> None:
    if params.swing_lookback < 1:
        raise DomainError(f"swing lookback must be at least 1, got {params.swing_lookback}")
    if params.atr_period < 1:
        raise DomainError(f"ATR period must be at least 1, got {params.atr_period}")
    if params.displacement_period < 1:
        raise DomainError(
            f"displacement period must be at least 1, got {params.displacement_period}"
        )
    if params.max_setup_bars < 1:
        raise DomainError(f"max setup bars must be at least 1, got {params.max_setup_bars}")
    if params.rr_target <= 0:
        raise DomainError(f"RR target must be positive, got {params.rr_target}")
    if params.max_trades_per_day < 1:
        raise DomainError(
            f"max trades per day must be at least 1, got {params.max_trades_per_day}"
        )
    if not params.sources:
        raise DomainError("at least one liquidity source must be enabled")
    if not params.trading_windows:
        raise DomainError("at least one trading window must be configured")
    if params.max_hold <= timedelta(0):
        raise DomainError(f"max hold must be positive, got {params.max_hold}")
    if params.news_filter_enabled:
        raise DomainError(
            "news_filter_enabled is on but no historical economic calendar is wired "
            "into this strategy. Running with it on would reject every setup rather "
            "than filtering any - see section 21 of the brief."
        )
