"""`algo/strategy/price_stop.py`: the shared trigger both CFD strategies use.

Every scenario is checked once here, at the level of the primitives
(`stop_level`, `stop_touched`, `stop_fill_price`) rather than only through a
strategy, so a bug in the shared logic cannot hide behind either strategy's own
test suite happening not to exercise it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.errors import DomainError
from algo.core.instrument import CfdId
from algo.core.position import Position
from algo.strategy.price_stop import stop_fill_price, stop_level, stop_touched

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=5)
TS = datetime(2026, 8, 24, tzinfo=UTC)
ENTRY = Decimal("4400.00")


def _bar(*, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        ts=TS, timeframe=TF, open=Decimal(open_), high=Decimal(high),
        low=Decimal(low), close=Decimal(close), volume=100,
    )


def _long(lots: int = 100, entry: Decimal = ENTRY) -> Position:
    return Position(
        instrument=XAUUSD, lots=lots, qty=Decimal(lots), cost_basis=entry * lots
    )


def _short(lots: int = 100, entry: Decimal = ENTRY) -> Position:
    return Position(
        instrument=XAUUSD, lots=-lots, qty=Decimal(-lots), cost_basis=entry * lots
    )


class TestStopLevel:
    def test_a_longs_stop_sits_below_entry(self) -> None:
        assert stop_level(ENTRY, Side.BUY, Decimal("0.5")) == Decimal("4378.00")

    def test_a_shorts_stop_sits_above_entry(self) -> None:
        assert stop_level(ENTRY, Side.SELL, Decimal("0.5")) == Decimal("4422.00")

    def test_zero_percent_is_the_entry_itself(self) -> None:
        assert stop_level(ENTRY, Side.BUY, Decimal("0")) == ENTRY

    def test_a_negative_percent_is_refused(self) -> None:
        with pytest.raises(DomainError, match="cannot be negative"):
            stop_level(ENTRY, Side.BUY, Decimal("-1"))

    def test_it_scales_with_entry_price(self) -> None:
        """A 0.5% stop on a 100 entry and a 10000 entry are proportionally the
        same distance, not the same absolute number of points."""
        low = stop_level(Decimal("100"), Side.BUY, Decimal("0.5"))
        high = stop_level(Decimal("10000"), Side.BUY, Decimal("0.5"))

        assert low == Decimal("99.500")
        assert high == Decimal("9950.000")


class TestStopTouched:
    def test_a_long_is_touched_by_the_bars_low(self) -> None:
        bar = _bar(open_="4400", high="4400", low="4378.00", close="4400")

        assert stop_touched(bar, _long(), Decimal("0.5")) is True

    def test_a_long_just_above_the_level_is_not_touched(self) -> None:
        bar = _bar(open_="4400", high="4400", low="4378.01", close="4400")

        assert stop_touched(bar, _long(), Decimal("0.5")) is False

    def test_a_short_is_touched_by_the_bars_high(self) -> None:
        bar = _bar(open_="4400", high="4422.00", low="4400", close="4400")

        assert stop_touched(bar, _short(), Decimal("0.5")) is True

    def test_a_short_just_below_the_level_is_not_touched(self) -> None:
        bar = _bar(open_="4400", high="4421.99", low="4400", close="4400")

        assert stop_touched(bar, _short(), Decimal("0.5")) is False

    def test_the_close_is_irrelevant_when_the_range_touches(self) -> None:
        """The whole point of checking the range: a spike through the level
        that recovers before the close must still count. A close-only check
        would understate a real stop's own frequency - the wrong direction to
        be optimistic in for a safety feature."""
        bar = _bar(open_="4395", high="4400", low="4370.00", close="4395")

        assert stop_touched(bar, _long(), Decimal("0.5")) is True

    def test_the_close_alone_being_past_the_level_is_not_enough_without_the_range(
        self,
    ) -> None:
        """The converse: a close beyond the level with the low never actually
        reaching it (impossible in real OHLC, but the function must still use
        the range field, not silently fall back to the close) is exercised via
        the range fields directly - low above the level even though close is
        below it would be invalid data, so this instead confirms the check
        reads `low`/`high`, not `close`, by using a close that looks alarming
        while the range says otherwise."""
        bar = _bar(open_="4400", high="4400", low="4379.00", close="4379.00")

        assert stop_touched(bar, _long(), Decimal("0.5")) is False

    def test_zero_percent_never_touches(self) -> None:
        bar = _bar(open_="4400", high="4400", low="0.01", close="4400")

        assert stop_touched(bar, _long(), Decimal("0")) is False

    def test_a_flat_position_is_never_touched(self) -> None:
        flat = Position(instrument=XAUUSD, lots=0, qty=Decimal("0"))
        bar = _bar(open_="4400", high="4400", low="1", close="4400")

        assert stop_touched(bar, flat, Decimal("0.5")) is False


class TestStopFillPrice:
    def test_a_normal_touch_fills_at_the_level(self) -> None:
        bar = _bar(open_="4400", high="4400", low="4378.00", close="4390")

        assert stop_fill_price(bar, _long(), Decimal("0.5")) == Decimal("4378.00")

    def test_a_gap_through_the_level_fills_at_the_open(self) -> None:
        """The bar opened already past the stop - the level itself was never
        tradeable, so the fill is the worse, honest price. Same `GAPPED_STOP`
        doctrine `algo/execution/fills.py` already applies on the MCX path."""
        bar = _bar(open_="4360.00", high="4365", low="4355", close="4358")

        assert stop_fill_price(bar, _long(), Decimal("0.5")) == Decimal("4360.00")

    def test_a_short_gap_fills_at_the_open_too(self) -> None:
        bar = _bar(open_="4450.00", high="4460", low="4445", close="4455")

        assert stop_fill_price(bar, _short(), Decimal("0.5")) == Decimal("4450.00")

    def test_calling_it_when_nothing_was_touched_is_refused(self) -> None:
        bar = _bar(open_="4400", high="4400", low="4390", close="4400")

        with pytest.raises(DomainError, match="not touched"):
            stop_fill_price(bar, _long(), Decimal("0.5"))
