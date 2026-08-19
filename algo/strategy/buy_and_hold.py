"""Buy and hold — the first falsification strategy. Brief §9 Milestone 3.

    "Buy-and-hold should track the instrument."

It buys once, on the first bar it is given enough history for, and then does
nothing. Any divergence between its equity curve and the instrument's own move is
either cost or an engine bug, and since the costs are known the difference is
attributable.
"""

from __future__ import annotations

from algo.core.enums import Side, SignalAction
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext


class BuyAndHold(Strategy):
    """Buy one position on the first bar and hold it to the end of the run."""

    strategy_id = "buy_and_hold"

    def __init__(self, instrument: InstrumentId, *, config_hash: str = "") -> None:
        self._instrument = instrument
        self._config_hash = config_hash
        self._entered = False

    def warmup_bars(self) -> int:
        return 0

    def params(self) -> dict[str, str]:
        return {"instrument": self._instrument.key}

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if self._entered:
            return []
        self._entered = True

        legs = (
            SignalLeg(
                instrument=self._instrument,
                direction=Side.BUY,
                entry=PriceIntent.market(),
            ),
        )
        return [
            Signal(
                signal_id=signal_id(
                    strategy_id=self.strategy_id,
                    params_hash=self.params_hash(),
                    bar_close_iso=iso(ctx.now),
                    action=SignalAction.OPEN.value,
                    leg_keys=(f"{self._instrument.key}:BUY",),
                    config_hash=self._config_hash,
                ),
                strategy_id=self.strategy_id,
                ts=ctx.now,
                action=SignalAction.OPEN,
                legs=legs,
                reason="buy-and-hold benchmark: entering on the first available bar",
                context={"close": str(ctx.bar.close), "bar_index": str(ctx.session.bar_index)},
            )
        ]
