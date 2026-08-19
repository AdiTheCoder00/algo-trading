"""The strangle strategy, tested against chains that behave like the real one.

The fixtures are generated, but they are generated with the live chain's shape:
futures at 1,56,640, nine days to the 28 Aug expiry, 21.75% at the money, a rising
call skew, and — in the tests that matter most — **strikes that are listed but not
quoted**. A chain fixture where everything quotes would be testing a market that
does not exist, and would let the strategy pass while being unable to trade.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from algo.core.bar import M30, Bar, BarWindow
from algo.core.chain import OptionChainSnapshot
from algo.core.enums import Atomicity, Exchange, Right, Side, SignalAction
from algo.core.instrument import FutureId, InstrumentSpec, OptionId
from algo.core.position import Position
from algo.core.signal import Signal
from algo.core.timeutil import utc
from algo.data.synthetic_chain import build_chain
from algo.exchange.calendar import MarketCalendar
from algo.exchange.expiries import (
    ExpiryCalendar,
    ExpirySet,
    InstrumentMasterExpiries,
    LastFridayRule,
)
from algo.exchange.specs import ContractSpecStore
from algo.pricing.chain_greeks import enrich
from algo.strategy.context import BarContext, PositionView, build_session_info
from algo.strategy.delta_strangle import DeltaStrangle

FUTURES = Decimal("156640")
R = 0.065
EXPIRY = date(2026, 8, 28)
EXPIRES_AT = utc(2026, 8, 28, 18, 0)
GOLDM_FUT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))

CYCLE = ExpirySet(
    option_expiry=EXPIRY,
    futures_expiry=date(2026, 9, 4),
    tender_period_start=date(2026, 9, 1),
)

SPEC = InstrumentSpec(
    underlying="GOLDM",
    exchange=Exchange.MCX,
    lot_size=Decimal("100"),
    multiplier=Decimal("10"),
    tick_size=Decimal("0.50"),
    strike_interval=Decimal("500"),
    min_lots=1,
    effective_from=date(2026, 1, 1),
    source="strangle test fixture",
)


class StaticChain:
    """A chain provider that returns the same enriched snapshot at any instant."""

    def __init__(self, snapshot: OptionChainSnapshot) -> None:
        self._snapshot = snapshot

    def chain_at(
        self, underlying: str, option_expiry: date, ts: object
    ) -> OptionChainSnapshot | None:
        del ts
        if underlying != self._snapshot.underlying or option_expiry != self._snapshot.option_expiry:
            return None
        return self._snapshot


def _chain(
    *, ts: object, quote_gaps: frozenset[Decimal] = frozenset(), skew: float = 0.00012
) -> OptionChainSnapshot:
    raw = build_chain(
        ts=ts,  # type: ignore[arg-type]
        underlying_future=GOLDM_FUT,
        option_expiry=EXPIRY,
        futures_price=FUTURES,
        expires_at=EXPIRES_AT,
        vol=0.2175,
        r=R,
        skew_per_strike=skew,
        quote_gaps=quote_gaps,
    )
    return enrich(raw, expires_at=EXPIRES_AT, r=R)


def _context(
    calendar: MarketCalendar,
    *,
    at: object,
    chain: OptionChainSnapshot,
    positions: dict[str, Position] | None = None,
) -> BarContext:
    bar = Bar(
        ts=at,  # type: ignore[arg-type]
        timeframe=M30,
        open=FUTURES,
        high=FUTURES,
        low=FUTURES,
        close=FUTURES,
        volume=10,
    )
    session_day = date(2026, 8, 19)
    master = InstrumentMasterExpiries({("GOLDM", 2026, 8): CYCLE})
    return BarContext(
        window=BarWindow.of([bar]),
        session=build_session_info(
            bar=bar,
            session_close=calendar.session_close(session_day),
            is_us_dst=True,
            bar_index=1,
            bars_in_session=29,
        ),
        specs=ContractSpecStore([SPEC]),
        positions=PositionView(positions or {}),
        timeframe=M30,
        chain_provider=StaticChain(chain),
        expiries=ExpiryCalendar(authority=master, rule=LastFridayRule(calendar)),
    )


def _strategy(**kwargs: object) -> DeltaStrangle:
    defaults: dict[str, object] = {"underlying": "GOLDM", "min_dte": 5, "max_dte": 45}
    defaults.update(kwargs)
    return DeltaStrangle(**defaults)  # type: ignore[arg-type]


#: 09:30 IST on 19 Aug 2026 — the configured entry bar, four hours into UTC.
ENTRY_BAR = utc(2026, 8, 19, 4, 0)


class TestEntry:
    def test_it_sells_a_call_and_a_put_at_the_entry_bar(
        self, calendar: MarketCalendar
    ) -> None:
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        signals = _strategy().on_bar(ctx)

        assert len(signals) == 1
        signal = signals[0]
        assert signal.action is SignalAction.OPEN
        assert len(signal.legs) == 2
        assert {leg.direction for leg in signal.legs} == {Side.SELL}

    def test_both_legs_are_within_the_delta_tolerance(
        self, calendar: MarketCalendar
    ) -> None:
        chain = _chain(ts=ENTRY_BAR)
        ctx = _context(calendar, at=ENTRY_BAR, chain=chain)
        signal = _strategy().on_bar(ctx)[0]

        for leg in signal.legs:
            option = leg.instrument
            assert isinstance(option, OptionId)
            row = chain.by_strike(option.strike, option.right)
            assert row is not None and row.delta is not None
            assert abs(abs(row.delta) - 0.25) <= 0.05

    def test_the_call_is_above_and_the_put_below_the_futures(
        self, calendar: MarketCalendar
    ) -> None:
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        signal = _strategy().on_bar(ctx)[0]
        strikes = {
            leg.instrument.right: leg.instrument.strike  # type: ignore[union-attr]
            for leg in signal.legs
        }
        assert strikes[Right.CE] > FUTURES
        assert strikes[Right.PE] < FUTURES

    def test_the_legs_are_atomic(self, calendar: MarketCalendar) -> None:
        """A call that fills while the put rejects is a naked short call — a
        completely different instrument of risk (D-008)."""
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        assert _strategy().on_bar(ctx)[0].atomicity is Atomicity.ALL_OR_NONE

    def test_the_combo_exits_travel_with_the_signal(
        self, calendar: MarketCalendar
    ) -> None:
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        signal = _strategy().on_bar(ctx)[0]
        assert signal.combo_take_profit is not None
        assert signal.combo_stop is not None
        assert signal.combo_take_profit.kind == "PCT_OF_MARGIN_AT_ENTRY"
        assert signal.combo_take_profit.value == Decimal("2")
        assert signal.combo_stop.value == Decimal("1")

    def test_the_reason_answers_the_six_weeks_later_question(
        self, calendar: MarketCalendar
    ) -> None:
        """Brief §5: the reason is mandatory and gets logged."""
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        signal = _strategy().on_bar(ctx)[0]
        assert "short strangle" in signal.reason
        assert "2026-08-28" in signal.reason
        assert "delta" in signal.reason
        for key in ("expiry", "dte", "futures", "call_strike", "put_strike", "call_iv"):
            assert key in signal.context

    def test_it_carries_no_lot_size(self, calendar: MarketCalendar) -> None:
        """A strategy that computes lot size is a bug (brief §5)."""
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        signal = _strategy().on_bar(ctx)[0]
        assert not hasattr(signal.legs[0], "lots")
        assert not hasattr(signal.legs[0], "qty")


class TestWhenItDoesNotTrade:
    def test_not_at_other_bars(self, calendar: MarketCalendar) -> None:
        other = utc(2026, 8, 19, 6, 0)  # 11:30 IST
        ctx = _context(calendar, at=other, chain=_chain(ts=other))
        assert _strategy().on_bar(ctx) == []

    def test_not_when_a_position_is_already_open(self, calendar: MarketCalendar) -> None:
        chain = _chain(ts=ENTRY_BAR)
        held = OptionId(
            underlying_future=GOLDM_FUT,
            option_expiry=EXPIRY,
            strike=Decimal("160500"),
            right=Right.CE,
        )
        ctx = _context(
            calendar,
            at=ENTRY_BAR,
            chain=chain,
            positions={
                held.key: Position(
                    instrument=held, qty=Decimal("-1"), lots=-1, multiplier=Decimal("10")
                )
            },
        )
        assert _strategy().on_bar(ctx) == []

    def test_not_outside_the_dte_band_and_it_says_so(
        self, calendar: MarketCalendar
    ) -> None:
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        strategy = _strategy(min_dte=20, max_dte=45)
        assert strategy.on_bar(ctx) == []
        notes = strategy.drain_notes()
        assert notes
        assert "outside the [20, 45] band" in notes[0]

    def test_not_on_a_partial_bar(self, calendar: MarketCalendar) -> None:
        """The 23:30-23:55 stub. The risk layer may act on it; the strategy may not."""
        stub = utc(2026, 8, 19, 4, 0)
        bar = Bar(
            ts=stub,
            timeframe=M30,
            open=FUTURES,
            high=FUTURES,
            low=FUTURES,
            close=FUTURES,
            volume=1,
            is_partial=True,
        )
        ctx = BarContext(
            window=BarWindow.of([bar]),
            session=build_session_info(
                bar=bar,
                session_close=calendar.session_close(date(2026, 8, 19)),
                is_us_dst=True,
                bar_index=29,
                bars_in_session=30,
            ),
            specs=ContractSpecStore([SPEC]),
            positions=PositionView({}),
            timeframe=M30,
            chain_provider=StaticChain(_chain(ts=stub)),
            expiries=None,
        )
        assert _strategy().on_bar(ctx) == []


class TestThinBooks:
    """The case the live chain actually presented."""

    def test_it_refuses_to_trade_when_the_target_strikes_are_not_quoted(
        self, calendar: MarketCalendar
    ) -> None:
        """Silently falling back to the nearest quoted strike would report fills
        at 0.34 delta while claiming to trade 0.25."""
        gaps = frozenset(
            Decimal(str(k)) for k in range(159500, 163001, 500)
        ) | frozenset(Decimal(str(k)) for k in range(150000, 154001, 500))
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR, quote_gaps=gaps))
        strategy = _strategy()
        assert strategy.on_bar(ctx) == []

    def test_the_refusal_explains_which_side_failed_and_what_was_available(
        self, calendar: MarketCalendar
    ) -> None:
        """"No entry" alone hides the difference between an unlisted strike and a
        listed one nobody is quoting."""
        gaps = frozenset(Decimal(str(k)) for k in range(159500, 163001, 500))
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR, quote_gaps=gaps))
        strategy = _strategy()
        strategy.on_bar(ctx)
        note = strategy.drain_notes()[0]
        assert "call at 0.25" in note
        assert "put" not in note.split("—")[0], "only the call side should be reported missing"
        assert "closest tradeable" in note
        assert "rows were tradeable" in note

    def test_a_narrower_tolerance_can_make_the_trade_impossible(
        self, calendar: MarketCalendar
    ) -> None:
        ctx = _context(calendar, at=ENTRY_BAR, chain=_chain(ts=ENTRY_BAR))
        assert _strategy(delta_tolerance=Decimal("0.001")).on_bar(ctx) == []

    def test_it_never_selects_an_unquoted_row(self, calendar: MarketCalendar) -> None:
        chain = _chain(ts=ENTRY_BAR, quote_gaps=frozenset({Decimal("160500")}))
        ctx = _context(calendar, at=ENTRY_BAR, chain=chain)
        signals = _strategy().on_bar(ctx)
        if signals:
            for leg in signals[0].legs:
                option = leg.instrument
                assert isinstance(option, OptionId)
                row = chain.by_strike(option.strike, option.right)
                assert row is not None and row.is_tradeable


class TestDeterminism:
    def test_the_same_chain_produces_the_same_signal_id(
        self, calendar: MarketCalendar
    ) -> None:
        chain = _chain(ts=ENTRY_BAR)
        first = _strategy().on_bar(_context(calendar, at=ENTRY_BAR, chain=chain))[0]
        second = _strategy().on_bar(_context(calendar, at=ENTRY_BAR, chain=chain))[0]
        assert first.signal_id == second.signal_id

    def test_changing_a_parameter_changes_the_signal_id(
        self, calendar: MarketCalendar
    ) -> None:
        chain = _chain(ts=ENTRY_BAR)
        base = _strategy().on_bar(_context(calendar, at=ENTRY_BAR, chain=chain))[0]
        tweaked = _strategy(target_delta=Decimal("0.30")).on_bar(
            _context(calendar, at=ENTRY_BAR, chain=chain)
        )[0]
        assert base.signal_id != tweaked.signal_id

    def test_a_higher_target_delta_sells_closer_to_the_money(
        self, calendar: MarketCalendar
    ) -> None:
        chain = _chain(ts=ENTRY_BAR)
        quarter = _strategy().on_bar(_context(calendar, at=ENTRY_BAR, chain=chain))[0]
        third = _strategy(target_delta=Decimal("0.35")).on_bar(
            _context(calendar, at=ENTRY_BAR, chain=chain)
        )[0]

        def call_strike(signal: Signal) -> Decimal:
            for leg in signal.legs:
                option = leg.instrument
                if isinstance(option, OptionId) and option.right is Right.CE:
                    return option.strike
            raise AssertionError("no call leg")

        assert call_strike(third) < call_strike(quarter)


class TestEntryTimeConfiguration:
    def test_a_different_entry_time_is_honoured(self, calendar: MarketCalendar) -> None:
        noon = utc(2026, 8, 19, 6, 30)  # 12:00 IST
        ctx = _context(calendar, at=noon, chain=_chain(ts=noon))
        assert _strategy(entry_times_ist=(time(12, 0),)).on_bar(ctx)

    def test_multiple_entry_times(self, calendar: MarketCalendar) -> None:
        strategy = _strategy(entry_times_ist=(time(9, 30), time(12, 0)))
        for moment in (ENTRY_BAR, utc(2026, 8, 19, 6, 30)):
            ctx = _context(calendar, at=moment, chain=_chain(ts=moment))
            assert strategy.on_bar(ctx)
