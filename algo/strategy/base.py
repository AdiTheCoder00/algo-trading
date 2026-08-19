"""The `Strategy` contract. Brief §5.

A strategy emits intent and nothing else. It cannot see lots, cash, margin or the
broker, and it cannot place an order — those live behind types it has no reference
to. "A strategy that computes lot size is a bug", so the type system is arranged
so that it cannot.

`params_hash` exists so a signal's identity depends on the parameters that
produced it (see `core.ids`). Change a parameter and the same bar produces a
different signal id, which is what stops a replay after a config edit from
matching an order placed under the old settings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from algo.core.fill import Fill
from algo.core.ids import stable_hash
from algo.core.signal import Signal
from algo.strategy.context import BarContext


class Strategy(ABC):
    """Base class for every strategy, in backtest, paper and live alike."""

    strategy_id: str = "unnamed"

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> list[Signal]:
        """Called once per closed bar. Returns zero or more intents."""

    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars of history required before `on_bar` should be trusted."""

    def params(self) -> dict[str, str]:
        """Parameters that define this strategy's behaviour, as strings.

        Strings rather than floats so the hash is stable across platforms — a
        float repr that differs in the last digit would silently change every
        signal id.
        """
        return {}

    def params_hash(self) -> str:
        return stable_hash(self.params())

    # ------------------------------------------------------- optional hooks
    # Proposed additions to §5 (D-008 / Q7b), all no-ops by default so an
    # existing strategy is unaffected.

    def on_fill(self, fill: Fill) -> None:  # noqa: B027 - optional hook, no-op by design
        """Informational. A strategy may record fills; it may not act on them here."""

    def on_session_start(self, ctx: BarContext) -> None:  # noqa: B027 - optional hook
        """Called before the first bar of a session."""

    def on_session_end(self, ctx: BarContext) -> list[Signal]:
        """Called after the last bar of a session. Where square-off intent belongs."""
        return []
