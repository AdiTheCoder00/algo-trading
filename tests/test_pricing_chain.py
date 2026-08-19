"""Chain pricing, run against the real GOLDM chain rather than a fixture I invented.

Every price below was read off the live Angel One option chain for the 28 Aug 2026
expiry on 19 Aug 2026, underlying shown as 1,56,640. Testing against real quotes
catches things a synthetic fixture cannot — and did: the forward check in this
file exists because inverting this chain revealed the displayed underlying was
about thirty points away from what the options were actually priced off.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Right
from algo.core.instrument import FutureId, OptionId
from algo.core.quote import Quote
from algo.core.timeutil import utc
from algo.pricing.chain_greeks import (
    atm_iv,
    enrich,
    theoretical_delta_strike,
    time_to_expiry,
)
from algo.pricing.forward import forward_from_parity, implied_forward

CAPTURED_AT = utc(2026, 8, 19, 18, 0)  # 23:30 IST on 19 Aug
EXPIRES_AT = utc(2026, 8, 28, 18, 0)  # session close on 28 Aug — exactly 9 days
STATED_FUTURES = Decimal("156640")
R = 0.065

#: (strike, call LTP, put LTP). `None` where the chain showed no quote — those
#: gaps are real and are part of what makes this a useful fixture.
OBSERVED: tuple[tuple[str, str | None, str | None], ...] = (
    ("155000", "3017.50", "1405.00"),
    ("155500", "2731.00", "1615.50"),
    ("156000", "2461.00", "1851.00"),
    ("156500", "2200.50", None),
    ("157000", "1975.00", "2370.50"),
    ("157500", "1761.50", None),
    ("158000", "1565.50", "2956.00"),
    ("159000", "1239.50", None),
)

FUTURE = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))


def _option(strike: str, right: Right) -> OptionId:
    return OptionId(
        underlying_future=FUTURE,
        option_expiry=date(2026, 8, 28),
        strike=Decimal(strike),
        right=right,
    )


def _quote(ltp: str | None, *, two_sided: bool) -> Quote:
    if ltp is None:
        return Quote(exchange_ts=CAPTURED_AT, received_ts=CAPTURED_AT)
    last = Decimal(ltp)
    if not two_sided:
        return Quote(exchange_ts=CAPTURED_AT, received_ts=CAPTURED_AT, ltp=last)
    half = Decimal("5.00")  # ten ticks either side — a plausible thin-book spread
    return Quote(
        exchange_ts=CAPTURED_AT,
        received_ts=CAPTURED_AT,
        bid=last - half,
        ask=last + half,
        bid_qty=5,
        ask_qty=5,
        ltp=last,
        volume=100,
    )


def _snapshot(*, two_sided: bool) -> OptionChainSnapshot:
    rows: list[ChainRow] = []
    for strike, call, put in OBSERVED:
        for right, ltp in ((Right.CE, call), (Right.PE, put)):
            rows.append(
                ChainRow(
                    option=_option(strike, right),
                    quote=_quote(ltp, two_sided=two_sided),
                )
            )
    return OptionChainSnapshot(
        ts=CAPTURED_AT,
        underlying="GOLDM",
        option_expiry=date(2026, 8, 28),
        futures_price=STATED_FUTURES,
        rows=tuple(rows),
    )


@pytest.fixture
def ltp_chain() -> OptionChainSnapshot:
    """As the terminal actually showed it: last traded prices, no book."""
    return _snapshot(two_sided=False)


@pytest.fixture
def quoted_chain() -> OptionChainSnapshot:
    """The same chain with a plausible two-sided book, for selection tests."""
    return _snapshot(two_sided=True)


class TestTimeToExpiry:
    def test_nine_days(self) -> None:
        assert time_to_expiry(CAPTURED_AT, EXPIRES_AT) == pytest.approx(9 / 365, abs=1e-12)

    def test_never_negative_after_expiry(self) -> None:
        assert time_to_expiry(EXPIRES_AT, CAPTURED_AT) == 0.0

    def test_an_option_still_has_time_value_on_expiry_morning(self) -> None:
        """A date-only calculation would price the whole final session at zero."""
        morning = utc(2026, 8, 28, 4, 0)
        assert time_to_expiry(morning, EXPIRES_AT) > 0


class TestEnrichment:
    def test_every_quoted_row_gets_a_volatility_and_a_delta(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        quoted = [r for r in priced.rows if r.quote.ltp is not None]
        assert len(quoted) == 13
        assert all(r.iv is not None and r.delta is not None for r in quoted)

    def test_unquoted_rows_stay_unpriced(self, ltp_chain: OptionChainSnapshot) -> None:
        """No borrowing the neighbouring strike's volatility to fill a gap."""
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        blanks = [r for r in priced.rows if r.quote.ltp is None]
        assert len(blanks) == 3
        assert all(r.iv is None and r.delta is None and r.priced_from == "" for r in blanks)
        assert all(not r.is_tradeable for r in blanks)

    def test_the_price_source_is_recorded(self, quoted_chain: OptionChainSnapshot) -> None:
        priced = enrich(quoted_chain, expires_at=EXPIRES_AT, r=R)
        sources = {r.priced_from for r in priced.rows if r.iv is not None}
        assert sources == {"MID"}

    def test_ltp_is_used_only_when_the_book_is_one_sided(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        assert {r.priced_from for r in priced.rows if r.iv is not None} == {"LTP"}

    def test_atm_volatility(self, ltp_chain: OptionChainSnapshot) -> None:
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        vol = atm_iv(priced)
        assert vol is not None
        assert vol == pytest.approx(0.2175, abs=1e-3)

    def test_deltas_decline_as_calls_move_out_of_the_money(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        calls = [r for r in priced.rows if r.right is Right.CE and r.delta is not None]
        deltas = [
            r.delta for r in sorted(calls, key=lambda r: r.strike) if r.delta is not None
        ]
        assert deltas == sorted(deltas, reverse=True)

    def test_the_furthest_quoted_call_is_not_yet_a_quarter_delta(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        """159000 CE prices near 0.34 delta — the strategy's target is further out
        than anything the visible chain quotes.

        Priced off its own solved volatility (22.51%), not a flat ATM vol —
        which is why this is 0.342 rather than the 0.336 a flat 21.75% gives.
        The chain is the market's opinion, so the per-strike vol is the one
        that decides which strike gets sold.
        """
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        row = priced.by_strike(Decimal("159000"), Right.CE)
        assert row is not None and row.delta is not None
        assert row.delta == pytest.approx(0.342, abs=0.005)
        assert row.delta > 0.25


class TestStrikeSelection:
    def test_the_target_strikes_are_off_the_quoted_ladder(
        self, quoted_chain: OptionChainSnapshot
    ) -> None:
        """The finding that matters for whether this strategy is executable."""
        priced = enrich(quoted_chain, expires_at=EXPIRES_AT, r=R)
        call_k = theoretical_delta_strike(
            priced,
            expires_at=EXPIRES_AT,
            r=R,
            target_delta=0.25,
            right=Right.CE,
            strike_interval=Decimal("500"),
        )
        put_k = theoretical_delta_strike(
            priced,
            expires_at=EXPIRES_AT,
            r=R,
            target_delta=0.25,
            right=Right.PE,
            strike_interval=Decimal("500"),
        )
        assert call_k == Decimal("160500")
        assert put_k == Decimal("153000")

        listed = priced.strikes()
        assert call_k > max(listed), "the 0.25 delta call is above every listed strike"
        assert put_k < min(listed), "the 0.25 delta put is below every listed strike"

    def test_nearest_delta_refuses_rows_with_no_book(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        """LTP alone is not a tradeable quote — selection must find nothing."""
        priced = enrich(ltp_chain, expires_at=EXPIRES_AT, r=R)
        assert priced.nearest_delta(0.35, Right.CE, tolerance=0.10) is None

    def test_nearest_delta_selects_from_a_two_sided_book(
        self, quoted_chain: OptionChainSnapshot
    ) -> None:
        priced = enrich(quoted_chain, expires_at=EXPIRES_AT, r=R)
        chosen = priced.nearest_delta(0.35, Right.CE, tolerance=0.05)
        assert chosen is not None
        assert chosen.strike == Decimal("159000")

    def test_a_target_no_strike_can_reach_returns_nothing(
        self, quoted_chain: OptionChainSnapshot
    ) -> None:
        priced = enrich(quoted_chain, expires_at=EXPIRES_AT, r=R)
        assert priced.nearest_delta(0.25, Right.CE, tolerance=0.02) is None


class TestForwardConsistency:
    """The check that came out of pricing the real chain.

    Put volatilities came out consistently above call volatilities at every
    strike — a one-sided error, which is the signature of a wrong forward rather
    than of noise.
    """

    def test_parity_forward_is_algebraically_correct(self) -> None:
        implied = forward_from_parity(
            call_price=1975.00, put_price=2370.50, strike=157000.0, t=9 / 365, r=R
        )
        assert implied == pytest.approx(156603.9, abs=0.5)

    def test_the_chain_implies_a_lower_forward_than_the_terminal_displayed(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        check = implied_forward(ltp_chain, t=9 / 365, r=R)
        assert check.pairs_used == 5
        assert check.implied is not None
        assert float(check.implied) == pytest.approx(156611.0, abs=2.0)
        assert check.gap is not None
        assert float(check.gap) == pytest.approx(29.0, abs=2.0)

    def test_the_estimates_agree_with_each_other(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        """Tight clustering is what makes this a signal rather than one bad print."""
        check = implied_forward(ltp_chain, t=9 / 365, r=R)
        assert check.spread_of_estimates is not None
        assert float(check.spread_of_estimates) < 20.0

    def test_the_gap_trips_a_tight_tolerance_and_clears_a_loose_one(
        self, ltp_chain: OptionChainSnapshot
    ) -> None:
        check = implied_forward(ltp_chain, t=9 / 365, r=R)
        assert not check.is_consistent(tolerance_pct=Decimal("0.01"))
        assert check.is_consistent(tolerance_pct=Decimal("0.05"))

    def test_no_two_sided_strike_means_nothing_to_contradict(self) -> None:
        """Absence of evidence must not halt the engine on a thin day."""
        rows = tuple(
            ChainRow(option=_option("157000", Right.CE), quote=_quote("1975.00", two_sided=False))
            for _ in range(1)
        )
        snapshot = OptionChainSnapshot(
            ts=CAPTURED_AT,
            underlying="GOLDM",
            option_expiry=date(2026, 8, 28),
            futures_price=STATED_FUTURES,
            rows=rows,
        )
        check = implied_forward(snapshot, t=9 / 365, r=R)
        assert check.pairs_used == 0
        assert check.implied is None
        assert check.is_consistent(tolerance_pct=Decimal("0.001"))
