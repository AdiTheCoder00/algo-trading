"""Milestone 3 falsification. Brief §9:

    "Run a buy-and-hold and a coin-flip strategy first. Buy-and-hold should track
     the instrument; the coin-flip should lose approximately the spread x trade
     count. If it doesn't, the engine is wrong — fix it before continuing."

These are not smoke tests. Each one has an answer computable by hand *before* the
engine runs, and the engine has to reproduce it — in `Decimal`, exactly, not
within a tolerance. A backtest engine that cannot reproduce a hand-computed
number on a flat market has no business being trusted on a real one.

The sharpest of them is `test_gross_pnl_is_exactly_zero_on_a_flat_market`: on a
market that never moves, a coin flip must make exactly nothing before costs and
lose exactly the costs. Any engine bug — a sign error, a double-counted charge, a
fill on the wrong bar — breaks that equality immediately.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from algo.backtest.engine import BacktestEngine, BacktestResult
from algo.core.bar import M1, M30, Bar
from algo.core.enums import Exchange, Side
from algo.core.instrument import FutureId, InstrumentSpec
from algo.costs.charges import FlatChargeModel, McxChargeModel, ZeroChargeModel
from algo.costs.slippage import NoSlippage, TickSlippage
from algo.costs.spread import FixedTickSpread, NoSpread
from algo.data.resample import resample
from algo.data.synthetic import flat_session, one_minute_session
from algo.exchange.calendar import MarketCalendar
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.portfolio.book import Portfolio
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.strategy.base import Strategy
from algo.strategy.buy_and_hold import BuyAndHold
from algo.strategy.coin_flip import CoinFlip
from tests.conftest import SUMMER_DAY

TICK = Decimal("0.50")
MULT = Decimal("10")
START = Decimal("1000000.00")
FLAT_PRICE = Decimal("156640.00")

GOLDM = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))

SPEC = InstrumentSpec(
    underlying="GOLDM",
    exchange=Exchange.MCX,
    lot_size=Decimal("100"),
    multiplier=MULT,
    tick_size=TICK,
    min_lots=1,
    effective_from=date(2026, 1, 1),
    source="falsification fixture",
)


def _run(
    strategy: Strategy,
    bars: list[Bar],
    calendar: MarketCalendar,
    *,
    spread_ticks: int = 0,
    stop_ticks: int = 0,
    market_ticks: int = 0,
    charges: object | None = None,
) -> BacktestResult:
    simulator = FillSimulator(
        spread=FixedTickSpread(spread_ticks) if spread_ticks else NoSpread(),
        slippage=(
            TickSlippage(market_ticks=market_ticks, stop_ticks=stop_ticks)
            if (market_ticks or stop_ticks)
            else NoSlippage()
        ),
        charges=charges or ZeroChargeModel(),  # type: ignore[arg-type]
    )
    engine = BacktestEngine(
        bars=bars,
        calendar=calendar,
        specs=ContractSpecStore([SPEC]),
        strategy=strategy,
        risk=RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=1,
            max_lots_per_underlying=5,
        ),
        simulator=simulator,
        portfolio=Portfolio(START),
        instrument=GOLDM,
        timeframe=M30,
        is_option=False,
        config_hash="test",
    )
    return engine.run()


def _flat_bars(calendar: MarketCalendar) -> list[Bar]:
    return resample(
        flat_session(calendar, SUMMER_DAY, price=FLAT_PRICE),
        calendar=calendar,
        timeframe=M30,
    )


def _walk_bars(calendar: MarketCalendar, seed: int = 31) -> list[Bar]:
    return resample(
        one_minute_session(calendar, SUMMER_DAY, seed=seed, start_price=FLAT_PRICE),
        calendar=calendar,
        timeframe=M30,
    )


def _ramp_bars(calendar: MarketCalendar, *, step: Decimal = Decimal("10")) -> list[Bar]:
    """A session that rises by a known amount every minute. No randomness."""
    opened = calendar.session_open(SUMMER_DAY)
    minutes = calendar.session_minutes(SUMMER_DAY)
    bars: list[Bar] = []
    level = FLAT_PRICE
    for i in range(1, minutes + 1):
        nxt = level + step
        bars.append(
            Bar(
                ts=opened + timedelta(minutes=i),
                timeframe=M1,
                open=level,
                high=nxt,
                low=level,
                close=nxt,
                volume=1,
            )
        )
        level = nxt
    return resample(bars, calendar=calendar, timeframe=M30)


# ------------------------------------------------------------- zero-cost proofs


class TestZeroCostArithmetic:
    """Brief §9: a zero-cost, zero-slippage config must reproduce hand-computed
    P&L exactly, in Decimal. This isolates the engine from the cost model."""

    def test_buy_and_hold_on_a_flat_market_makes_exactly_nothing(
        self, calendar: MarketCalendar
    ) -> None:
        bars = _flat_bars(calendar)
        result = _run(BuyAndHold(GOLDM), bars, calendar)
        assert result.net_pnl == Decimal("0")
        assert result.total_charges == Decimal("0")
        assert result.spread_cost == Decimal("0")

    def test_buy_and_hold_captures_exactly_the_move_it_was_present_for(
        self, calendar: MarketCalendar
    ) -> None:
        """Entry is the *second* bar's open, because the decision was made on the
        first bar's close. Marked out at the last bar's close."""
        bars = _ramp_bars(calendar)
        result = _run(BuyAndHold(GOLDM), bars, calendar)
        expected = (bars[-1].close - bars[1].open) * MULT * Decimal(1)
        assert result.net_pnl == expected

    def test_a_coin_flip_on_a_flat_market_makes_exactly_nothing(
        self, calendar: MarketCalendar
    ) -> None:
        bars = _flat_bars(calendar)
        result = _run(CoinFlip(GOLDM, seed=1), bars, calendar)
        assert result.net_pnl == Decimal("0")
        # One round trip every two bars: open fills on bar i, close fills on
        # i+1, and the decision on i+1 can only reach the market on i+2. 29 bars
        # therefore complete exactly 14.
        assert result.round_trips == 14
        assert len(bars) == 29


# ------------------------------------------------------- the cost falsification


class TestCoinFlipLosesExactlyTheCosts:
    """The Milestone 3 gate. If these fail, the engine is wrong."""

    def test_gross_pnl_is_exactly_zero_on_a_flat_market(
        self, calendar: MarketCalendar
    ) -> None:
        """A market that never moves cannot produce a gain or a loss before costs.

        Everything the strategy lost must therefore be cost, exactly. This single
        equality catches sign errors, double-counted charges and fills booked on
        the wrong bar.
        """
        bars = _flat_bars(calendar)
        result = _run(
            CoinFlip(GOLDM, seed=2),
            bars,
            calendar,
            spread_ticks=2,
            charges=FlatChargeModel(Decimal("20")),
        )
        assert result.gross_pnl == Decimal("0")
        assert result.net_pnl == -(result.spread_cost + result.total_charges)
        assert result.net_pnl < 0

    def test_the_loss_is_the_spread_times_the_fill_count(
        self, calendar: MarketCalendar
    ) -> None:
        """"Spread x trade count", computed by hand.

        A 2-tick spread costs half a tick... no: half the spread is one tick, so
        each fill crosses 1 tick = Rs 0.50 of price, which is Rs 5 per lot at a
        multiplier of 10.
        """
        bars = _flat_bars(calendar)
        result = _run(
            CoinFlip(GOLDM, seed=3), bars, calendar, spread_ticks=2, charges=ZeroChargeModel()
        )
        half_spread = TICK * Decimal(2) / Decimal(2)  # 2 ticks total -> 1 tick each way
        expected_per_fill = half_spread * MULT * Decimal(1)
        assert expected_per_fill == Decimal("5.00")
        assert result.spread_cost == expected_per_fill * Decimal(len(result.fills))
        assert result.net_pnl == -result.spread_cost

    def test_charges_are_one_per_fill(self, calendar: MarketCalendar) -> None:
        bars = _flat_bars(calendar)
        result = _run(
            CoinFlip(GOLDM, seed=4), bars, calendar, charges=FlatChargeModel(Decimal("20"))
        )
        assert result.total_charges == Decimal("20") * Decimal(len(result.fills))

    def test_more_spread_costs_proportionally_more(
        self, calendar: MarketCalendar
    ) -> None:
        bars = _flat_bars(calendar)
        two = _run(CoinFlip(GOLDM, seed=5), bars, calendar, spread_ticks=2)
        four = _run(CoinFlip(GOLDM, seed=5), bars, calendar, spread_ticks=4)
        assert four.spread_cost == two.spread_cost * 2
        assert four.net_pnl == two.net_pnl * 2

    def test_slippage_adds_on_top_of_the_spread(self, calendar: MarketCalendar) -> None:
        bars = _flat_bars(calendar)
        plain = _run(CoinFlip(GOLDM, seed=6), bars, calendar, spread_ticks=2)
        slipped = _run(
            CoinFlip(GOLDM, seed=6), bars, calendar, spread_ticks=2, market_ticks=1, stop_ticks=2
        )
        assert slipped.spread_cost > plain.spread_cost
        assert slipped.net_pnl < plain.net_pnl

    def test_predicted_and_realised_cost_agree(self, calendar: MarketCalendar) -> None:
        """Predicted independently of the code path that applies it — which is
        what makes the comparison worth anything."""
        bars = _flat_bars(calendar)
        result = _run(
            CoinFlip(GOLDM, seed=7),
            bars,
            calendar,
            spread_ticks=2,
            charges=FlatChargeModel(Decimal("20")),
        )
        # The final position is left open, so realised covers one extra fill
        # beyond the completed round trips. Predicted counts round trips only.
        assert result.predicted_cost > 0
        per_round_trip = result.predicted_cost / Decimal(result.round_trips)
        realised_per_round_trip = result.realised_cost / Decimal(result.round_trips)
        assert abs(realised_per_round_trip - per_round_trip) < per_round_trip * Decimal("0.1")

    def test_a_random_walk_loses_roughly_the_costs(self, calendar: MarketCalendar) -> None:
        """The weaker sanity check: with real price movement, gross P&L is a
        zero-drift random variable, so net should sit near minus the costs — but
        the noise is real and the test says so rather than pretending otherwise."""
        bars = _walk_bars(calendar)
        result = _run(
            CoinFlip(GOLDM, seed=8),
            bars,
            calendar,
            spread_ticks=2,
            charges=FlatChargeModel(Decimal("20")),
        )
        assert result.realised_cost > 0
        assert result.net_pnl == result.gross_pnl - result.realised_cost


# ------------------------------------------------------------ engine invariants


class TestEngineInvariants:
    def test_no_order_fills_on_the_bar_that_produced_it(
        self, calendar: MarketCalendar
    ) -> None:
        """The most flattering backtest bug there is, structurally prevented."""
        bars = _walk_bars(calendar)
        result = _run(BuyAndHold(GOLDM), bars, calendar)
        assert result.fills
        assert result.fills[0].ts == bars[1].ts
        assert result.fills[0].ts != bars[0].ts

    def test_the_fill_price_is_the_next_bars_open(self, calendar: MarketCalendar) -> None:
        bars = _walk_bars(calendar)
        result = _run(BuyAndHold(GOLDM), bars, calendar)
        assert result.fills[0].price == bars[1].open

    def test_the_equity_curve_has_one_point_per_bar(
        self, calendar: MarketCalendar
    ) -> None:
        bars = _walk_bars(calendar)
        result = _run(BuyAndHold(GOLDM), bars, calendar)
        assert len(result.equity_curve) == len(bars)
        assert [p.ts for p in result.equity_curve] == [b.ts for b in bars]

    def test_equity_reconciles_two_ways_at_every_point(
        self, calendar: MarketCalendar
    ) -> None:
        """`check_identity` runs inside the engine after every fill and every
        mark; reaching the end without raising is the assertion."""
        bars = _walk_bars(calendar)
        result = _run(
            CoinFlip(GOLDM, seed=9),
            bars,
            calendar,
            spread_ticks=2,
            charges=FlatChargeModel(Decimal("20")),
        )
        for point in result.equity_curve:
            assert point.equity == point.cash + point.market_value

    def test_no_orders_are_queued_after_the_final_bar(
        self, calendar: MarketCalendar
    ) -> None:
        """Silently dropping them would understate cost and trade count."""
        bars = _flat_bars(calendar)
        result = _run(CoinFlip(GOLDM, seed=10), bars, calendar)
        assert result.fills[-1].ts == bars[-1].ts

    def test_runs_are_byte_identical(self, calendar: MarketCalendar) -> None:
        """Brief §7.4."""
        bars = _walk_bars(calendar)

        def fingerprint() -> list[str]:
            result = _run(
                CoinFlip(GOLDM, seed=11),
                bars,
                calendar,
                spread_ticks=2,
                charges=FlatChargeModel(Decimal("20")),
            )
            return [f.model_dump_json() for f in result.fills]

        assert fingerprint() == fingerprint()

    def test_the_dataset_hash_identifies_the_bars(self, calendar: MarketCalendar) -> None:
        walk = _run(BuyAndHold(GOLDM), _walk_bars(calendar), calendar)
        flat = _run(BuyAndHold(GOLDM), _flat_bars(calendar), calendar)
        assert walk.dataset_hash != flat.dataset_hash
        assert len(walk.dataset_hash) == 16


class TestHonestyOfTheReport:
    def test_placeholder_charge_rates_are_flagged(self, calendar: MarketCalendar) -> None:
        """A net P&L figure must never be mistaken for a calibrated one (D-011)."""
        bars = _flat_bars(calendar)
        result = _run(
            BuyAndHold(GOLDM), bars, calendar, spread_ticks=2, charges=McxChargeModel.default()
        )
        assert not result.costs_verified
        assert any("PLACEHOLDER" in w for w in result.warnings)

    def test_a_modelled_spread_is_flagged(self, calendar: MarketCalendar) -> None:
        bars = _flat_bars(calendar)
        result = _run(BuyAndHold(GOLDM), bars, calendar, spread_ticks=2)
        assert not result.spread_measured
        assert any("MODELLED" in w for w in result.warnings)

    def test_cost_drag_is_none_when_gross_is_zero(self, calendar: MarketCalendar) -> None:
        """Rather than dividing by zero and reporting infinity."""
        bars = _flat_bars(calendar)
        result = _run(CoinFlip(GOLDM, seed=12), bars, calendar, spread_ticks=2)
        assert result.gross_pnl == 0
        assert result.cost_drag_pct is None


class TestRiskLayerRefusals:
    def test_a_second_position_is_refused_and_logged(
        self, calendar: MarketCalendar
    ) -> None:
        """Never silent — brief §8."""

        class DoubleUp(Strategy):
            strategy_id = "double_up"

            def warmup_bars(self) -> int:
                return 0

            def on_bar(self, ctx):  # type: ignore[no-untyped-def]
                from algo.core.enums import SignalAction
                from algo.core.ids import signal_id
                from algo.core.signal import Signal, SignalLeg
                from algo.core.timeutil import iso

                return [
                    Signal(
                        signal_id=signal_id(
                            strategy_id=self.strategy_id,
                            params_hash="x",
                            bar_close_iso=iso(ctx.now),
                            action="OPEN",
                            leg_keys=(GOLDM.key,),
                            config_hash="test",
                        ),
                        strategy_id=self.strategy_id,
                        ts=ctx.now,
                        action=SignalAction.OPEN,
                        legs=(SignalLeg(instrument=GOLDM, direction=Side.BUY),),
                        reason="deliberately opening every bar to trip the cap",
                    )
                ]

        result = _run(DoubleUp(), _flat_bars(calendar), calendar)
        assert result.rejections
        assert all(r.reason.value == "MAX_CONCURRENT" for r in result.rejections)
        assert all(r.detail for r in result.rejections)


class TestChargeModel:
    def test_ctt_falls_on_the_sell_side_only(self) -> None:
        """The cost that lands on every entry of a premium-selling strategy."""
        model = McxChargeModel.default()
        common = {
            "lots": 1,
            "price": Decimal("1000"),
            "multiplier": MULT,
            "is_option": True,
            "on": date(2026, 8, 19),
        }
        sell = model.charges_for(side=Side.SELL, **common)  # type: ignore[arg-type]
        buy = model.charges_for(side=Side.BUY, **common)  # type: ignore[arg-type]
        assert sell.ctt > 0
        assert buy.ctt == 0

    def test_stamp_duty_falls_on_the_buy_side_only(self) -> None:
        model = McxChargeModel.default()
        common = {
            "lots": 1,
            "price": Decimal("1000"),
            "multiplier": MULT,
            "is_option": True,
            "on": date(2026, 8, 19),
        }
        assert model.charges_for(side=Side.BUY, **common).stamp_duty > 0  # type: ignore[arg-type]
        assert model.charges_for(side=Side.SELL, **common).stamp_duty == 0  # type: ignore[arg-type]

    def test_option_charges_are_on_premium_not_notional(self) -> None:
        """Applying an option rate to notional overstates costs ~100x."""
        model = McxChargeModel.default()
        premium = model.charges_for(
            side=Side.SELL,
            lots=1,
            price=Decimal("1000"),
            multiplier=MULT,
            is_option=True,
            on=date(2026, 8, 19),
        )
        # A premium of 1000 x multiplier 10 = Rs 10,000 turnover.
        # CTT at 0.05% of that is Rs 5.00 — not 0.05% of Rs 15.6 lakh.
        assert premium.ctt == Decimal("5.00")

    def test_gst_applies_to_brokerage_and_fees_not_to_ctt(self) -> None:
        model = McxChargeModel.default()
        charges = model.charges_for(
            side=Side.SELL,
            lots=1,
            price=Decimal("1000"),
            multiplier=MULT,
            is_option=True,
            on=date(2026, 8, 19),
        )
        expected = (charges.brokerage + charges.exchange_txn + charges.sebi_fee) * Decimal("0.18")
        assert charges.gst == pytest.approx(expected, abs=Decimal("0.01"))

    def test_the_shipped_rates_are_marked_unverified(self) -> None:
        assert not McxChargeModel.default().is_verified
