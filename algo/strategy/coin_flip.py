"""Coin flip — the second falsification strategy. Brief §9 Milestone 3.

    "The coin-flip should lose approximately the spread x trade count. If it
     doesn't, the engine is wrong — fix it before continuing."

The strategy reads its position **from the context**, never from its own memory
of what it asked for. That distinction is not pedantry: the first version tracked
its own intent, and when the risk layer refused an order the strategy carried on
as though it had been filled — emitting closes for positions that did not exist
and quietly accumulating a three-lot position that then tripped a rounding error
deep in the cost basis. In live trading the same divergence arrives via rejections
and partial fills, and it would be far harder to spot.

So the rule, which every strategy from here on follows: **the portfolio is the
source of truth about what is held. A strategy's memory of its own intentions is
not.**

The behaviour is therefore: flat, so open; open, so close. One round trip every
two bars, seeded from the bar timestamp so a shorter run makes the same decisions
on the same dates.
"""

from __future__ import annotations

import random

from algo.core.enums import Side, SignalAction
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext


class CoinFlip(Strategy):
    """Open at random when flat; close when open. No edge, by construction."""

    strategy_id = "coin_flip"

    def __init__(self, instrument: InstrumentId, *, seed: int = 0, config_hash: str = "") -> None:
        self._instrument = instrument
        self._seed = seed
        self._config_hash = config_hash

    def warmup_bars(self) -> int:
        return 0

    def params(self) -> dict[str, str]:
        return {"instrument": self._instrument.key, "seed": str(self._seed)}

    def _flip(self, ctx: BarContext) -> Side:
        rng = random.Random(f"{self._seed}:{iso(ctx.now)}")
        return Side.BUY if rng.random() < 0.5 else Side.SELL

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        held = ctx.positions().get(self._instrument)

        if held is not None and not held.is_flat:
            closing = Side.SELL if held.qty > 0 else Side.BUY
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    closing,
                    f"coin flip: flattening a {held.side} position of {held.lots} lot(s)",
                )
            ]

        entering = self._flip(ctx)
        return [
            self._signal(
                ctx,
                SignalAction.OPEN,
                entering,
                f"coin flip: flat, seeded draw chose {entering}",
            )
        ]

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
                    instrument=self._instrument,
                    direction=side,
                    entry=PriceIntent.market(),
                ),
            ),
            reason=reason,
            context={"close": str(ctx.bar.close)},
        )
