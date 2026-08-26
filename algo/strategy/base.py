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
from collections.abc import Mapping

from algo.core.fill import Fill
from algo.core.ids import stable_hash
from algo.core.signal import Signal
from algo.strategy.context import BarContext


class Strategy(ABC):
    """Base class for every strategy, in backtest, paper and live alike."""

    strategy_id: str = "unnamed"

    def __init__(self) -> None:
        self._notes: list[str] = []

    def note(self, message: str) -> None:
        """Record why this bar produced no signal, or produced the one it did.

        Brief §8 requires a skipped trade to be logged rather than silently
        dropped. A strategy cannot log — it has no I/O — so it leaves a note
        and the engine collects it. Without this, "no strike was quoted at
        0.25 delta" and "the strategy chose not to trade" look identical in
        the output, and they are not remotely the same thing.
        """
        self._notes.append(message)

    def drain_notes(self) -> list[str]:
        """Take the notes recorded since the last call."""
        notes, self._notes = self._notes, []
        return notes

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

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, str]:
        """Strategy state that must survive a process restart.

        Almost nothing belongs here. Position state comes from the context on
        every bar (D-041) precisely so it *cannot* drift, and anything derivable
        from the context must keep being derived. What belongs here is state that
        is genuinely the strategy's own and is not reconstructible from the book
        - `DeltaStrangle`'s record of which expiry cycles it has already traded
        being the motivating case, since a flat account looks identical whether
        this cycle was traded and closed or never entered at all.

        Strings, like `params`, so the stored form does not depend on a float
        repr. Empty by default: a strategy with no such state persists nothing.
        """
        return {}

    def restore(self, state: Mapping[str, str]) -> None:  # noqa: B027 - optional hook
        """Reload what `state` saved. Must tolerate keys it does not recognise.

        The caller is responsible for checking that the state was saved under the
        same `params_hash` - restoring a cadence recorded under different
        parameters would let a config edit silently skip or repeat a cycle.
        """
