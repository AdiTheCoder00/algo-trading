"""Donchian-channel breakout — the well-defined form of "trend line breakout".

A hand-drawn trend line is not implementable without a human's judgement call
about which two swing points to connect, and this project does not invent
indicators without a stated, checkable definition (the same reason
`algo/pricing/indicators.py` mirrors an existing tool exactly rather than
picking its own EMA convention). The Donchian channel - the highest high and
lowest low of the last `lookback` bars - **is** that definition, formalised: it
is what "price broke its recent trend line" means once you have to write it
down. It is also the literal core of the Turtle Trading system, so "trend line
breakout" has a long, checkable precedent in this exact shape.

## No incremental state for the channel itself

An EMA is path-dependent - its value depends on every bar it has ever seen, so
`MacdCrossover` has to carry running state and persist it across a restart. A
rolling max/min over the last `lookback` bars depends on **only** those bars,
recomputed identically from `ctx.history(...)` on every call. There is nothing
to seed and nothing to lose on a restart for the channel itself - a strategy
instance and a freshly constructed one facing the same recent history make the
same decision. (The optional trailing stop below is path-dependent in its own
right and does carry state when enabled - see that section.)

## The channel excludes the bar being tested

`ctx.history(lookback + 1)` returns `lookback + 1` bars ending at, and
including, the current one. The channel is built from the **prior** `lookback`
bars only - today's own high can never be part of the ceiling today's close is
compared against, or a "breakout" would be checking today's price against a
range that already contains it, which cannot be broken by definition.

## Position management mirrors `MacdCrossover`

Same reasoning: a breakout is its own exit signal for the opposite side. A long
taken on an upside breakout is closed the moment price makes a fresh
`lookback`-bar low - the same event that would open a short if the strategy
were flat.

## A percentage stop-loss, checked before anything else

Added after the fact, the same way and for the same reason as
`MacdCrossover`'s (D-123's gap statement applied here too, until now):
`stop_loss_pct` (default 0.5) is checked first, every bar a position is held,
before even the warmup gate - a held position must never go unprotected
because the channel has not been recomputed yet. Uses the bar's actual
low/high rather than its close, via the shared `algo/strategy/price_stop.py` -
see that module for why a close-only check would understate a real stop's own
frequency.

## An optional trailing profit stop, layered on top

`trail_pct` (default 0, meaning off) adds a second, independent exit -
`algo/strategy/trailing_profit_stop.py`, the same module `MacdCrossover` uses.
Once a held position has moved `trail_activation_pct` (default 2%) in its
favour, a trailing stop arms `trail_pct` behind the best price seen since
entry. It protects nothing before that threshold - a profit-lock, not a
loss-bound - and is checked *after* the flat stop each bar. Once armed, its
level can never sit worse than entry - "cost to cost", see the trail module's
own docstring - so an armed trail's worst outcome is a scratch, never a
loser. This is the one piece of this strategy that is path-dependent:
enabling it (`trail_pct > 0`) means `state`/`restore` now persist the running
peak too, so a restart does not silently drop a trail that was already armed
mid-trade.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from algo.core.enums import Side, SignalAction
from algo.core.errors import DomainError
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext
from algo.strategy.price_stop import stop_touched
from algo.strategy.trailing_profit_stop import (
    TrailState,
    advance_trail,
    start_trail,
    trail_touched,
)


class TrendlineBreakout(Strategy):
    """Long on a fresh `lookback`-bar high, short on a fresh `lookback`-bar low."""

    strategy_id = "xauusd_trendline_breakout_v1"

    def __init__(
        self,
        *,
        instrument: InstrumentId,
        lookback: int = 20,
        stop_loss_pct: Decimal = Decimal("0.5"),
        trail_activation_pct: Decimal = Decimal("2"),
        trail_pct: Decimal = Decimal("0"),
        config_hash: str = "",
    ) -> None:
        super().__init__()
        if lookback < 2:
            raise DomainError(f"lookback must be at least 2, got {lookback}")
        if stop_loss_pct < 0:
            raise DomainError(f"stop_loss_pct cannot be negative, got {stop_loss_pct}")
        if trail_activation_pct < 0:
            raise DomainError(
                f"trail_activation_pct cannot be negative, got {trail_activation_pct}"
            )
        if trail_pct < 0:
            raise DomainError(f"trail_pct cannot be negative, got {trail_pct}")
        self._instrument = instrument
        self._lookback = lookback
        self._stop_loss_pct = stop_loss_pct
        self._trail_activation_pct = trail_activation_pct
        self._trail_pct = trail_pct
        self._config_hash = config_hash
        self._trail: TrailState | None = None

    def warmup_bars(self) -> int:
        return self._lookback + 1

    def params(self) -> dict[str, str]:
        return {
            "instrument": self._instrument.key,
            "lookback": str(self._lookback),
            "stop_loss_pct": str(self._stop_loss_pct),
            "trail_activation_pct": str(self._trail_activation_pct),
            "trail_pct": str(self._trail_pct),
        }

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, str]:
        """Only the trailing stop's running peak, when one is enabled and
        armed - the channel itself needs nothing (see the module docstring)."""
        if self._trail is None:
            return {}
        return {
            "trail_side": self._trail.side.value,
            "trail_entry": str(self._trail.entry_price),
            "trail_peak": str(self._trail.peak),
        }

    def restore(self, state: Mapping[str, str]) -> None:
        raw_trail_side = state.get("trail_side", "").strip()
        if not raw_trail_side:
            return
        try:
            self._trail = TrailState(
                side=Side(raw_trail_side),
                entry_price=Decimal(state["trail_entry"]),
                peak=Decimal(state["trail_peak"]),
            )
        except (KeyError, ValueError) as exc:
            raise DomainError(
                f"cannot restore trailing-stop state from {dict(state)!r}: {exc}. "
                "Refusing to run with a partially-restored trail."
            ) from exc

    # ------------------------------------------------------------------ logic
    def on_bar(self, ctx: BarContext) -> list[Signal]:
        # Checked before the warmup gate - a held position must never go
        # unprotected because the channel has not been recomputed yet.
        held = ctx.positions().get(self._instrument)
        if held is not None and not held.is_flat:
            side = Side.BUY if held.qty > 0 else Side.SELL
            if self._trail is None or self._trail.side is not side:
                self._trail = start_trail(held.average_price, side)
            self._trail = advance_trail(self._trail, ctx.bar)
        else:
            self._trail = None

        if held is not None and not held.is_flat and stop_touched(
            ctx.bar, held, self._stop_loss_pct
        ):
            closing_side = Side.SELL if held.qty > 0 else Side.BUY
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    closing_side,
                    f"stop loss: {self._stop_loss_pct}% move against a "
                    f"{held.side} position of {held.lots} lot(s), entry "
                    f"{held.average_price:.2f}",
                )
            ]

        if (
            held is not None
            and not held.is_flat
            and self._trail is not None
            and trail_touched(self._trail, ctx.bar, self._trail_activation_pct, self._trail_pct)
        ):
            closing_side = Side.SELL if held.qty > 0 else Side.BUY
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    closing_side,
                    f"trailing stop: {self._trail_pct}% pullback from a peak of "
                    f"{self._trail.peak:.2f} (armed at {self._trail_activation_pct}% "
                    f"profit) on a {held.side} position of {held.lots} lot(s), "
                    f"entry {held.average_price:.2f}",
                )
            ]

        if not ctx.has_history(self.warmup_bars()):
            self.note(
                f"no entry: {len(ctx.bars)} bar(s) seen, need {self.warmup_bars()} "
                f"for a {self._lookback}-bar channel"
            )
            return []

        window = ctx.history(self.warmup_bars())
        # Exclude the current bar (the last element) so the channel is built
        # from strictly prior bars - see the module docstring.
        channel_high = float(window.highs()[:-1].max())
        channel_low = float(window.lows()[:-1].min())
        close = float(ctx.bar.close)

        broke_up = close > channel_high
        broke_down = close < channel_low

        if held is not None and not held.is_flat:
            wants_close = (held.qty > 0 and broke_down) or (held.qty < 0 and broke_up)
            if not wants_close:
                return []
            closing_side = Side.SELL if held.qty > 0 else Side.BUY
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    closing_side,
                    f"trendline breakout: fresh {self._lookback}-bar "
                    f"{'low' if closing_side is Side.BUY else 'high'}, flattening a "
                    f"{held.side} position of {held.lots} lot(s) "
                    f"(close {close:.2f} vs channel [{channel_low:.2f}, {channel_high:.2f}])",
                )
            ]

        if broke_up:
            return [
                self._signal(
                    ctx, SignalAction.OPEN, Side.BUY,
                    f"trendline breakout: close {close:.2f} above the "
                    f"{self._lookback}-bar high {channel_high:.2f}",
                )
            ]
        if broke_down:
            return [
                self._signal(
                    ctx, SignalAction.OPEN, Side.SELL,
                    f"trendline breakout: close {close:.2f} below the "
                    f"{self._lookback}-bar low {channel_low:.2f}",
                )
            ]
        return []

    def _signal(
        self, ctx: BarContext, action: SignalAction, side: Side, reason: str
    ) -> Signal:
        leg_key = f"{self._instrument.key}:{side}"
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
                SignalLeg(
                    instrument=self._instrument, direction=side, entry=PriceIntent.market()
                ),
            ),
            reason=reason,
            context={"close": str(ctx.bar.close), "lookback": str(self._lookback)},
        )
