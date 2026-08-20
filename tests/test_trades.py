"""Round trips and the statistics that need them. Brief §10.

The property worth stating: a strangle is **one** trade, not two. Both legs open
together and close together, and reporting them separately would show one winner
and one loser on a position that was always a single bet — which would make the
win rate, the profit factor and the R-distribution all meaningless at once.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from algo.core.enums import Exchange, Right, Side
from algo.core.errors import DomainError
from algo.core.fill import Charges, Fill
from algo.core.instrument import FutureId, OptionId
from algo.core.timeutil import utc
from algo.core.trade import Trade, TradeLeg
from algo.portfolio.trades import TradeBuilder, summarise
from algo.reporting.metrics import trade_stats

NOW = utc(2026, 8, 19, 4, 0)
FUT = FutureId(underlying="GOLDM", expiry=date(2026, 9, 4))


def _option(strike: str, right: Right) -> OptionId:
    return OptionId(
        underlying_future=FUT,
        option_expiry=date(2026, 8, 28),
        strike=Decimal(strike),
        right=right,
        exchange=Exchange.MCX,
    )


CALL = _option("160500", Right.CE)
PUT = _option("153000", Right.PE)


def _fill(option: OptionId, side: Side, price: str, *, at: int = 0, fee: str = "20") -> Fill:
    return Fill(
        fill_id=f"{option.key}-{side}-{price}",
        client_order_id="c",
        signal_id="sig1",
        instrument=option,
        side=side,
        lots=1,
        qty=Decimal("1"),
        price=Decimal(price),
        ts=NOW + timedelta(minutes=30 * at),
        charges=Charges(brokerage=Decimal(fee)),
    )


def _builder() -> TradeBuilder:
    builder = TradeBuilder("goldm_delta_strangle_v1")
    builder.open(
        signal_id="sig1",
        reason="short strangle, 9d to 2026-08-28",
        context={"dte": "9", "call_strike": "160500"},
        risk_r=None,
        at=NOW,
        realised_so_far=Decimal("0"),
    )
    return builder


class TestBuildingATrade:
    def test_a_strangle_is_one_trade_with_two_legs(self) -> None:
        builder = _builder()
        for fill in (
            _fill(CALL, Side.SELL, "755.50"),
            _fill(PUT, Side.SELL, "768.50"),
            _fill(CALL, Side.BUY, "600.00", at=4),
            _fill(PUT, Side.BUY, "560.00", at=4),
        ):
            builder.add(fill)
        trade = builder.close(
            at=NOW + timedelta(hours=2), realised_now=Decimal("3640"), exit_reason="TAKE_PROFIT"
        )

        assert len(trade.legs) == 2
        assert {leg.instrument.key for leg in trade.legs} == {CALL.key, PUT.key}
        assert all(leg.side is Side.SELL for leg in trade.legs)

    def test_each_leg_records_where_it_opened_and_closed(self) -> None:
        builder = _builder()
        builder.add(_fill(CALL, Side.SELL, "755.50"))
        builder.add(_fill(CALL, Side.BUY, "600.00", at=4))
        trade = builder.close(at=NOW, realised_now=Decimal("1555"), exit_reason="TAKE_PROFIT")

        leg = trade.legs[0]
        assert leg.entry_price == Decimal("755.50")
        assert leg.exit_price == Decimal("600.00")
        assert leg.exit_ts is not None

    def test_pnl_comes_from_the_portfolio_not_a_second_calculation(self) -> None:
        """One piece of code knows how a round trip closes out; a second
        implementation would eventually disagree with it."""
        builder = _builder()
        builder.add(_fill(CALL, Side.SELL, "755.50"))
        builder.add(_fill(CALL, Side.BUY, "600.00", at=4))
        trade = builder.close(at=NOW, realised_now=Decimal("1555"), exit_reason="TAKE_PROFIT")

        assert trade.gross_pnl == Decimal("1555")
        assert trade.charges.total == Decimal("40")
        assert trade.net_pnl == Decimal("1515")

    def test_the_reason_travels_with_the_trade(self) -> None:
        """Brief §5's six-weeks-later question, answerable from the trade log."""
        trade = _builder().close(
            at=NOW, realised_now=Decimal("0"), exit_reason="TAKE_PROFIT"
        )
        assert "short strangle" in trade.reason
        assert trade.context["dte"] == "9"
        assert trade.exit_reason == "TAKE_PROFIT"

    def test_r_is_the_configured_stop_not_the_maximum_loss(self) -> None:
        """Assumption 7.4. A short strangle's maximum loss is unbounded, so an R
        measured against it would mean nothing."""
        builder = _builder()
        builder.set_risk(Decimal("1000"))
        builder.add(_fill(CALL, Side.SELL, "755.50", fee="0"))
        builder.add(_fill(CALL, Side.BUY, "600.00", at=4, fee="0"))
        trade = builder.close(at=NOW, realised_now=Decimal("1555"), exit_reason="TAKE_PROFIT")
        assert trade.r_multiple == Decimal("1.555")

    def test_no_stop_means_no_r_multiple(self) -> None:
        """Rather than a number computed against a denominator nobody chose."""
        builder = _builder()
        builder.add(_fill(CALL, Side.SELL, "755.50"))
        trade = builder.close(at=NOW, realised_now=Decimal("100"), exit_reason="END_OF_RUN")
        assert trade.r_multiple is None

    def test_two_trades_cannot_be_open_at_once(self) -> None:
        builder = _builder()
        with pytest.raises(DomainError, match="reported as one"):
            builder.open(
                signal_id="sig2",
                reason="x",
                context={},
                risk_r=None,
                at=NOW,
                realised_so_far=Decimal("0"),
            )

    def test_a_fill_with_no_trade_open_is_refused(self) -> None:
        builder = TradeBuilder("s")
        with pytest.raises(DomainError, match="no trade open"):
            builder.add(_fill(CALL, Side.SELL, "755.50"))

    def test_an_unfinished_trade_is_abandoned_not_counted(self) -> None:
        """A position still open when the data ends is not a completed round trip;
        counting it would put an unrealised figure into a realised statistic."""
        builder = _builder()
        builder.add(_fill(CALL, Side.SELL, "755.50"))
        builder.abandon()
        assert not builder.is_open


