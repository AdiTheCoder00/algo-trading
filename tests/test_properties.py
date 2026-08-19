"""Property tests. Brief §11.

Example-based tests check the cases I thought of. These check the ones I did not.
Each property below is an invariant the rest of the system is allowed to assume,
so a counterexample here is a real defect rather than a curiosity.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from algo.core.bar import M1, Bar, BarWindow
from algo.core.enums import Side
from algo.core.fill import Fill
from algo.core.instrument import OptionId
from algo.core.money import is_on_tick, quantize_to_tick, round_down_to_lot_step
from algo.core.position import Position
from algo.core.timeutil import utc

prices = st.decimals(min_value=Decimal("0"), max_value=Decimal("1000000"), places=2)
ticks = st.sampled_from([Decimal("0.05"), Decimal("0.10"), Decimal("0.50"), Decimal("1")])
lot_counts = st.decimals(min_value=Decimal("0"), max_value=Decimal("10000"), places=4)
steps = st.integers(min_value=1, max_value=25)


class TestTickQuantisation:
    @given(price=prices, tick=ticks)
    def test_result_is_always_on_the_grid(self, price: Decimal, tick: Decimal) -> None:
        for side in ("BUY", "SELL"):
            assert is_on_tick(quantize_to_tick(price, tick, side=side), tick)

    @given(price=prices, tick=ticks)
    def test_a_buy_is_never_made_more_aggressive(self, price: Decimal, tick: Decimal) -> None:
        assert quantize_to_tick(price, tick, side="BUY") <= price

    @given(price=prices, tick=ticks)
    def test_a_sell_is_never_made_more_aggressive(self, price: Decimal, tick: Decimal) -> None:
        assert quantize_to_tick(price, tick, side="SELL") >= price

    @given(price=prices, tick=ticks)
    def test_quantisation_moves_less_than_one_tick(self, price: Decimal, tick: Decimal) -> None:
        for side in ("BUY", "SELL"):
            assert abs(quantize_to_tick(price, tick, side=side) - price) < tick


class TestLotSizing:
    @given(lots=lot_counts, step=steps)
    def test_never_rounds_up(self, lots: Decimal, step: int) -> None:
        """Brief §8. Rounding up would silently exceed the risk budget."""
        assert round_down_to_lot_step(lots, step) <= lots

    @given(lots=lot_counts, step=steps)
    def test_result_is_a_whole_number_of_steps(self, lots: Decimal, step: int) -> None:
        assert round_down_to_lot_step(lots, step) % step == 0

    @given(lots=lot_counts, step=steps)
    def test_within_one_step_of_the_request(self, lots: Decimal, step: int) -> None:
        rounded = round_down_to_lot_step(lots, step)
        assert lots - Decimal(rounded) < Decimal(step)

    @given(lots=lot_counts, step=steps)
    def test_never_negative(self, lots: Decimal, step: int) -> None:
        assert round_down_to_lot_step(lots, step) >= 0


class TestPositionAccounting:
    """Brief §11: portfolio equity always equals cash plus unrealised P&L."""

    @given(
        entry=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000"), places=2),
        mark=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000"), places=2),
        lots=st.integers(min_value=1, max_value=10),
        side=st.sampled_from([Side.BUY, Side.SELL]),
    )
    @settings(max_examples=200)
    def test_closing_at_the_mark_realises_exactly_the_unrealised(
        self, entry: Decimal, mark: Decimal, lots: int, side: Side
    ) -> None:
        """The identity the equity curve depends on.

        If unrealised and realised disagree, an equity curve can move at the
        moment a position closes even though nothing traded — which is the
        classic accounting bug that makes a backtest untrustworthy.
        """
        option = _option()
        opened = Position(instrument=option, multiplier=Decimal("10")).apply(
            _fill(option, side, lots, entry)
        )
        unrealised = opened.unrealised_pnl(mark)
        closed = opened.apply(_fill(option, side.opposite, lots, mark))
        assert closed.is_flat
        assert closed.realised_pnl == unrealised

    @given(
        entry=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000"), places=2),
        lots=st.integers(min_value=1, max_value=10),
        side=st.sampled_from([Side.BUY, Side.SELL]),
    )
    def test_marking_at_entry_is_flat_pnl(
        self, entry: Decimal, lots: int, side: Side
    ) -> None:
        option = _option()
        position = Position(instrument=option, multiplier=Decimal("10")).apply(
            _fill(option, side, lots, entry)
        )
        assert position.unrealised_pnl(entry) == Decimal("0")

    @given(
        first=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000"), places=2),
        second=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000"), places=2),
    )
    def test_average_price_lies_between_the_two_fills(
        self, first: Decimal, second: Decimal
    ) -> None:
        option = _option()
        position = (
            Position(instrument=option, multiplier=Decimal("10"))
            .apply(_fill(option, Side.SELL, 1, first))
            .apply(_fill(option, Side.SELL, 1, second))
        )
        assert min(first, second) <= position.average_price <= max(first, second)


class TestBarWindowInvariants:
    @given(count=st.integers(min_value=1, max_value=60))
    def test_the_window_never_exposes_more_than_it_was_given(self, count: int) -> None:
        bars = _bars(count)
        window = BarWindow.of(bars)
        assert len(window) == count
        assert window.current is bars[-1]

    @given(
        count=st.integers(min_value=1, max_value=60),
        take=st.integers(min_value=0, max_value=200),
    )
    def test_tail_never_widens(self, count: int, take: int) -> None:
        window = BarWindow.of(_bars(count))
        assert len(window.tail(take)) <= len(window)
        assert len(window.tail(take)) == min(take, count)


def _option() -> OptionId:
    from datetime import date

    from algo.core.enums import Right
    from algo.core.instrument import FutureId

    return OptionId(
        underlying_future=FutureId(underlying="GOLDM", expiry=date(2026, 9, 4)),
        option_expiry=date(2026, 8, 28),
        strike=Decimal("160500"),
        right=Right.CE,
    )


def _fill(option: OptionId, side: Side, lots: int, price: Decimal) -> Fill:
    return Fill(
        fill_id="f",
        client_order_id="c",
        signal_id="s",
        instrument=option,
        side=side,
        lots=lots,
        qty=Decimal(lots),
        price=price,
        ts=utc(2026, 8, 19, 4, 0),
    )


def _bars(count: int) -> list[Bar]:
    start = utc(2026, 8, 19, 3, 31)
    return [
        Bar(
            ts=start + timedelta(minutes=i),
            timeframe=M1,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1,
        )
        for i in range(count)
    ]
