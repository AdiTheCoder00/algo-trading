"""The live loop: real bars in, real orders out.

Brief §9 Milestone 6/7. Everything downstream of a decision already existed
before this module - `Router` (at-most-once, reconcile-before-send),
`KotakBroker`, `PaperBroker`, `OrderJournal`, `Reconciler` - and everything
upstream of one existed in `BacktestEngine`. What was missing was the loop
between them, and `algo live` stopped short of it deliberately until now.

## It does not decide anything itself

Every trading decision comes from `BacktestEngine.decide`, unchanged and shared
verbatim with the backtest. This module chooses *when* to ask and *what to do
with the answer*; it never chooses what to trade, how much, or when to exit. The
paper broker already states the principle for fills - "not a similar one, the
same `FillSimulator` object" - and it matters more for sizing and exits than it
does for fills.

So a live run and a backtest over the same bars cannot disagree about intent.
They can still disagree about *outcome*, and that difference is the whole point
of running this: the backtest assumes its orders fill, and the router can refuse.

## The four things that make it safe rather than merely working

**It settles before it decides.** Fills that happened since the last pass are
applied to the portfolio first, so the strategy is asked "given what you actually
hold" - never "given what you asked for last time". `is_flat` is the strategy's
first gate, and feeding it a stale answer would re-enter a position it already
has.

**It cannot send before the books agree.** `Router.place` refuses until a clean
reconciliation has run, including the first order after a restart - which is
exactly when local state is least trustworthy. This loop does not bypass that;
it reconciles on entry and lets the router enforce the rest.

**It processes each closed bar exactly once.** A poll that returns the same bar
twice must not act twice. Bars are keyed by timestamp and the loop tracks the
last one it acted on, because a duplicated entry signal is a doubled position.

**It stops.** `max_passes` and `until` bound every run. A trading loop with no
stopping condition is one that keeps trading after the thing that was watching it
has gone home.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from algo.backtest.engine import BacktestEngine, BarDecision
from algo.core.bar import Bar
from algo.core.clock import Clock
from algo.core.errors import AlgoError, DomainError
from algo.core.fill import Fill
from algo.core.order import Order
from algo.core.timeutil import iso, ist_date
from algo.execution.router import Outcome, RoutingResult
from algo.persistence.state import StateStore


@runtime_checkable
class BarFeed(Protocol):
    """Closed bars for the current session, newest last.

    Returns *closed* bars only. A feed that included the forming bar would hand
    the strategy a price that can still change, which is look-ahead by another
    name - the one form of it the backtest's firewall cannot catch, because in
    live there is no future array to withhold.
    """

    def closed_bars(self) -> Sequence[Bar]: ...


@runtime_checkable
class FillFeed(Protocol):
    """Fills the broker has reported since the last call."""

    def new_fills(self) -> Sequence[Fill]: ...


@dataclass(frozen=True, slots=True)
class PassResult:
    """What one pass of the loop did. Empty is the normal case."""

    ts: datetime
    bar_ts: datetime | None
    settled: tuple[Fill, ...] = field(default_factory=tuple)
    decision: BarDecision | None = None
    routed: tuple[RoutingResult, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def acted(self) -> bool:
        return bool(self.settled or self.routed)

    def summary(self) -> str:
        if self.note:
            return self.note
        parts = []
        if self.settled:
            parts.append(f"{len(self.settled)} fill(s) settled")
        if self.decision is not None and self.decision.exit_event is not None:
            parts.append(f"exit: {self.decision.exit_event.reason.value}")
        for result in self.routed:
            parts.append(f"{result.client_order_id} -> {result.outcome.value}")
        return "; ".join(parts) or "nothing to do"


class LiveLoop:
    """Drive `BacktestEngine.decide` from a live feed and route what it returns."""

    __slots__ = (
        "_bars",
        "_chain",
        "_clock",
        "_engine",
        "_fills",
        "_last_bar_ts",
        "_place",
        "_state",
    )

    def __init__(
        self,
        *,
        engine: BacktestEngine,
        bars: BarFeed,
        fills: FillFeed,
        place: Callable[[list[Order]], Sequence[RoutingResult]],
        clock: Clock,
        state: StateStore | None = None,
        chain: Callable[[Bar], object] | None = None,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._fills = fills
        self._place = place
        self._clock = clock
        self._state = state
        self._chain = chain
        self._last_bar_ts: datetime | None = None

    @property
    def last_bar_ts(self) -> datetime | None:
        """The bar this loop has already acted on. Survives for the process's
        life so a duplicated poll cannot re-enter a position."""
        return self._last_bar_ts

    def pass_once(self) -> PassResult:
        """Settle, then decide on the newest closed bar if it is new.

        One pass is deliberately the whole unit of work, and it returns rather
        than logging: it makes the loop testable without a clock, a socket or a
        sleep, and every safety property below is asserted against this.
        """
        now = self._clock.now()

        # ---- 1. settle first. The strategy must be asked about what is
        #         actually held, not about what it asked for last pass.
        settled = tuple(self._fills.new_fills())
        for fill in settled:
            self._engine.apply_fill(fill, session_day=ist_date(fill.ts))

        closed = self._bars.closed_bars()
        if not closed:
            return PassResult(ts=now, bar_ts=None, settled=settled, note="no closed bar yet")

        bar = closed[-1]
        if self._last_bar_ts is not None and bar.ts <= self._last_bar_ts:
            return PassResult(
                ts=now,
                bar_ts=bar.ts,
                settled=settled,
                note=f"bar {iso(bar.ts)} already acted on",
            )

        # ---- 2. refresh the chain, once, before anything asks it a question.
        #         A single `decide` asks several times - the strategy, the
        #         dashboard snapshot, then each mark - and those answers must all
        #         come from one poll or a strike can be chosen at one instant and
        #         marked at another.
        if self._chain is not None:
            try:
                self._chain(bar)
            except AlgoError as exc:
                # No chain means no delta, and no delta means the strategy would
                # silently find no strike. Saying so beats a quiet no-trade.
                return PassResult(
                    ts=now,
                    bar_ts=bar.ts,
                    settled=settled,
                    note=f"chain unavailable at {iso(bar.ts)}: {exc}",
                )

        # ---- 3. decide. Shared verbatim with the backtest.
        index = self._engine.append_bar(bar)
        decision = self._engine.decide(bar, index)
        self._last_bar_ts = bar.ts

        # ---- 4. route. The router may still refuse, and that is a result, not
        #         an error - it is the reconcile-before-send rule doing its job.
        routed = tuple(self._place(list(decision.orders))) if decision.orders else ()
        if self._state is not None:
            for result in routed:
                # Anything but PLACED is worth a note the dashboard shows:
                # BLOCKED_UNRECONCILED and UNCONFIRMED especially, since those
                # mean the intended position and the real one may now differ.
                if result.outcome is not Outcome.PLACED:
                    self._state.record_note(
                        bar.ts,
                        f"order {result.client_order_id} {result.outcome.value}: "
                        f"{result.detail}",
                    )
        return PassResult(
            ts=now, bar_ts=bar.ts, settled=settled, decision=decision, routed=routed
        )

    def run(
        self,
        *,
        max_passes: int,
        until: datetime | None = None,
        on_pass: Callable[[PassResult], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.0,
    ) -> list[PassResult]:
        """Pass repeatedly until bounded out.

        `max_passes` is required and has no default. A trading loop's stopping
        condition is not a detail to be left to a caller who forgot.
        """
        if max_passes < 1:
            raise DomainError(f"max_passes must be at least 1, got {max_passes}")

        results: list[PassResult] = []
        for pass_number in range(max_passes):
            if until is not None and self._clock.now() >= until:
                break
            result = self.pass_once()
            results.append(result)
            if on_pass is not None:
                on_pass(result)
            if sleep is not None and poll_interval_s > 0 and pass_number < max_passes - 1:
                sleep(poll_interval_s)
        return results