def _trade(net: str, *, r: str | None = None, charges: str = "0") -> Trade:
    return Trade(
        trade_id=f"t{net}{r}",
        strategy_id="s",
        signal_id="sig",
        legs=(
            TradeLeg(
                instrument=CALL,
                side=Side.SELL,
                lots=1,
                entry_price=Decimal("755.50"),
                entry_ts=NOW,
            ),
        ),
        opened_at=NOW,
        closed_at=NOW + timedelta(hours=1),
        gross_pnl=Decimal(net) + Decimal(charges),
        charges=Charges(brokerage=Decimal(charges)),
        r_multiple=Decimal(r) if r is not None else None,
        exit_reason="TAKE_PROFIT",
        reason="test",
    )


class TestTradeStatistics:
    def test_win_rate_and_counts(self) -> None:
        stats = trade_stats([_trade("100"), _trade("-50"), _trade("200"), _trade("-30")])
        assert stats.trades == 4
        assert (stats.wins, stats.losses) == (2, 2)
        assert stats.win_rate == Decimal("50")

    def test_profit_factor(self) -> None:
        stats = trade_stats([_trade("300"), _trade("-100")])
        assert stats.gross_profit == Decimal("300")
        assert stats.gross_loss == Decimal("100")
        assert stats.profit_factor == Decimal("3")

    def test_profit_factor_is_none_with_no_losses(self) -> None:
        """`None` means nothing to divide by. 0.0 would read as 'loses everything'."""
        assert trade_stats([_trade("100"), _trade("50")]).profit_factor is None

    def test_longest_losing_streak(self) -> None:
        stats = trade_stats(
            [_trade("10"), _trade("-1"), _trade("-2"), _trade("-3"), _trade("5"), _trade("-1")]
        )
        assert stats.longest_losing_streak == 3

    def test_expectancy_and_average_r(self) -> None:
        stats = trade_stats([_trade("100", r="2"), _trade("-50", r="-1"), _trade("100", r="2")])
        assert stats.expectancy_r == Decimal("1")
        assert stats.average_win_r == Decimal("2")
        assert stats.average_loss_r == Decimal("-1")

    def test_r_statistics_say_how_many_trades_they_used(self) -> None:
        """Averaging a subset without saying so overstates what the number covers."""
        stats = trade_stats([_trade("100", r="2"), _trade("-50"), _trade("30")])
        assert stats.trades == 3
        assert stats.trades_with_r == 1
        assert stats.expectancy_r == Decimal("2")

    def test_no_trades_gives_no_ratios(self) -> None:
        stats = trade_stats([])
        assert stats.trades == 0
        assert stats.win_rate is None
        assert stats.profit_factor is None
        assert stats.expectancy_r is None

    def test_largest_win_and_loss(self) -> None:
        stats = trade_stats([_trade("100"), _trade("-250"), _trade("40")])
        assert stats.largest_win == Decimal("100")
        assert stats.largest_loss == Decimal("-250")

    def test_the_r_distribution_is_available_not_just_the_mean(self) -> None:
        """A premium seller's shape — many small wins, rare large losses — is
        invisible in an average and obvious in a histogram (brief §10)."""
        stats = trade_stats(
            [_trade("10", r="0.4")] * 8 + [_trade("-100", r="-3.0")]
        )
        assert len(stats.r_multiples) == 9
        buckets = stats.histogram(buckets=4)
        assert sum(count for _, count in buckets) == 9
        assert buckets[0][1] == 1, "the single large loss sits alone in the left bucket"
        assert buckets[-1][1] == 8

    def test_the_histogram_copes_with_identical_values(self) -> None:
        stats = trade_stats([_trade("10", r="1.0")] * 3)
        assert stats.histogram() == [("+1.00R", 3)]


class TestSummary:
    def test_it_reads_as_a_trade_log(self) -> None:
        builder = _builder()
        builder.set_risk(Decimal("1000"))
        builder.add(_fill(CALL, Side.SELL, "755.50", fee="0"))
        builder.add(_fill(CALL, Side.BUY, "600.00", at=4, fee="0"))
        trade = builder.close(
            at=NOW + timedelta(hours=2), realised_now=Decimal("1555"), exit_reason="TAKE_PROFIT"
        )
        line = summarise([trade])
        assert "TAKE_PROFIT" in line
        assert "+1.56R" in line  # 1.555 rounds to 1.56
        assert "sold" in line

    def test_an_empty_log_says_so(self) -> None:
        assert summarise([]) == "no completed round trips"
