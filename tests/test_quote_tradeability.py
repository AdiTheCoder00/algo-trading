"""The tradeability gate, and specifically the two checks Q17 added.

These exist because the whole suite passed both before and after the spread
gate was introduced — which meant nothing covered it. A gate nothing tests is a
gate that can be removed by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algo.core.enums import QuoteFlag
from algo.core.quote import DEFAULT_MAX_SPREAD_PCT, Quote

TS = datetime(2026, 8, 26, 14, 50, tzinfo=UTC)


def _quote(bid: str | None, ask: str | None, **kwargs: object) -> Quote:
    return Quote(
        exchange_ts=TS,
        received_ts=TS,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        **kwargs,  # type: ignore[arg-type]
    )


class TestSpreadWidth:
    def test_the_row_from_q17_is_refused(self) -> None:
        """The exact book that motivated the gate, from a real GOLDM scrape.

        bid 76.5 / ask 884.5 is uncrossed, positive and fresh, so every other
        check in `status` passes it. Its mid of 480.5 inverted to a delta of
        0.150 — the strategy's own selling target — while the real neighbouring
        strikes sat at 0.063 and 0.045.
        """
        quote = _quote("76.5", "884.5", volume=0)

        assert quote.status() is QuoteFlag.TOO_WIDE
        assert not quote.is_tradeable

    def test_the_worst_observed_book_is_refused(self) -> None:
        assert _quote("1", "1833").status() is QuoteFlag.TOO_WIDE

    def test_a_real_near_the_money_book_still_passes(self) -> None:
        """The gate has to leave genuine books alone. This is a real 160000 CE:
        a 6.5-point spread on a 1784 mid, about 0.36%."""
        quote = _quote("1781.5", "1788", volume=38756, open_interest=4725)

        assert quote.status() is QuoteFlag.OK
        assert quote.is_tradeable

    def test_a_cheap_wing_option_with_a_tight_book_still_passes(self) -> None:
        """Relative, not absolute: half a point on a 76-rupee option is tight,
        and an absolute threshold tuned for the money would have killed it."""
        assert _quote("76", "77.5", volume=32546, open_interest=4675).is_tradeable

    def test_the_boundary_is_inclusive(self) -> None:
        """Exactly at the threshold is allowed; past it is not. Pinned so the
        comparison cannot silently flip to >= later."""
        at = _quote("95", "105")  # mid 100, spread 10 -> exactly 10%
        assert at.spread_pct == DEFAULT_MAX_SPREAD_PCT
        assert at.status() is QuoteFlag.OK

        past = _quote("94.9", "105")
        assert past.spread_pct is not None
        assert past.spread_pct > DEFAULT_MAX_SPREAD_PCT
        assert past.status() is QuoteFlag.TOO_WIDE

    def test_the_check_can_be_disabled_for_a_caller_that_wants_raw(self) -> None:
        assert _quote("1", "1833").status(max_spread_pct=None) is QuoteFlag.OK

    def test_spread_pct_is_none_on_a_one_sided_book(self) -> None:
        assert _quote("100", None).spread_pct is None
        assert _quote(None, "100").spread_pct is None


class TestOpenInterest:
    def test_a_reported_zero_is_refused(self) -> None:
        assert _quote("100", "101", open_interest=0).status() is QuoteFlag.NO_OPEN_INTEREST

    def test_an_unreported_open_interest_is_not_evidence_of_anything(self) -> None:
        """`None` means the feed never said, which must not be read as zero.

        The synthetic chain fixtures and several live feeds leave this unset;
        treating absence as zero would make every one of them untradeable.
        """
        quote = _quote("100", "101")

        assert quote.open_interest is None
        assert quote.status() is QuoteFlag.OK


class TestTheOlderChecksStillWin:
    """Order matters: a more fundamental defect should be reported as itself,
    not mislabelled as a wide spread."""

    @pytest.mark.parametrize(
        ("bid", "ask", "expected"),
        [
            (None, "100", QuoteFlag.EMPTY_BOOK),
            ("100", None, QuoteFlag.EMPTY_BOOK),
            ("0", "100", QuoteFlag.NON_POSITIVE),
            ("-5", "100", QuoteFlag.NON_POSITIVE),
            ("200", "100", QuoteFlag.CROSSED),
        ],
    )
    def test_a_broken_book_keeps_its_own_flag(
        self, bid: str | None, ask: str | None, expected: QuoteFlag
    ) -> None:
        assert _quote(bid, ask).status() is expected

    def test_staleness_is_still_opt_in(self) -> None:
        late = Quote(
            exchange_ts=TS,
            received_ts=datetime(2026, 8, 26, 14, 50, 30, tzinfo=UTC),
            bid=Decimal("100"),
            ask=Decimal("101"),
        )

        assert late.status() is QuoteFlag.OK
        assert late.status(stale_after_s=5) is QuoteFlag.STALE
