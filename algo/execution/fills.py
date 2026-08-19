"""The fill simulator — shared verbatim by the backtest and the paper adapter.

Brief §4: "If backtest and live use different code paths for anything except I/O,
you have built two systems that will disagree." This module is the reason paper
trading can be a check on the backtest rather than a second opinion from a second
implementation.

Two conventions decide everything downstream, so both are stated once here:

**When does a signal execute?** A signal is produced on bar `i`'s close. Its order
executes at bar `i+1`'s **open**. Filling at bar `i`'s own close would let a
decision made from a bar profit from that same bar — the subtlest form of
look-ahead there is, because the numbers all look plausible.

**Which way does a fill round?** Against us, always. A buy fills at the higher
tick, a sell at the lower. Rounding the friendly way is a free fraction of a tick
on every trade.

Intrabar assumptions follow brief §6 exactly, and they are pessimistic on purpose:

* A stop inside the bar's range fills at the stop price plus slippage.
* A gap through the stop fills at the **open**, not the stop price.
* If both the stop and the target sit inside one bar, **the stop is assumed to
  have hit first** — because bar data cannot tell us the order, and the
  alternative flatters every result.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from algo.core.bar import Bar
from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.core.fill import Charges, Fill
from algo.core.instrument import InstrumentSpec
from algo.core.money import worst_tick_for_fill
from algo.costs.charges import ChargeModel
from algo.costs.slippage import SlippageModel
from algo.costs.spread import SpreadModel


class ExitTrigger(StrEnum):
    NONE = "NONE"
    STOP = "STOP"
    TARGET = "TARGET"
    GAPPED_STOP = "GAPPED_STOP"


class ExitCheck(BaseModel):
    """Whether a protective level was touched inside a bar, and at what price."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: ExitTrigger
    price: Decimal | None = None

    @property
    def fired(self) -> bool:
        return self.trigger is not ExitTrigger.NONE

    @property
    def is_stop(self) -> bool:
        return self.trigger in (ExitTrigger.STOP, ExitTrigger.GAPPED_STOP)


def check_exit(
    bar: Bar,
    *,
    position_side: Side,
    stop: Decimal | None,
    target: Decimal | None,
) -> ExitCheck:
    """Decide whether a stop or target was hit within `bar`.

    `position_side` is the side of the *open position*: BUY for a long, SELL for a
    short. A long is stopped out below and takes profit above; a short is the
    mirror. Getting this backwards is the kind of sign error that produces a
    beautiful equity curve, so it is spelled out rather than inferred.
    """
    is_long = position_side is Side.BUY

    stop_hit = stop is not None and (bar.low <= stop if is_long else bar.high >= stop)
    target_hit = target is not None and (bar.high >= target if is_long else bar.low <= target)

    if stop_hit and stop is not None:
        gapped = bar.open <= stop if is_long else bar.open >= stop
        if gapped:
            # Brief §6: gaps fill at the open, not at the stop price. This is
            # where the overnight risk on a short strangle actually shows up.
            return ExitCheck(trigger=ExitTrigger.GAPPED_STOP, price=bar.open)
        return ExitCheck(trigger=ExitTrigger.STOP, price=stop)

    if target_hit and target is not None:
        return ExitCheck(trigger=ExitTrigger.TARGET, price=target)

    return ExitCheck(trigger=ExitTrigger.NONE)


class FillSimulator:
    """Turns an intended trade into a `Fill`, with the costs attached."""

    __slots__ = ("_charges", "_slippage", "_spread")

    def __init__(
        self,
        *,
        spread: SpreadModel,
        slippage: SlippageModel,
        charges: ChargeModel,
    ) -> None:
        self._spread = spread
        self._slippage = slippage
        self._charges = charges

    @property
    def costs_verified(self) -> bool:
        """Whether the charge rates behind this simulator are calibrated."""
        return self._charges.is_verified

    @property
    def spread_measured(self) -> bool:
        """Whether the spread came from a real book rather than a model."""
        return self._spread.is_measured

    def fill(
        self,
        *,
        fill_id: str,
        client_order_id: str,
        signal_id: str,
        instrument_key: str,
        instrument: object,
        side: Side,
        lots: int,
        reference_price: Decimal,
        spec: InstrumentSpec,
        ts_utc: object,
        session_day: date,
        is_option: bool,
        is_stop: bool = False,
    ) -> Fill:
        """Price a fill at `reference_price`, moved against us and charged."""
        del instrument_key
        if lots < 1:
            raise DomainError(f"cannot fill {lots} lots")
        if reference_price <= 0:
            raise DomainError(f"cannot fill at a non-positive price {reference_price}")

        tick = spec.tick_size
        half_spread = self._spread.half_spread(reference_price, tick)
        extra = self._slippage.extra(tick=tick, is_stop=is_stop)
        adverse = half_spread + extra

        raw = reference_price + adverse if side is Side.BUY else reference_price - adverse
        if raw <= 0:
            raise DomainError(
                f"slippage of {adverse} drove the fill price to {raw} from {reference_price}"
            )
        filled = worst_tick_for_fill(raw, tick, side=side.value)

        charges = self._charges.charges_for(
            side=side,
            lots=lots,
            price=filled,
            multiplier=spec.multiplier,
            is_option=is_option,
            on=session_day,
        )

        return Fill(
            fill_id=fill_id,
            client_order_id=client_order_id,
            signal_id=signal_id,
            instrument=instrument,  # type: ignore[arg-type]
            side=side,
            lots=lots,
            qty=Decimal(lots),
            price=filled,
            ts=ts_utc,  # type: ignore[arg-type]
            charges=charges,
            slippage=abs(filled - reference_price),
            is_modelled=not self._spread.is_measured,
        )

    def predicted_round_trip_cost(
        self, *, price: Decimal, lots: int, spec: InstrumentSpec, is_option: bool, on: date
    ) -> tuple[Decimal, Charges]:
        """What one complete in-and-out should cost, before it happens.

        Used by the Milestone 3 falsification: the engine reports this alongside
        the realised figure, and a divergence between them means the engine is
        wrong. Predicting the cost independently of the code path that applies it
        is what makes that comparison worth anything.
        """
        tick = spec.tick_size
        half_spread = self._spread.half_spread(price, tick)
        extra = self._slippage.extra(tick=tick, is_stop=False)
        # Both legs cross the book, so a round trip pays the full spread.
        spread_cost = (half_spread + extra) * Decimal(2) * spec.multiplier * Decimal(lots)

        buy = self._charges.charges_for(
            side=Side.BUY,
            lots=lots,
            price=price,
            multiplier=spec.multiplier,
            is_option=is_option,
            on=on,
        )
        sell = self._charges.charges_for(
            side=Side.SELL,
            lots=lots,
            price=price,
            multiplier=spec.multiplier,
            is_option=is_option,
            on=on,
        )
        return spread_cost, buy + sell
