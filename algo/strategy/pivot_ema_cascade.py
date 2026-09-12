"""Fibonacci pivot break, then a full 10/20/50/100/200 EMA cascade, entering on the 200.

## The rule, stated once

Two things are on the chart: **Pivot Points Standard with the type set to
Fibonacci**, and the five EMAs 10, 20, 50, 100 and 200. On the 5-minute bars, a
short is armed when a candle closes down through *any* pivot line, and then
completes when price closes down through the 10, the 20, the 50, the 100 and
finally the 200 EMA **in that order**. The candle that closes below the 200 is
the entry. The exit is the mirror of the start of the cascade: the first candle
that closes back above *both* the 10 and the 20 EMA. A long is the same rule
with every comparison flipped.

"After, or on the same candle" is part of the rule, not a liberty taken with it:
on a fast 5-minute bar price can slice several EMAs at once, and requiring one
bar per step would silently discard exactly the moves the rule is trying to
describe. So each bar advances the cascade as far as that bar's close allows.

## What "crosses" means here, and why it is the close

Every step is tested on the **close**, never on the wick: `prev_close > level >=
close` going down. The rule's own last step says "jahan close ho wahan entry" -
where it closes, that is the entry - so the entry step is close-based by
construction, and using a wick for the earlier steps would mean the cascade is
half one definition and half another. A long shadow through the 50 EMA that
closes back above it is not price having lost the 50.

This is the same comparison shape `EmaBollinger` uses for its band re-entry
(`prev_close < prev_lower <= close`), for the same reason: a level is crossed by
a bar that ends on the other side of it, not by one that visits.

## Why the cascade carries state, and what resets it

The five EMAs are path-dependent and are stepped once per bar and persisted -
`ema_bb.py` argues this at length and nothing here differs. The **cascade** is
state of a second kind: it is a partially-matched pattern, not an indicator, and
it is the only thing in this strategy that a restart could not rebuild from the
bars.

It is reset by two things and only two:

*   **A new session.** The pivot lines are redrawn from the session that just
    finished, so a cascade armed against yesterday's S1 is armed against a line
    that no longer exists. Nothing intraday is carried across the boundary.
*   **Price closing back above the last level it crossed** (below, for a long).
    A cascade is a claim that price is leaving these levels behind; a close back
    through the most recent one falsifies that claim and the count starts again.

There is deliberately **no bar-count expiry**. A "the cascade must complete
within N bars" cap is a tunable with no prior, and D-131 is the standing entry
on what tuning a threshold against this instrument's history is worth. The two
resets above are structural - they follow from what the pattern claims - so they
need no number.

## The pivots come from a session the strategy watched end to end

`FibPivots` needs the previous session's high, low and close. Rather than
re-derive them from the window - which on M5 would need ~276 bars of history
just to see one session, and would break the moment a caller passed a shorter
one - the session's OHLC is accumulated bar by bar and rolled over when
`ctx.session.session_date` changes.

The **first** session a strategy instance sees is discarded, because a run that
starts mid-session has watched only part of it and its "high" is the high of
whatever fragment was supplied. Pivots therefore appear at the second rollover,
not the first. Reporting a level computed from half a session would be the kind
of number that looks right on the chart and is wrong by however much the missing
half moved.

## Warmup is a residue argument, as it is everywhere else here

`seed_shed_bars` generalises the arithmetic `ema_bb.py` did by hand for its 50
EMA: an `ema()` seeded with the first close carries `(1 - alpha)^n` of that seed
after `n` bars, and `warmup_bars` is where that residue falls to the same 5.8%
`indicators.warmup_bars()` accepts for the MACD. For a 200 EMA that is 285 bars,
not 200. Feeding it 200 and calling it warm would put the entry trigger - the
single most important line in the rule - about 13% of the way to being the seed
close rather than the average.

## Not registered in `strategy_for`, and no numbers claimed

`ema_bb.py` states the precedent (D-151): registering a strategy makes it
reachable by the live loop and the dashboard, and that should follow a
measurement rather than precede one.

`scripts/measure_pivot_ema_cascade_xauusd.py` is the measurement, and it needs
MetaTrader 5 - which means real XAUUSD bars from the user's own terminal. **It
has not been run.** Nothing in this module or its tests is evidence that this
rule makes money; they are evidence that the code implements the rule that was
described. The four strategies measured across D-151 to D-154 were all built
from published rules that sounded sound and none of them survived contact with
the data, so the honest reading of an unmeasured fifth is that it is unmeasured.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from algo.core.enums import Side, SignalAction
from algo.core.errors import DomainError
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.pricing.indicators import FIB_RATIOS, FibPivots, fib_pivots
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext
from algo.strategy.protective_exits import ExitKind, ProtectiveExits

#: The five EMAs the rule names, slowest last. The order is load-bearing: the
#: cascade walks this tuple in sequence and the final entry is its last element.
EMA_PERIODS = (10, 20, 50, 100, 200)

#: The residue `indicators.warmup_bars()` tolerates for the MACD, reused as the
#: definition of "this EMA has shed its seed". See the module docstring.
SEED_RESIDUE = 0.058


def seed_shed_bars(period: int) -> int:
    """Bars until an `ema(period)` seeded with the first close has shed it.

    `(1 - alpha)^n <= SEED_RESIDUE`, solved for `n`. Returns 72 for a 50-period
    EMA, which is the number `ema_bb.py` arrived at by hand for the same
    argument - this is that reasoning written down rather than a new one.
    """
    if period < 2:
        raise DomainError(f"EMA period must be at least 2, got {period}")
    alpha = 2.0 / (period + 1.0)
    return math.ceil(math.log(SEED_RESIDUE) / math.log(1.0 - alpha))


def _ema_step(close: float, previous: float | None, alpha: float) -> float:
    """One recursive EMA step, seeded with the first close as `ema()` is."""
    return close if previous is None else alpha * close + (1.0 - alpha) * previous


class _Cascade:
    """One direction's partially-matched pattern.

    `stage` counts levels crossed: 0 is idle, 1 is "a pivot line has gone", and
    `1 + len(periods)` means the 200 EMA went too and the entry fires. `last`
    is the level of the most recent step, which is what a re-cross is tested
    against.
    """

    __slots__ = ("last", "pivot_name", "stage")

    def __init__(self) -> None:
        self.stage = 0
        self.last = 0.0
        self.pivot_name = ""

    def reset(self) -> None:
        self.stage = 0
        self.last = 0.0
        self.pivot_name = ""

    def state(self, prefix: str) -> dict[str, str]:
        if self.stage == 0:
            return {}
        return {
            f"{prefix}_stage": str(self.stage),
            f"{prefix}_last": repr(self.last),
            f"{prefix}_pivot": self.pivot_name,
        }

    def restore(self, prefix: str, state: Mapping[str, str]) -> None:
        raw = state.get(f"{prefix}_stage", "").strip()
        if not raw:
            self.reset()
            return
        self.stage = int(raw)
        self.last = float(state.get(f"{prefix}_last", "0"))
        self.pivot_name = state.get(f"{prefix}_pivot", "")


class PivotEmaCascade(Strategy):
    """Fibonacci pivot break into a five-EMA cascade, entering on the 200 EMA."""

    strategy_id = "xauusd_pivot_ema_cascade_v1"

    def __init__(
        self,
        *,
        instrument: InstrumentId,
        ema_periods: Sequence[int] = EMA_PERIODS,
        stop_loss_pct: Decimal = Decimal("0.5"),
        trail_activation_pct: Decimal = Decimal("2"),
        trail_pct: Decimal = Decimal("0"),
        giveback_frac: Decimal = Decimal("0"),
        config_hash: str = "",
    ) -> None:
        super().__init__()
        periods = tuple(int(p) for p in ema_periods)
        if len(periods) < 3:
            # Two would leave the exit rule (the first two) and the entry
            # trigger (the last) naming the same lines, so the position would
            # be eligible to close on the bar it opened.
            raise DomainError(f"need at least three EMA periods, got {periods}")
        if any(p < 2 for p in periods):
            raise DomainError(f"every EMA period must be at least 2, got {periods}")
        if list(periods) != sorted(periods) or len(set(periods)) != len(periods):
            raise DomainError(f"EMA periods must be strictly increasing, got {periods}")

        self._instrument = instrument
        self._periods = periods
        self._config_hash = config_hash
        self._exits = ProtectiveExits(
            stop_loss_pct=stop_loss_pct,
            trail_activation_pct=trail_activation_pct,
            trail_pct=trail_pct,
            giveback_frac=giveback_frac,
        )

        self._emas: list[float | None] = [None] * len(periods)
        self._prev_close: float | None = None
        self._bars_seen = 0

        # The session being accumulated, and the pivots drawn from the one
        # before it. `_partial_session` marks a session the instance joined
        # part-way through - see the module docstring.
        self._session_day: date | None = None
        self._session_high = 0.0
        self._session_low = 0.0
        self._session_close = 0.0
        self._partial_session = True
        self._pivots: FibPivots | None = None

        self._short = _Cascade()
        self._long = _Cascade()

    # ----------------------------------------------------------------- contract
    def warmup_bars(self) -> int:
        """The slowest EMA's seed-shedding length, plus the bar it is compared to."""
        return seed_shed_bars(self._periods[-1]) + 1

    def params(self) -> dict[str, str]:
        return {
            "instrument": self._instrument.key,
            "ema_periods": ",".join(str(p) for p in self._periods),
            "pivots": "fibonacci",
            **self._exits.params(),
        }

    # -------------------------------------------------------------- persistence
    def state(self) -> dict[str, str]:
        """`repr()` on every float, as `ema_bb.py` does and for the same reason.

        The EMAs and the pivots are both resumed from these strings; a rounded
        value would restart the recursion from a different number than the one
        it stopped at, and would move a pivot line the cascade is counted
        against.
        """
        carried = dict(self._exits.state())
        carried["bars_seen"] = str(self._bars_seen)
        for period, value in zip(self._periods, self._emas, strict=True):
            if value is not None:
                carried[f"ema_{period}"] = repr(value)
        if self._prev_close is not None:
            carried["prev_close"] = repr(self._prev_close)
        if self._session_day is not None:
            carried["session_day"] = self._session_day.isoformat()
            carried["session_high"] = repr(self._session_high)
            carried["session_low"] = repr(self._session_low)
            carried["session_close"] = repr(self._session_close)
            carried["session_partial"] = str(self._partial_session)
        if self._pivots is not None:
            # `P` and the range are enough to rebuild every line; see `restore`.
            carried["pivot_p"] = repr(self._pivots.p)
            carried["pivot_span"] = repr(self._pivots.r3 - self._pivots.p)
        carried.update(self._short.state("short"))
        carried.update(self._long.state("long"))
        return carried

    def restore(self, state: Mapping[str, str]) -> None:
        self._exits.restore(state)
        self._bars_seen = int(state.get("bars_seen", "0"))
        self._emas = [
            float(state[f"ema_{p}"]) if f"ema_{p}" in state else None for p in self._periods
        ]
        raw_prev = state.get("prev_close", "").strip()
        self._prev_close = float(raw_prev) if raw_prev else None
        raw_day = state.get("session_day", "").strip()
        if raw_day:
            self._session_day = date.fromisoformat(raw_day)
            self._session_high = float(state["session_high"])
            self._session_low = float(state["session_low"])
            self._session_close = float(state["session_close"])
            self._partial_session = state.get("session_partial", "True") == "True"
        raw_pivot = state.get("pivot_p", "").strip()
        if raw_pivot:
            # Rebuilt from `P` and the span rather than from H/L/C: those three
            # are not recoverable from the levels (three unknowns, and `P` fixes
            # only their sum), but every level is a function of exactly these
            # two numbers, so this restores each line to the value it had.
            pivot, span = float(raw_pivot), float(state["pivot_span"])
            r1, r2, r3 = (pivot + ratio * span for ratio in FIB_RATIOS)
            s1, s2, s3 = (pivot - ratio * span for ratio in FIB_RATIOS)
            self._pivots = FibPivots(p=pivot, r1=r1, r2=r2, r3=r3, s1=s1, s2=s2, s3=s3)
        self._short.restore("short", state)
        self._long.restore("long", state)

    # --------------------------------------------------------------------- logic
    def on_bar(self, ctx: BarContext) -> list[Signal]:
        close = float(ctx.bar.close)
        prev_close = self._prev_close

        # Stepped first and unconditionally, before the exit check and before
        # the warmup gate: a bar on which a stop fires is still a bar the EMAs
        # saw. `ema_bb.py` records why the MACD port's opposite behaviour is a
        # wart rather than a convention.
        for i, period in enumerate(self._periods):
            self._emas[i] = _ema_step(close, self._emas[i], 2.0 / (period + 1.0))
        self._bars_seen += 1
        self._roll_session(ctx, close)
        self._prev_close = close

        held = ctx.positions().get(self._instrument)
        exit_decision = self._exits.check(ctx.bar, held)
        if exit_decision is not None:
            self._short.reset()
            self._long.reset()
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    exit_decision.side,
                    exit_decision.reason,
                    exit_kind=exit_decision.kind,
                )
            ]

        if self._bars_seen < self.warmup_bars() or prev_close is None:
            self.note(
                f"no entry: {self._bars_seen} bar(s) seen, need {self.warmup_bars()} "
                f"for a {self._periods[-1]}-period EMA to have shed its seed"
            )
            return []

        emas = [value for value in self._emas if value is not None]
        if len(emas) != len(self._periods):
            return []

        if held is not None and not held.is_flat:
            # No cascade is counted while a position is held: the next entry is
            # a fresh pattern, not the tail of the one that is already on.
            self._short.reset()
            self._long.reset()
            return self._maybe_exit(ctx, held, close, prev_close, emas)

        if self._pivots is None:
            self.note("no entry: no complete session yet, so no pivot lines to break")
            return []

        # Short first, arbitrarily but consistently. The two cascades cannot
        # both complete on one bar - one needs a close below every EMA and the
        # other a close above every EMA - so the order decides nothing.
        signal = self._advance(ctx, close, prev_close, emas, short=True)
        return signal or self._advance(ctx, close, prev_close, emas, short=False) or []

    # ------------------------------------------------------------------ sessions
    def _roll_session(self, ctx: BarContext, close: float) -> None:
        """Accumulate this session's OHLC, and redraw the pivots when it turns over."""
        day = ctx.session.session_date
        if self._session_day is None:
            self._session_day = day
            self._session_high = float(ctx.bar.high)
            self._session_low = float(ctx.bar.low)
            self._session_close = close
            # The instance joined this session at whatever bar it was handed,
            # which is not necessarily its first.
            self._partial_session = True
            return

        if day == self._session_day:
            self._session_high = max(self._session_high, float(ctx.bar.high))
            self._session_low = min(self._session_low, float(ctx.bar.low))
            self._session_close = close
            return

        if self._partial_session:
            self.note(
                f"session {self._session_day} was joined part-way through, so no "
                "pivots are drawn from it"
            )
        else:
            self._pivots = fib_pivots(
                high=self._session_high, low=self._session_low, close=self._session_close
            )
        # A new session redraws the lines, so every partially-matched cascade
        # is counted against lines that no longer exist.
        self._short.reset()
        self._long.reset()
        self._session_day = day
        self._session_high = float(ctx.bar.high)
        self._session_low = float(ctx.bar.low)
        self._session_close = close
        self._partial_session = False

    # ------------------------------------------------------------------ cascade
    def _advance(
        self,
        ctx: BarContext,
        close: float,
        prev_close: float,
        emas: list[float],
        *,
        short: bool,
    ) -> list[Signal] | None:
        """Walk one direction's cascade as far as this bar's close allows."""
        cascade = self._short if short else self._long
        pivots = self._pivots
        if pivots is None:  # pragma: no cover - the caller has already checked
            return None

        def crossed(level: float) -> bool:
            """Closed through `level` on this bar, in this cascade's direction."""
            if short:
                return prev_close > level >= close
            return prev_close < level <= close

        if cascade.stage > 0:
            # A close back through the last level crossed falsifies the claim
            # the cascade is making, so the count starts again - possibly from
            # a pivot break on this very bar, which the loop below allows.
            retraced = close > cascade.last if short else close < cascade.last
            if retraced:
                cascade.reset()

        if cascade.stage == 0:
            for level in pivots.levels():
                if crossed(level):
                    cascade.stage = 1
                    cascade.last = level
                    cascade.pivot_name = pivots.nearest(level)[0]
                    break

        # "After, or on the same candle": keep stepping while this one bar's
        # close is beyond the next EMA in the sequence.
        while 0 < cascade.stage <= len(self._periods):
            level = emas[cascade.stage - 1]
            if not crossed(level):
                return None
            cascade.stage += 1
            cascade.last = level

        if cascade.stage != len(self._periods) + 1:
            return None

        side = Side.SELL if short else Side.BUY
        direction = "below" if short else "above"
        pivot_name = cascade.pivot_name
        cascade.reset()
        return [
            self._signal(
                ctx,
                SignalAction.OPEN,
                side,
                f"pivot/EMA cascade: close {close:.2f} {direction} the "
                f"{self._periods[-1]} EMA {emas[-1]:.2f}, completing a cascade that "
                f"began {direction} the {pivot_name} pivot and went through "
                + ", ".join(f"{p} EMA" for p in self._periods[:-1]),
            )
        ]

    def _maybe_exit(
        self,
        ctx: BarContext,
        held: object,
        close: float,
        prev_close: float,
        emas: list[float],
        *,
        fast: int = 2,
    ) -> list[Signal]:
        """The mirror of the cascade's first two EMAs: close back through both.

        Tested as "closed beyond both" rather than "crossed both on this bar".
        The entry required a close below (above) every EMA including these two,
        so the first bar that ends on the other side of both *is* the crossing -
        and a `prev_close` test would additionally require that both were
        regained on one bar, which would hold a position through a two-bar
        recovery the rule says to leave.
        """
        long = held.qty > 0  # type: ignore[attr-defined]
        fast_emas = emas[:fast]
        if long:
            done = all(close < value for value in fast_emas)
        else:
            done = all(close > value for value in fast_emas)
        if not done:
            return []
        where = "below" if long else "above"
        closing_side = Side.SELL if long else Side.BUY
        levels = ", ".join(
            f"{p} EMA {v:.2f}" for p, v in zip(self._periods[:fast], fast_emas, strict=True)
        )
        return [
            self._signal(
                ctx,
                SignalAction.CLOSE,
                closing_side,
                f"pivot/EMA cascade: close {close:.2f} back {where} the {levels}, "
                f"flattening a {held.side} position of {held.lots} lot(s)",  # type: ignore[attr-defined]
            )
        ]

    # ------------------------------------------------------------------ helpers
    def _signal(
        self,
        ctx: BarContext,
        action: SignalAction,
        side: Side,
        reason: str,
        *,
        exit_kind: ExitKind | None = None,
    ) -> Signal:
        leg_key = f"{self._instrument.key}:{side}"
        context = {"close": str(ctx.bar.close)}
        for period, value in zip(self._periods, self._emas, strict=True):
            if value is not None:
                context[f"ema_{period}"] = f"{value:.4f}"
        if self._pivots is not None:
            context["pivot"] = f"{self._pivots.p:.4f}"
        if exit_kind is not None:
            context["exit"] = exit_kind
        return Signal(
            signal_id=signal_id(
                strategy_id=self.strategy_id,
                params_hash=self.params_hash(),
                bar_close_iso=iso(ctx.now),
                action=action.value,
                leg_keys=(leg_key,),
                config_hash=self._config_hash,
            ),
            strategy_id=self.strategy_id,
            ts=ctx.now,
            action=action,
            legs=(
                SignalLeg(instrument=self._instrument, direction=side, entry=PriceIntent.market()),
            ),
            reason=reason,
            context=context,
        )
