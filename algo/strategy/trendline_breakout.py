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

## No incremental state, unlike `MacdCrossover`

An EMA is path-dependent - its value depends on every bar it has ever seen, so
`MacdCrossover` has to carry running state and persist it across a restart. A
rolling max/min over the last `lookback` bars depends on **only** those bars,
recomputed identically from `ctx.history(...)` on every call. There is nothing
to seed and nothing to lose on a restart - a strategy instance and a freshly
constructed one facing the same recent history make the same decision. So
`state`/`restore` are left at the base class's no-op default.

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
were flat. No independent stop-loss is added, for the same reason
`MacdCrossover` has none: inventing one would make this a strategy nobody asked
for. See D-123's gap statement - it applies here too.
"""

from __future__ import annotations

from algo.core.enums import Side, SignalAction
from algo.core.errors import DomainError
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext


class TrendlineBreakout(Strategy):
    """Long on a fresh `lookback`-bar high, short on a fresh `lookback`-bar low."""

    strategy_id = "xauusd_trendline_breakout_v1"

    def __init__(
        self, *, instrument: InstrumentId, lookback: int = 20, config_hash: str = ""
    ) -> None:
        super().__init__()
        if lookback < 2:
            raise DomainError(f"lookback must be at least 2, got {lookback}")
        self._instrument = instrument
        self._lookback = lookback
        self._config_hash = config_hash

    def warmup_bars(self) -> int:
        return self._lookback + 1

    def params(self) -> dict[str, str]:
        return {"instrument": self._instrument.key, "lookback": str(self._lookback)}

    def on_bar(self, ctx: BarContext) -> list[Signal]:
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

        held = ctx.positions().get(self._instrument)
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
