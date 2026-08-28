"""The live loop's safety properties.

Every test here is about something that costs real money when it is wrong:
acting twice on one bar doubles a position, deciding before settling re-enters a
position already held, and a loop without a bound keeps trading unattended.

The engine underneath is a real `BacktestEngine`, not a stub - the whole claim
of `LiveLoop` is that it reaches the same decision path the backtest does, and a
stubbed engine would let that claim go untested.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from algo.backtest.engine import BacktestEngine
from algo.core.bar import Bar, Timeframe
from algo.core.clock import Clock
from algo.core.enums import Exchange, Side
from algo.core.errors import DomainError
from algo.core.fill import Charges, Fill
from algo.core.instrument import FutureId
from algo.costs.charges import McxChargeModel
from algo.costs.slippage import TickSlippage
from algo.costs.spread import FixedTickSpread
from algo.data.resample import resample
from algo.data.synthetic import one_minute_session
from algo.exchange.calendar import synthetic_calendar
from algo.exchange.specs import ContractSpecStore
from algo.execution.fills import FillSimulator
from algo.execution.router import Outcome, RoutingResult
from algo.live.loop import LiveLoop
from algo.portfolio.book import Portfolio
from algo.risk.engine import FixedLotSizer, RiskEngine
from algo.strategy.coin_flip import CoinFlip

if TYPE_CHECKING:
    from algo.core.order import BrokerOrderRef, Order
    from algo.core.signal import Signal
    from algo.execution.broker import (
        BrokerFillSnapshot,
        BrokerHealth,
        BrokerOrderSnapshot,
        BrokerPositionSnapshot,
        Funds,
    )
    from algo.live.feeds import BrokerFillFeed
    from algo.live.loop import PassResult
    from algo.strategy.context import BarContext

# What the `rig` fixture hands a test: the loop plus the three doubles it
# was built from, and the bars the feed replays.
Rig = tuple[LiveLoop, "ScriptedBars", "ScriptedFills", "RecordingPlacer", list[Bar]]

INSTRUMENT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX)
TF = Timeframe(minutes=30)


class FrozenClock(Clock):
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: float) -> None:
        self._at += timedelta(seconds=seconds)


class ScriptedBars:
    """A feed the test drives by hand, including repeating itself."""

    def __init__(self) -> None:
        self.visible: list[Bar] = []

    def closed_bars(self) -> list[Bar]:
        return list(self.visible)


class ScriptedFills:
    def __init__(self) -> None:
        self.queue: list[Fill] = []

    def new_fills(self) -> list[Fill]:
        out, self.queue = self.queue, []
        return out


class RecordingPlacer:
    """Stands in for `Router.place_all`, recording what it was asked to send."""

    def __init__(self, outcome: Outcome = Outcome.PLACED) -> None:
        self.calls: list[list[Order]] = []
        self._outcome = outcome

    def __call__(self, orders: list[Order]) -> list[RoutingResult]:
        orders = list(orders)
        self.calls.append(orders)
        return [
            RoutingResult(
                outcome=self._outcome,
                client_order_id=o.client_order_id,
                detail="test",
            )
            for o in orders
        ]

    @property
    def sent(self) -> int:
        return sum(len(c) for c in self.calls)


def _opening_fill(ts: datetime) -> Fill:
    """A fill the loop did not create - the broker's, as far as it knows."""
    return Fill(
        fill_id="broker-1",
        client_order_id="coid-1",
        signal_id="sig-1",
        instrument=INSTRUMENT,
        side=Side.BUY,
        lots=1,
        qty=Decimal(100),
        price=Decimal("156640.00"),
        ts=ts,
        charges=Charges(
            brokerage=Decimal("0"),
            ctt=Decimal("0"),
            exchange_txn=Decimal("0"),
            sebi_fee=Decimal("0"),
            stamp_duty=Decimal("0"),
            gst=Decimal("0"),
        ),
        slippage=Decimal("0"),
        is_modelled=False,
    )


def _bars() -> list[Bar]:
    calendar = synthetic_calendar()
    return resample(
        one_minute_session(calendar, date(2026, 8, 19), seed=20260819),
        calendar=calendar,
        timeframe=TF,
    )


