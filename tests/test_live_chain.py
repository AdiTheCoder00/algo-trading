"""The live chain provider (D-112).

Two properties carry real money. **Enrichment**: a raw market-data poll returns
`delta=None` on every row, and `DeltaStrangle` selects strikes *by delta* - so an
unenriched chain makes the strategy silently never trade. **Staleness**: pricing
a short option against a quote the feed stopped updating is the same error
`prices.py` refuses for a missing mark, with a smaller number on it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from algo.core.enums import Exchange, Right
from algo.core.errors import DataError
from algo.core.instrument import FutureId
from algo.core.timeutil import to_ist
from algo.data.synthetic_chain import build_chain
from algo.live.chain import LiveChainProvider

FUTURE = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4), exchange=Exchange.MCX)
EXPIRY = date(2026, 8, 28)
TS = datetime(2026, 8, 19, 4, 30, tzinfo=UTC)


class StubFeed:
    """Returns a chain with no greeks, exactly as a real market-data poll does."""

    def __init__(self, ts: datetime = TS) -> None:
        self.ts = ts
        self.polls = 0

    def poll(self, option_expiry: date):
        self.polls += 1
        chain = build_chain(
            ts=self.ts,
            underlying_future=FUTURE,
            option_expiry=option_expiry,
            futures_price=Decimal("156640"),
            expires_at=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
            vol=0.2175,
            strikes_each_side=10,
            strike_centre=Decimal("156640"),
            populate_greeks=False,
        )
        return chain


def _provider(feed: StubFeed, **kwargs: object) -> LiveChainProvider:
    kwargs.setdefault("max_staleness_s", 120.0)
    return LiveChainProvider(feed=feed, **kwargs)  # type: ignore[arg-type]


class TestItEnriches:
    def test_the_raw_poll_really_has_no_greeks(self) -> None:
        """Guards the premise. If the fixture ever gained deltas the test below
        would pass without proving anything."""
        raw = StubFeed().poll(EXPIRY)

        assert all(row.delta is None for row in raw.rows)

    def test_the_provider_supplies_them(self) -> None:
        provider = _provider(StubFeed())

        chain = provider.refresh(EXPIRY)

        assert any(row.delta is not None for row in chain.rows)

    def test_a_delta_target_can_now_be_matched(self) -> None:
        """The point of the whole module: without this the strategy finds no
        strike and never trades, silently."""
        provider = _provider(StubFeed())
        chain = provider.refresh(EXPIRY)

        assert chain.nearest_delta(0.25, Right.CE, tolerance=0.10) is not None
        assert chain.nearest_delta(0.25, Right.PE, tolerance=0.10) is not None


class TestOnePollAnswersEveryQuestion:
    def test_chain_and_marks_come_from_the_same_poll(self) -> None:
        """A strike chosen at one instant and marked at another is a real risk;
        refreshing per question would allow it."""
        feed = StubFeed()
        provider = _provider(feed)
        provider.refresh(EXPIRY)
        polls_after_refresh = feed.polls

        chain = provider.chain_at("GOLDM", EXPIRY, TS)
        assert chain is not None
        key = chain.rows[0].option.key
        provider.mark(key, TS)
        provider.fill_reference(key, TS)

        assert feed.polls == polls_after_refresh, "asking must not poll"

    def test_marks_are_the_mid_not_the_last_trade(self) -> None:
        provider = _provider(StubFeed())
        chain = provider.refresh(EXPIRY)
        row = next(r for r in chain.rows if r.quote.mid is not None)

        assert provider.mark(row.option.key, TS) == row.quote.mid


class TestStalenessIsRefused:
    def test_a_fresh_snapshot_prices(self) -> None:
        provider = _provider(StubFeed(), max_staleness_s=120.0)
        chain = provider.refresh(EXPIRY)
        key = chain.rows[0].option.key

        assert provider.mark(key, TS + timedelta(seconds=60)) is not None

    def test_an_old_snapshot_does_not(self) -> None:
        provider = _provider(StubFeed(), max_staleness_s=120.0)
        chain = provider.refresh(EXPIRY)
        key = chain.rows[0].option.key

        assert provider.mark(key, TS + timedelta(seconds=300)) is None

    def test_the_chain_itself_goes_away_too(self) -> None:
        """Not just marks. A stale ladder would have the strategy select strikes
        from prices nobody is showing any more."""
        provider = _provider(StubFeed(), max_staleness_s=120.0)
        provider.refresh(EXPIRY)

        assert provider.chain_at("GOLDM", EXPIRY, TS + timedelta(seconds=300)) is None

    def test_require_fresh_says_why(self) -> None:
        provider = _provider(StubFeed(), max_staleness_s=120.0)
        provider.refresh(EXPIRY)

        with pytest.raises(DataError, match="past the 120s limit"):
            provider.require_fresh(TS + timedelta(seconds=300))

    def test_require_fresh_before_any_poll_says_that_instead(self) -> None:
        provider = _provider(StubFeed())

        with pytest.raises(DataError, match="no chain has been polled yet"):
            provider.require_fresh(TS)

    def test_nothing_prices_before_the_first_poll(self) -> None:
        provider = _provider(StubFeed())

        assert provider.mark("anything", TS) is None
        assert provider.chain_at("GOLDM", EXPIRY, TS) is None


class TestItWillNotAnswerForTheWrongCycle:
    def test_a_different_expiry_gets_none_not_the_polled_one(self) -> None:
        """Handing back the wrong ladder would have the strategy select strikes
        in an expiry it did not ask about."""
        provider = _provider(StubFeed())
        provider.refresh(EXPIRY)

        assert provider.chain_at("GOLDM", date(2026, 9, 25), TS) is None

    def test_a_different_underlying_gets_none(self) -> None:
        provider = _provider(StubFeed())
        provider.refresh(EXPIRY)

        assert provider.chain_at("SILVERM", EXPIRY, TS) is None


class TestTheLoopTradesTheStrangleEndToEnd:
    """The whole point of connecting the chain: `LiveLoop` -> `decide` ->
    `DeltaStrangle` selecting real strikes by real delta, and orders coming out.

    Everything here is the production path except the market-data transport.
    """

    def _rig(self):
        from algo.backtest.engine import BacktestEngine
        from algo.backtest.prices import BarPriceSource, CompositePriceSource
        from algo.core.bar import Timeframe
        from algo.costs.charges import McxChargeModel
        from algo.costs.slippage import TickSlippage
        from algo.costs.spread import FixedTickSpread
        from algo.data.resample import resample
        from algo.data.synthetic import one_minute_session
        from algo.exchange.calendar import synthetic_calendar
        from algo.exchange.expiries import (
            ExpiryCalendar,
            ExpirySet,
            InstrumentMasterExpiries,
        )
        from algo.exchange.specs import ContractSpecStore
        from algo.execution.fills import FillSimulator
        from algo.live.loop import LiveLoop
        from algo.portfolio.book import Portfolio
        from algo.risk.engine import FixedLotSizer, RiskEngine
        from algo.strategy.delta_strangle import DeltaStrangle
        from tests.test_live_loop import (
            FrozenClock,
            RecordingPlacer,
            ScriptedBars,
            ScriptedFills,
        )

        calendar = synthetic_calendar()
        bars = resample(
            one_minute_session(calendar, date(2026, 8, 19), seed=20260819),
            calendar=calendar,
            timeframe=Timeframe(minutes=30),
        )
        feed = StubFeed(ts=bars[1].ts)
        provider = _provider(feed, max_staleness_s=3600.0)
        master = InstrumentMasterExpiries(
            {("GOLDM", 2026, 8): ExpirySet(option_expiry=EXPIRY, futures_expiry=FUTURE.expiry)}
        )
        engine = BacktestEngine(
            bars=bars[:1],
            calendar=calendar,
            specs=ContractSpecStore.default(),
            # Entry time aligned to the bar this rig feeds. The 09:30 rule has
            # its own tests; what is under test here is that a connected chain
            # lets the strategy find strikes at all.
            strategy=DeltaStrangle(
                underlying="GOLDM",
                min_dte=3,
                max_dte=45,
                entry_times_ist=(to_ist(bars[1].ts).time(),),
            ),
            risk=RiskEngine(
                sizer=FixedLotSizer(1),
                spec_for=None,
                max_concurrent_positions=2,
                max_lots_per_underlying=10,
            ),
            simulator=FillSimulator(
                spread=FixedTickSpread(2),
                slippage=TickSlippage(market_ticks=0, stop_ticks=2),
                charges=McxChargeModel.default(),
            ),
            portfolio=Portfolio(Decimal("1000000")),
            instrument=FUTURE,
            timeframe=Timeframe(minutes=30),
            is_option=True,
            price_source=CompositePriceSource(provider, BarPriceSource(FUTURE, bars[:1])),
            chain_provider=provider,
            expiries=ExpiryCalendar(authority=master, rule=None),
        )
        bar_feed, fill_feed, placer = ScriptedBars(), ScriptedFills(), RecordingPlacer()
        loop = LiveLoop(
            engine=engine,
            bars=bar_feed,
            fills=fill_feed,
            place=placer,
            clock=FrozenClock(bars[1].ts),
            chain=lambda _bar: provider.refresh(EXPIRY),
        )
        return loop, bar_feed, placer, bars, feed

    def test_the_loop_emits_a_two_legged_short_strangle(self) -> None:
        loop, bar_feed, placer, bars, _feed = self._rig()
        bar_feed.visible = bars[:2]

        result = loop.pass_once()

        assert result.decision is not None, result.summary()
        assert placer.sent == 2, f"a strangle is two legs, got {placer.sent}"
        sent = placer.calls[0]
        assert {o.instrument.right.value for o in sent} == {"CE", "PE"}
        assert all(o.side.value == "SELL" for o in sent)

    def test_the_chain_is_polled_once_per_bar(self) -> None:
        loop, bar_feed, _placer, bars, feed = self._rig()
        bar_feed.visible = bars[:2]

        loop.pass_once()
        after_one = feed.polls
        loop.pass_once()  # same bar - must not act, must not poll

        assert after_one == 1
        assert feed.polls == 1

    def test_a_chain_failure_stops_the_pass_and_says_so(self) -> None:
        """A quiet no-trade and a broken feed must not look the same."""
        loop, bar_feed, placer, bars, _feed = self._rig()
        bar_feed.visible = bars[:2]

        def boom(_bar):
            raise DataError("market data poll failed")

        loop._chain = boom

        result = loop.pass_once()

        assert result.decision is None
        assert "chain unavailable" in result.summary()
        assert placer.sent == 0
