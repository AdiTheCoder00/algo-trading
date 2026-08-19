"""Coin flip — the second falsification strategy. Brief §9 Milestone 3.

    "The coin-flip should lose approximately the spread x trade count. If it
     doesn't, the engine is wrong — fix it before continuing."

The design is chosen so the expected loss is computable by hand rather than
merely plausible: on every bar it closes whatever is open and opens a fresh
position in a seeded random direction. That is exactly one round trip per bar, so
the predicted cost is `bars x round-trip cost` with no estimation involved.

Run against a flat price series, gross P&L is exactly zero and the net must equal
the predicted cost to the paisa. Run against a random walk, gross P&L is a
zero-drift random variable and the net should sit near it. The first is a proof;
the second is a sanity check.

Seeded from the bar timestamp rather than a running counter, so a shorter run
produces the same decisions on the same dates. A counter would make the strategy
depend on where the backtest window started — an easy thing to miss and an
impossible one to debug later.
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
    """Flat every bar, then long or short at random. One round trip per bar."""

    strategy_id = "coin_flip"

    def __init__(self, instrument: InstrumentId, *, seed: int = 0, config_hash: str = "") -> None:
        self._instrument = instrument
        self._seed = seed
        self._config_hash = config_hash
        self._side: Side | None = None

    def warmup_bars(self) -> int:
        return 0

    def params(self) -> dict[str, str]:
        return {"instrument": self._instrument.key, "seed": str(self._seed)}

    def _flip(self, ctx: BarContext) -> Side:
        rng = random.Random(f"{self._seed}:{iso(ctx.now)}")
        return Side.BUY if rng.random() < 0.5 else Side.SELL

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        signals: list[Signal] = []

        if self._side is not None:
            closing = self._side.opposite
            signals.append(
                self._signal(ctx, SignalAction.CLOSE, closing, "coin flip: flattening")
            )

        entering = self._flip(ctx)
        signals.append(
            self._signal(
                ctx,
                SignalAction.OPEN,
                entering,
                f"coin flip: seeded draw chose {entering}",
            )
        )
        self._side = entering
        return signals

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