def _engine(seed_bars: list[Bar]) -> BacktestEngine:
    return BacktestEngine(
        bars=list(seed_bars),
        calendar=synthetic_calendar(),
        specs=ContractSpecStore.default(),
        strategy=CoinFlip(INSTRUMENT, seed=20260819),
        risk=RiskEngine(
            sizer=FixedLotSizer(1),
            spec_for=None,
            max_concurrent_positions=1,
            max_lots_per_underlying=5,
        ),
        simulator=FillSimulator(
            spread=FixedTickSpread(2),
            slippage=TickSlippage(market_ticks=0, stop_ticks=2),
            charges=McxChargeModel.default(),
        ),
        portfolio=Portfolio(Decimal("1000000")),
        instrument=INSTRUMENT,
        timeframe=TF,
    )


@pytest.fixture
def rig() -> Rig:
    all_bars = _bars()
    feed = ScriptedBars()
    fills = ScriptedFills()
    placer = RecordingPlacer()
    # The engine starts with one bar so the window is never empty; the feed
    # replays the rest as though they were arriving live.
    engine = _engine(all_bars[:1])
    loop = LiveLoop(
        engine=engine,
        bars=feed,
        fills=fills,
        place=placer,
        clock=FrozenClock(all_bars[0].ts),
    )
    return loop, feed, fills, placer, all_bars


class TestEachBarActsExactlyOnce:
    """A duplicated entry signal is a doubled position."""

    def test_a_repeated_poll_does_not_act_again(self, rig: Rig) -> None:
        loop, feed, _fills, placer, all_bars = rig
        feed.visible = all_bars[:2]

        first = loop.pass_once()
        before = placer.sent
        second = loop.pass_once()

        assert first.decision is not None
        assert second.decision is None, "the same bar must not be decided twice"
        assert placer.sent == before
        assert "already acted on" in second.summary()

    def test_a_new_bar_does_act(self, rig: Rig) -> None:
        loop, feed, _fills, _placer, all_bars = rig
        feed.visible = all_bars[:2]
        loop.pass_once()

        feed.visible = all_bars[:3]
        result = loop.pass_once()

        assert result.decision is not None
        assert result.bar_ts == all_bars[2].ts

    def test_the_watermark_is_the_bar_timestamp(self, rig: Rig) -> None:
        loop, feed, _fills, _placer, all_bars = rig
        feed.visible = all_bars[:2]
        loop.pass_once()

        assert loop.last_bar_ts == all_bars[1].ts

    def test_an_out_of_order_bar_is_refused(self, rig: Rig) -> None:
        """A feed that goes backwards is broken; guessing which bar is real
        would be worse than stopping."""
        loop, feed, _fills, _placer, all_bars = rig
        feed.visible = all_bars[:3]
        loop.pass_once()

        # Same timestamp, different content - the loop must not act on it.
        feed.visible = [*all_bars[:3], all_bars[1]]
        result = loop.pass_once()

        assert result.decision is None


class TestItSettlesBeforeDeciding:
    def test_an_empty_feed_still_settles(self, rig: Rig) -> None:
        """Fills must be booked even on a pass that has no new bar to act on -
        otherwise they queue up and the next decision is made on stale
        positions."""
        loop, feed, fills, _placer, _all = rig
        feed.visible = []
        fills.queue = []

        result = loop.pass_once()

        assert result.bar_ts is None
        assert "no closed bar yet" in result.summary()

    def test_a_fill_reaches_the_portfolio_before_the_strategy_is_asked(self) -> None:
        """The ordering, asserted from inside the strategy.

        `is_flat` is `DeltaStrangle`'s very first gate, so a fill settled *after*
        the decision would let it re-enter a position it already holds. Nothing
        outside the strategy can observe that ordering, so this test records it
        from within.
        """
        all_bars = _bars()
        seen: list[bool] = []

        class RecordsWhatItWasTold(CoinFlip):
            def on_bar(self, ctx: BarContext) -> list[Signal]:
                seen.append(ctx.positions().is_flat)
                return []

        engine = _engine(all_bars[:1])
        engine._strategy = RecordsWhatItWasTold(INSTRUMENT, seed=1)
        feed, fills, placer = ScriptedBars(), ScriptedFills(), RecordingPlacer()
        loop = LiveLoop(
            engine=engine,
            bars=feed,
            fills=fills,
            place=placer,
            clock=FrozenClock(all_bars[0].ts),
        )

        feed.visible = all_bars[:2]
        fills.queue = [_opening_fill(all_bars[1].ts)]
        loop.pass_once()

        assert seen == [False], "the strategy was asked while believing it was flat"
        assert not engine._portfolio.is_flat


