"""The MCX charge stack, itemised.

Brief §10 asks for cost drag as a percentage of gross P&L, broken down. That is
only possible if the components are kept apart from the moment a fill happens —
reconstructing them later from a total is guesswork, and guesswork about costs is
how a losing strategy comes to look profitable.

Six components, three different bases:

| Component        | Charged on                          | Side  |
|------------------|-------------------------------------|-------|
| Brokerage        | per executed order                  | both  |
| CTT              | premium (options) / notional (fut.) | sell  |
| Exchange txn     | turnover                            | both  |
| SEBI turnover    | turnover                            | both  |
| Stamp duty       | turnover                            | buy   |
| GST              | brokerage + exchange + SEBI         | both  |

**CTT lands on the sell side, and the writer pays it.** For a strategy whose every
entry is a sale, that is a per-entry cost rather than a rounding item — which is
why it is a named field and not folded into "fees".

Decision D-011: the rates shipped here are **placeholders**. A model calibrated
against a real Angel One contract note replaces them, and until that happens
`ChargeRates.verified` is false and the live path refuses to use them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from algo.core.enums import Side
from algo.core.errors import ConfigError
from algo.core.fill import Charges
from algo.core.money import quantize_paisa

RATES_DIR = Path(__file__).parent.parent / "exchange" / "data"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class InstrumentClassRates(BaseModel):
    """Rates that differ between options and futures."""

    model_config = _FROZEN

    ctt_sell_pct: Decimal
    exchange_txn_pct: Decimal
    stamp_duty_buy_pct: Decimal


class ChargeRates(BaseModel):
    """One effective-dated set of rates.

    `verified` is the gate that keeps placeholder numbers out of a live account.
    It flips to true only when a real contract note has been reproduced to the
    paisa by a test.
    """

    model_config = _FROZEN

    verified: bool
    effective_from: date
    effective_to: date | None = None
    source: str = Field(min_length=1)

    brokerage_per_order: Decimal
    brokerage_pct_of_turnover: Decimal = Decimal("0")
    sebi_pct: Decimal
    gst_pct: Decimal

    option: InstrumentClassRates
    future: InstrumentClassRates

    def covers(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on <= self.effective_to

    def for_class(self, *, is_option: bool) -> InstrumentClassRates:
        return self.option if is_option else self.future


class ChargeModel(Protocol):
    """Anything that can price the cost of one fill."""

    @property
    def is_verified(self) -> bool:
        """False while the rates are placeholders. Reported on every run so a
        net P&L figure is never mistaken for a calibrated one (D-011)."""
        ...

    def charges_for(
        self,
        *,
        side: Side,
        lots: int,
        price: Decimal,
        multiplier: Decimal,
        is_option: bool,
        on: date,
    ) -> Charges: ...


class McxChargeModel:
    """The Indian commodity charge stack, applied per fill."""

    __slots__ = ("_rates",)

    def __init__(self, rates: list[ChargeRates]) -> None:
        if not rates:
            raise ConfigError("at least one set of charge rates is required")
        self._rates = sorted(rates, key=lambda r: r.effective_from)

    @classmethod
    def from_yaml(cls, path: Path) -> McxChargeModel:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "rates" not in raw:
            raise ConfigError(f"{path} must contain a top-level 'rates' list")
        return cls([ChargeRates.model_validate(entry) for entry in raw["rates"]])

    @classmethod
    def default(cls) -> McxChargeModel:
        return cls.from_yaml(RATES_DIR / "charges_mcx.yaml")

    def rates_on(self, on: date) -> ChargeRates:
        for rates in self._rates:
            if rates.covers(on):
                return rates
        raise ConfigError(
            f"no charge rates in force on {on}. Known ranges: "
            + ", ".join(f"{r.effective_from}..{r.effective_to or 'open'}" for r in self._rates)
        )

    @property
    def is_verified(self) -> bool:
        """False while any rate set is still a placeholder.

        Reported on every backtest so a net P&L figure is never mistaken for a
        calibrated one.
        """
        return all(r.verified for r in self._rates)

    def charges_for(
        self,
        *,
        side: Side,
        lots: int,
        price: Decimal,
        multiplier: Decimal,
        is_option: bool,
        on: date,
    ) -> Charges:
        rates = self.rates_on(on)
        klass = rates.for_class(is_option=is_option)

        # For an option, "turnover" is premium turnover — the exchange charges on
        # what changed hands, not on the notional the contract controls. Getting
        # this wrong inflates option costs by three orders of magnitude.
        turnover = price * multiplier * Decimal(lots)

        brokerage = rates.brokerage_per_order + _pct(turnover, rates.brokerage_pct_of_turnover)
        ctt = _pct(turnover, klass.ctt_sell_pct) if side is Side.SELL else Decimal("0")
        exchange_txn = _pct(turnover, klass.exchange_txn_pct)
        sebi_fee = _pct(turnover, rates.sebi_pct)
        stamp_duty = _pct(turnover, klass.stamp_duty_buy_pct) if side is Side.BUY else Decimal("0")
        gst = _pct(brokerage + exchange_txn + sebi_fee, rates.gst_pct)

        return Charges(
            brokerage=quantize_paisa(brokerage),
            ctt=quantize_paisa(ctt),
            exchange_txn=quantize_paisa(exchange_txn),
            sebi_fee=quantize_paisa(sebi_fee),
            stamp_duty=quantize_paisa(stamp_duty),
            gst=quantize_paisa(gst),
        )


class ZeroChargeModel:
    """No charges at all. For the zero-cost falsification only.

    Brief §9 Milestone 3: a zero-cost, zero-slippage configuration must reproduce
    hand-computed P&L exactly. This exists so that test can isolate the engine's
    arithmetic from the cost model's.
    """

    def charges_for(
        self,
        *,
        side: Side,
        lots: int,
        price: Decimal,
        multiplier: Decimal,
        is_option: bool,
        on: date,
    ) -> Charges:
        del side, lots, price, multiplier, is_option, on
        return Charges()

    @property
    def is_verified(self) -> bool:
        return True


class FlatChargeModel:
    """A fixed rupee amount per order. Used where a test needs a cost it can
    predict by hand without reimplementing the whole stack."""

    __slots__ = ("_per_order",)

    def __init__(self, per_order: Decimal) -> None:
        self._per_order = per_order

    def charges_for(
        self,
        *,
        side: Side,
        lots: int,
        price: Decimal,
        multiplier: Decimal,
        is_option: bool,
        on: date,
    ) -> Charges:
        del side, lots, price, multiplier, is_option, on
        return Charges(brokerage=self._per_order)

    @property
    def is_verified(self) -> bool:
        return True


def _pct(amount: Decimal, rate_pct: Decimal) -> Decimal:
    return amount * rate_pct / Decimal("100")
