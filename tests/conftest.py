"""Shared fixtures.

Everything here is synthetic and offline. Brief §2.8: no network calls in unit
tests, ever — the broker and the feed are interfaces with fakes.

Dates are chosen deliberately, not arbitrarily:

*   2026-08-19 is the day the live GOLDM chain was captured, and sits inside US
    daylight saving (session closes 23:30 IST, exactly 29 half-hour bars).
*   2026-11-10 sits outside it (session closes 23:55 IST, 29 bars plus a stub).

Having one of each in the fixtures means every test that touches the session
clock is exercised in both regimes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from algo.core.enums import Exchange, Right
from algo.core.instrument import FutureId, InstrumentSpec, OptionId
from algo.exchange.calendar import MarketCalendar, synthetic_calendar
from algo.exchange.specs import ContractSpecStore

#: Inside US DST — MCX closes 23:30 IST.
SUMMER_DAY = date(2026, 8, 19)
#: Outside US DST — MCX closes 23:55 IST.
WINTER_DAY = date(2026, 11, 10)


@pytest.fixture
def calendar() -> MarketCalendar:
    return synthetic_calendar()


@pytest.fixture
def goldm_spec() -> InstrumentSpec:
    return InstrumentSpec(
        underlying="GOLDM",
        exchange=Exchange.MCX,
        lot_size=Decimal("100"),
        multiplier=Decimal("10"),
        tick_size=Decimal("0.50"),
        strike_interval=Decimal("500"),
        min_lots=1,
        effective_from=date(2026, 1, 1),
        source="test fixture",
    )


@pytest.fixture
def specs(goldm_spec: InstrumentSpec) -> ContractSpecStore:
    return ContractSpecStore([goldm_spec])


@pytest.fixture
def goldm_future() -> FutureId:
    return FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))


@pytest.fixture
def goldm_call(goldm_future: FutureId) -> OptionId:
    """The 0.25-delta call from the observed chain: 160500 CE, 28 Aug 2026."""
    return OptionId(
        underlying_future=goldm_future,
        option_expiry=date(2026, 8, 28),
        strike=Decimal("160500"),
        right=Right.CE,
    )


@pytest.fixture
def goldm_put(goldm_future: FutureId) -> OptionId:
    """The 0.25-delta put from the observed chain: 153000 PE, 28 Aug 2026."""
    return OptionId(
        underlying_future=goldm_future,
        option_expiry=date(2026, 8, 28),
        strike=Decimal("153000"),
        right=Right.PE,
    )
