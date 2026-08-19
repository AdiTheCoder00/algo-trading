"""`BarContext` — everything a strategy is allowed to see, and nothing else.

This is the look-ahead firewall (brief §7.1, decision D-006). The engine builds a
context per bar from a **copy** of history `[0..i]`. There is no `dataframe`, no
`feed`, no `broker`, no `clock`, and no accessor that returns bar `i+1` — because
bar `i+1` was never copied in.

`__slots__` is not a micro-optimisation here. It means the engine cannot
accidentally attach a `_full_history` attribute later and hand a strategy the keys
to the future; such an assignment raises at the point it is written.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import suppress
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.chain import OptionChainSnapshot
from algo.core.enums import Exchange
from algo.core.errors import CalendarError, DataError, DomainError
from algo.core.instrument import InstrumentId, InstrumentSpec
from algo.core.position import Position
from algo.core.timeutil import ist_date, minutes_between
from algo.exchange.calendar import MarketCalendar
from algo.exchange.expiries import ExpiryCalendar, ExpirySet
from algo.exchange.specs import ContractSpecStore


class SessionInfo(BaseModel):
    """Where in the trading day this bar sits.

    `is_us_dst` is surfaced because it changes the session length, and therefore
    the number of bars in the day — a strategy that counts bars would otherwise be
    quietly wrong for eight months of the year (D-017).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    is_us_dst: bool
    minutes_to_close: int
    is_partial_bar: bool
    bar_index: int
    bars_in_session: int

    @property
    def is_first_bar(self) -> bool:
        return self.bar_index == 0

    @property
    def is_last_bar(self) -> bool:
        return self.bar_index == self.bars_in_session - 1


class PositionView:
    """Read-only view of the positions belonging to one strategy."""

    __slots__ = ("_positions",)

    def __init__(self, positions: dict[str, Position]) -> None:
        self._positions = dict(positions)

    def get(self, instrument: InstrumentId) -> Position | None:
        return self._positions.get(instrument.key)

    def open_positions(self) -> tuple[Position, ...]:
        return tuple(p for _, p in sorted(self._positions.items()) if not p.is_flat)

    @property
    def is_flat(self) -> bool:
        return not self.open_positions()

    def __len__(self) -> int:
        return len(self.open_positions())


@runtime_checkable
class ChainProvider(Protocol):
    """Supplies the option chain as of a given instant. Wired in at Milestone 4."""

    def chain_at(
        self, underlying: str, option_expiry: date, ts: datetime
    ) -> OptionChainSnapshot | None: ...