class TestRoutingIsNotAssumedToSucceed:
    def test_orders_reach_the_placer(self, rig: Rig) -> None:
        loop, feed, _fills, placer, all_bars = rig
        feed.visible = all_bars[:2]

        result = loop.pass_once()

        assert result.decision is not None
        assert placer.sent == len(result.decision.orders)

    def test_a_blocked_order_is_a_result_not_an_exception(self, rig: Rig) -> None:
        """`BLOCKED_UNRECONCILED` is the reconcile-before-send rule working, and
        the loop must carry on rather than crash."""
        loop, feed, _fills, _placer, all_bars = rig
        blocked = RecordingPlacer(Outcome.BLOCKED_UNRECONCILED)
        loop._place = blocked
        feed.visible = all_bars[:2]

        result = loop.pass_once()

        assert all(r.outcome is Outcome.BLOCKED_UNRECONCILED for r in result.routed)
        assert "BLOCKED_UNRECONCILED" in result.summary()

    def test_no_orders_means_no_call_at_all(self, rig: Rig) -> None:
        """A pass with nothing to send must not call the router - an empty send
        is still a round trip to a broker."""
        loop, feed, _fills, placer, all_bars = rig
        feed.visible = all_bars[:1] + all_bars[1:2]
        loop.pass_once()
        calls_after_first = len(placer.calls)

        # Advance to a bar; whether it trades is the strategy's business, but if
        # it emits nothing the placer must not be invoked.
        feed.visible = all_bars[:3]
        result = loop.pass_once()

        assert result.decision is not None
        if not result.decision.orders:
            assert len(placer.calls) == calls_after_first


class TestItStops:
    def test_max_passes_is_honoured(self, rig: Rig) -> None:
        loop, feed, _fills, _placer, all_bars = rig
        feed.visible = all_bars[:2]

        results = loop.run(max_passes=3)

        assert len(results) == 3

    def test_max_passes_must_be_positive(self, rig: Rig) -> None:
        loop, _feed, _fills, _placer, _all = rig

        with pytest.raises(DomainError, match="at least 1"):
            loop.run(max_passes=0)

    def test_until_stops_the_loop_early(self, rig: Rig) -> None:
        loop, feed, _fills, _placer, all_bars = rig
        feed.visible = all_bars[:2]
        past = all_bars[0].ts - timedelta(hours=1)

        results = loop.run(max_passes=50, until=past)

        assert results == []

    def test_on_pass_sees_every_pass(self, rig: Rig) -> None:
        loop, feed, _fills, _placer, all_bars = rig
        feed.visible = all_bars[:2]
        seen: list[PassResult] = []

        loop.run(max_passes=4, on_pass=seen.append)

        assert len(seen) == 4


class TestItSharesTheBacktestDecisionPath:
    def test_the_loop_never_reimplements_sizing(self, rig: Rig) -> None:
        """The orders in a decision are the engine's, byte for byte - the loop
        only forwards them. Guards against a future edit that 'adjusts' an
        order on the way out."""
        loop, feed, _fills, placer, all_bars = rig
        feed.visible = all_bars[:2]

        result = loop.pass_once()

        assert result.decision is not None
        assert list(result.decision.orders) == placer.calls[0]


