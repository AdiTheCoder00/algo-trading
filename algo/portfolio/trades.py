"""Assembling fills into round trips.

Until now the engine produced fills and an equity curve, and `core.trade.Trade`
sat fully defined and entirely unused — which meant the trade log was always
empty and every metric in brief §10 that needs a round trip was missing: profit
factor, win rate, expectancy in R, average win and loss in R, the longest losing
streak, the R-multiple distribution. This is the piece that was absent.

**What counts as one trade.** A trade opens when the book goes from flat to
holding something and closes when it returns to flat. For a strangle that is both
legs together, which is the only grouping that means anything — a call leg and a
put leg reported as two separate trades would show one winner and one loser on a
position that was always a single bet.

**Where the P&L comes from.** The portfolio's own realised figure, differenced
across the life of the trade, rather than recomputed here from leg prices. There
is already exactly one piece of code that knows how a round trip closes out
(`Position.apply`), and a second implementation would eventually disagree with it.

**What R is.** The configured stop, in rupees, frozen at entry — assumption 7.4,
not the maximum possible loss. A short strangle's maximum loss is unbounded, so an
R-multiple measured against it would be meaningless. Where no stop was set, the
R-multiple is `None` rather than a number computed against a denominator nobody
chose.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.core.fill import Charges, Fill
from algo.core.trade import Trade, TradeLeg


class TradeBuilder:
    """Accumulates fills into one open round trip, then seals it.

    Driven by the engine, which already knows when the book crosses flat. Keeping
    that knowledge in one place means the trade log and the `round_trips` count
    can never disagree about how many trades happened.
    """

    __slots__ = (
        "_charges",
        "_context",
        "_legs",
        "_open",
        "_opened_at",
        "_realised_at_open",
        "_reason",
        "_risk_r",
        "_sequence",
        "_signal_id",
        "_strategy_id",
    )

    def __init__(self, strategy_id: str) -> None:
        self._strategy_id = strategy_id
        self._open = False
        self._legs: dict[str, TradeLeg] = {}
        self._charges = Charges()
        self._signal_id = ""
        self._reason = ""
        self._context: Mapping[str, str] = {}
        self._risk_r: Decimal | None = None
        self._opened_at: datetime | None = None
        self._realised_at_open = Decimal("0")
        self._sequence = 0

    @property
    def is_open(self) -> bool:
        return self._open

    def set_risk(self, risk_r: Decimal | None) -> None:
        """Record R once the entry fills have resolved the stop level.

        Separate from `open()` because the stop is a percentage of the margin
        blocked, and the margin is not known until the entry has actually filled.
        """
        self._risk_r = risk_r

    def open(
        self,
        *,
        signal_id: str,
        reason: str,
        context: Mapping[str, str],
        risk_r: Decimal | None,
        at: datetime,
        realised_so_far: Decimal,
    ) -> None:
        """Begin a trade. `risk_r` is the stop level in rupees, or None."""
        if self._open:
            raise DomainError(
                "a trade is already open; the book must return to flat before "
                "another begins, or two positions would be reported as one"
            )
        self._open = True
        self._legs = {}
        self._charges = Charges()
        self._signal_id = signal_id
        self._reason = reason or "no reason recorded"
        self._context = dict(context)
        self._risk_r = risk_r
        self._opened_at = at
        self._realised_at_open = realised_so_far

    def add(self, fill: Fill) -> None:
        """Fold a fill into the open trade."""
        if not self._open:
            raise DomainError(f"fill {fill.fill_id} arrived with no trade open")
        key = fill.instrument.key
        self._charges = self._charges + fill.charges

        existing = self._legs.get(key)
        if existing is None:
            self._legs[key] = TradeLeg(
                instrument=fill.instrument,
                side=fill.side,
                lots=abs(fill.lots),
                entry_price=fill.price,
                entry_ts=fill.ts,
                charges=fill.charges,
            )
            return

        # A second fill on the same instrument closes it. Recording the exit here
        # rather than inferring it later keeps the leg readable in the trade log:
        # sold at X, bought back at Y.
        self._legs[key] = existing.model_copy(
            update={
                "exit_price": fill.price,
                "exit_ts": fill.ts,
                "charges": existing.charges + fill.charges,
            }
        )

    def close(
        self, *, at: datetime, realised_now: Decimal, exit_reason: str
    ) -> Trade:
        """Seal the trade and return it."""
        if not self._open or self._opened_at is None:
            raise DomainError("no trade is open to close")

        gross = realised_now - self._realised_at_open
        net = gross - self._charges.total
        self._sequence += 1

        trade = Trade(
            trade_id=f"{self._strategy_id}.{self._signal_id}.{self._sequence}",
            strategy_id=self._strategy_id,
            signal_id=self._signal_id,
            legs=tuple(self._legs[key] for key in sorted(self._legs)),
            opened_at=self._opened_at,
            closed_at=at,
            gross_pnl=gross,
            charges=self._charges,
            r_multiple=(
                net / self._risk_r if self._risk_r and self._risk_r > 0 else None
            ),
            exit_reason=exit_reason,
            reason=self._reason,
            context=self._context,
        )
        self._open = False
        self._legs = {}
        self._charges = Charges()
        return trade

    def abandon(self) -> None:
        """Discard an unfinished trade at the end of a run.

        A position still open when the data ends is not a completed round trip,
        and counting it as one would put an unrealised figure into a realised
        statistic. It is dropped, and the run reports the open position separately.
        """
        self._open = False
        self._legs = {}
        self._charges = Charges()


def summarise(trades: list[Trade]) -> str:
    """One line per trade, for a quick eyeball of a run."""
    if not trades:
        return "no completed round trips"
    lines = []
    for trade in trades:
        legs = ", ".join(
            f"{'sold' if leg.side is Side.SELL else 'bought'} {leg.instrument.key}"
            for leg in trade.legs
        )
        r = f"{trade.r_multiple:+.2f}R" if trade.r_multiple is not None else "-"
        lines.append(
            f"{trade.opened_at:%Y-%m-%d %H:%M} .. "
            f"{trade.closed_at:%H:%M}  net {trade.net_pnl:>10,}  {r:>8}  "
            f"{trade.exit_reason:<18} {legs}"
        )
    return "\n".join(lines)