class BarContext:
    """The strategy's entire view of the world at bar `i`."""

    __slots__ = (
        "_chain_provider",
        "_exchange",
        "_expiries",
        "_positions",
        "_session",
        "_specs",
        "_timeframe",
        "_window",
    )

    def __init__(
        self,
        *,
        window: BarWindow,
        session: SessionInfo,
        specs: ContractSpecStore,
        positions: PositionView,
        timeframe: Timeframe,
        exchange: Exchange = Exchange.MCX,
        chain_provider: ChainProvider | None = None,
        expiries: ExpiryCalendar | None = None,
    ) -> None:
        if len(window) == 0:
            raise DomainError("BarContext requires at least one closed bar")
        self._window = window
        self._session = session
        self._specs = specs
        self._positions = positions
        self._timeframe = timeframe
        self._exchange = exchange
        self._chain_provider = chain_provider
        self._expiries = expiries

    # ------------------------------------------------------------------ time
    @property
    def now(self) -> datetime:
        """Close timestamp of the current bar. The only clock a strategy gets.

        Deliberately not the wall clock: in a backtest they differ by years, and a
        strategy that could tell would be able to behave differently in live.
        """
        return self._window.current.ts

    @property
    def bar(self) -> Bar:
        return self._window.current

    @property
    def session(self) -> SessionInfo:
        return self._session

    @property
    def timeframe(self) -> Timeframe:
        return self._timeframe

    # --------------------------------------------------------------- history
    @property
    def bars(self) -> BarWindow:
        """Closed bars `[0..i]`. Indexing past the end raises `LookAheadError`."""
        return self._window

    def history(self, lookback: int) -> BarWindow:
        """The last `lookback` closed bars, ending at the current one."""
        if lookback < 1:
            raise DomainError(f"lookback must be >= 1, got {lookback}")
        return self._window.tail(lookback)

    def has_history(self, bars_required: int) -> bool:
        """Whether enough history exists for an indicator's warmup."""
        return len(self._window) >= bars_required

    # ----------------------------------------------------------------- chain
    def chain(self, underlying: str, option_expiry: date) -> OptionChainSnapshot:
        """The option chain as of this bar's close.

        Raises rather than returning an empty chain when no snapshot exists at
        this instant — an empty chain would silently select no strikes and look
        like a strategy that chose not to trade.
        """
        if self._chain_provider is None:
            raise DataError(
                "no chain provider is wired into this context; option access is "
                "available from Milestone 4 onwards"
            )
        snapshot = self._chain_provider.chain_at(underlying, option_expiry, self.now)
        if snapshot is None:
            raise DataError(
                f"no {underlying} chain snapshot for expiry {option_expiry} at {self.now}"
            )
        return snapshot

    # --------------------------------------------------------------- expiries
    def option_expiries(self, underlying: str) -> tuple[date, ...]:
        """Option expiry dates known for `underlying`, ascending.

        Read from the instrument master via the expiry calendar (D-023) — never
        computed from a weekday rule inside a strategy.
        """
        if self._expiries is None:
            raise DataError(
                "no expiry calendar is wired into this context; option expiry "
                "access needs one"
            )
        today = ist_date(self.now)
        found: list[date] = []
        year, month = today.year, today.month
        for _ in range(3):
            # A contract month the instrument master has never heard of is not
            # an error here — it simply is not listed yet. Anything else still
            # raises.
            with suppress(CalendarError):
                found.append(self._expiries.option_expiry(underlying, year, month))
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return tuple(sorted(d for d in found if d >= today))

    def nearest_expiry(self, underlying: str) -> ExpirySet:
        """The first cycle whose option expiry is on or after this bar's date."""
        if self._expiries is None:
            raise DataError("no expiry calendar is wired into this context")
        return self._expiries.nearest_expiry_on_or_after(underlying, ist_date(self.now))

    def days_to_expiry(self, underlying: str) -> int:
        return self.nearest_expiry(underlying).days_to_option_expiry(ist_date(self.now))

    # ----------------------------------------------------------------- specs
    def spec(self, underlying: str) -> InstrumentSpec:
        return self._specs.spec_for(underlying, self._exchange, ist_date(self.now))

    def tick_size(self, underlying: str) -> Decimal:
        return self.spec(underlying).tick_size

    # ------------------------------------------------------------- positions
    def positions(self) -> PositionView:
        return self._positions

    @property
    def is_flat(self) -> bool:
        return self._positions.is_flat

    def __repr__(self) -> str:
        return f"BarContext(at={self.now:%Y-%m-%d %H:%M}Z, history={len(self._window)} bars)"


def contexts_from_bars(
    bars: Sequence[Bar],
    *,
    calendar: MarketCalendar,
    specs: ContractSpecStore,
    timeframe: Timeframe,
    positions: PositionView | None = None,
    chain_provider: ChainProvider | None = None,
    exchange: Exchange = Exchange.MCX,
) -> Iterator[BarContext]:
    """Yield one context per bar, each seeing only `[0..i]`.

    This is *the* place history is sliced, and the backtest engine will use this
    same function rather than reimplementing it — so the look-ahead canaries
    exercise the real construction path, not a test-only imitation of it.

    The slice is materialised into a fresh tuple per bar. Copying is the point:
    handing over a reference to the full list, however carefully wrapped, leaves
    the future one attribute access away.
    """
    empty_positions = positions or PositionView({})

    # bars_in_session comes from the CALENDAR, never from counting the data.
    # Counting the series would tell a strategy how many bars today will have
    # before the day has finished — which is knowledge it cannot have live, and
    # is therefore look-ahead however innocuous it looks.
    session_bar_counts: dict[date, int] = {}

    seen_in_session: dict[date, int] = {}
    for index in range(len(bars)):
        bar = bars[index]
        session_day = ist_date(bar.ts)
        bar_index = seen_in_session.get(session_day, 0)
        seen_in_session[session_day] = bar_index + 1

        if session_day not in session_bar_counts:
            session_bar_counts[session_day] = len(
                calendar.bar_boundaries(session_day, timeframe)
            )

        window = BarWindow.of(tuple(bars[: index + 1]))
        session = build_session_info(
            bar=bar,
            session_close=calendar.session_close(session_day),
            is_us_dst=calendar.is_us_dst_session(session_day),
            bar_index=bar_index,
            bars_in_session=session_bar_counts[session_day],
        )
        yield BarContext(
            window=window,
            session=session,
            specs=specs,
            positions=empty_positions,
            timeframe=timeframe,
            exchange=exchange,
            chain_provider=chain_provider,
        )


def build_session_info(
    *,
    bar: Bar,
    session_close: datetime,
    is_us_dst: bool,
    bar_index: int,
    bars_in_session: int,
) -> SessionInfo:
    return SessionInfo(
        session_date=ist_date(bar.ts),
        is_us_dst=is_us_dst,
        minutes_to_close=minutes_between(bar.ts, session_close),
        is_partial_bar=bar.is_partial,
        bar_index=bar_index,
        bars_in_session=bars_in_session,
    )