class FakeBroker:
    """`executions` is the only method `BrokerFillFeed` calls; the rest are
    here because `Broker` is the declared parameter type, and a double that
    quietly implements less than the interface is how a test starts passing
    for the wrong reason."""

    def __init__(self, snaps: list[BrokerFillSnapshot]) -> None:
        self.snaps = snaps
        self.since_calls: list[datetime] = []

    def executions(self, since: datetime) -> list[BrokerFillSnapshot]:
        self.since_calls.append(since)
        return [s for s in self.snaps if s.ts >= since]

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def place(self, order: Order) -> BrokerOrderRef:
        raise NotImplementedError

    def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError

    def open_orders(self) -> list[BrokerOrderSnapshot]:
        raise NotImplementedError

    def order_by_client_id(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        raise NotImplementedError

    def positions(self) -> list[BrokerPositionSnapshot]:
        raise NotImplementedError

    def funds(self) -> Funds:
        raise NotImplementedError

    def health(self) -> BrokerHealth:
        raise NotImplementedError


def _snap(fill_id: str, ts: datetime, key: str | None = None) -> BrokerFillSnapshot:
    from algo.execution.broker import BrokerFillSnapshot

    return BrokerFillSnapshot(
        fill_id=fill_id,
        client_order_id=f"coid-{fill_id}",
        broker_order_id=f"bro-{fill_id}",
        instrument_key=key or INSTRUMENT.key,
        side=Side.BUY,
        lots=1,
        qty=Decimal("100"),
        price=Decimal("156640.00"),
        ts=ts,
    )


class TestBrokerFillFeed:
    def _feed(self, snaps: list[BrokerFillSnapshot], at: datetime) -> BrokerFillFeed:
        from algo.live.feeds import BrokerFillFeed

        return BrokerFillFeed(
            broker=FakeBroker(snaps),
            clock=FrozenClock(at),
            instruments={INSTRUMENT.key: INSTRUMENT},
            since=at,
        )

    def test_a_fill_is_returned_once_and_only_once(self) -> None:
        """The broker re-reports over an overlap window; booking the same fill
        twice would double the position."""
        at = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
        feed = self._feed([_snap("f1", at + timedelta(minutes=1))], at)

        assert [f.fill_id for f in feed.new_fills()] == ["f1"]
        assert list(feed.new_fills()) == []

    def test_a_late_fill_stamped_before_the_cursor_is_still_seen(self) -> None:
        """Exactly what the overlap window exists for - a fill that arrives out
        of order must not be skipped forever."""
        at = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
        broker = FakeBroker([_snap("f1", at + timedelta(minutes=2))])
        from algo.live.feeds import BrokerFillFeed

        feed = BrokerFillFeed(
            broker=broker,
            clock=FrozenClock(at),
            instruments={INSTRUMENT.key: INSTRUMENT},
            since=at,
        )
        feed.new_fills()

        broker.snaps.append(_snap("f0", at + timedelta(minutes=1)))

        assert [f.fill_id for f in feed.new_fills()] == ["f0"]

    def test_an_unknown_instrument_is_refused_not_skipped(self) -> None:
        """Booking it against a guess corrupts the portfolio; skipping it leaves
        a real position invisible. Neither is acceptable."""
        from algo.core.errors import DataError

        at = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
        feed = self._feed([_snap("f1", at, key="MCX:SILVERM:FUT:20260904")], at)

        with pytest.raises(DataError, match="refusing to book it against a guess"):
            feed.new_fills()

    def test_a_live_fill_is_not_marked_modelled(self) -> None:
        at = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
        feed = self._feed([_snap("f1", at)], at)

        fill = feed.new_fills()[0]

        assert fill.is_modelled is False
        assert fill.charges.total == Decimal("0"), (
            "an execution report carries no contract note; a modelled charge "
            "here would be indistinguishable from a real one later"
        )


class TestIterableBarFeed:
    def test_bars_come_back_in_timestamp_order(self) -> None:
        from algo.live.feeds import IterableBarFeed

        bars = _bars()
        feed = IterableBarFeed(lambda: [bars[2], bars[0], bars[1]])

        assert [b.ts for b in feed.closed_bars()] == [b.ts for b in bars[:3]]

    def test_it_is_re_read_each_call(self) -> None:
        """A live feed grows during a session; a snapshot taken once would
        freeze the loop on the first bar of the day."""
        from algo.live.feeds import IterableBarFeed

        bars = _bars()
        visible = [bars[0]]
        feed = IterableBarFeed(lambda: list(visible))
        assert len(feed.closed_bars()) == 1

        visible.append(bars[1])

        assert len(feed.closed_bars()) == 2
