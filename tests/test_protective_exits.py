"""`algo/strategy/protective_exits.py`: the sequencing over the primitives.

`test_price_stop.py` and `test_trailing_profit_stop.py` cover the levels
themselves. This file covers the part that is only visible once they are
composed - which exit is *reported* when a single bar crosses more than one of
them, and the fact that the reported kind names the level the exit actually
filled at. `algo/backtest/cfd_runner.py` prices a close from that kind, so a bar
reported as the wrong one is a wrong P&L figure and not just a wrong label.
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
from algo.strategy.protective_exits import ProtectiveExits

XAUUSD = CfdId(symbol="XAUUSD")
TF = Timeframe(minutes=30)
TS = datetime(2026, 8, 24, tzinfo=UTC)
ENTRY = Decimal("4400.00")


def _bar(*, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        ts=TS, timeframe=TF, open=Decimal(open_), high=Decimal(high),
        low=Decimal(low), close=Decimal(close), volume=100,
    )


def _long(cost: str = "4400.00") -> Position:
    return Position(
        instrument=XAUUSD, lots=1, qty=Decimal("1"), cost_basis=Decimal(cost)
    )


def _short(cost: str = "4400.00") -> Position:
    return Position(
        instrument=XAUUSD, lots=-1, qty=Decimal("-1"), cost_basis=Decimal(cost)
    )


def _exits(**kwargs: Decimal) -> ProtectiveExits:
    base = {
        "stop_loss_pct": Decimal("0"),
        "trail_activation_pct": Decimal("2"),
        "trail_pct": Decimal("0"),
        "giveback_frac": Decimal("0"),
    }
    base.update(kwargs)
    return ProtectiveExits(**base)  # type: ignore[arg-type]


def _run(exits: ProtectiveExits, held: Position, bars: list[Bar]):
    """Feed bars in order, returning the first decision that fired."""
    for bar in bars:
        decision = exits.check(bar, held)
        if decision is not None:
            return decision
    return None


class TestGivebackValidation:
    def test_a_negative_fraction_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="cannot be negative"):
            _exits(giveback_frac=Decimal("-0.1"))

    def test_a_percentage_typed_as_a_fraction_is_rejected_not_clamped(self) -> None:
        """50 meaning "50%" would otherwise behave exactly like 1.0 - a trail
        that exits at cost - with nothing in the error to say why."""
        with pytest.raises(DomainError, match="not a percentage"):
            _exits(giveback_frac=Decimal("50"))

    def test_one_is_allowed_as_the_boundary(self) -> None:
        _exits(giveback_frac=Decimal("1"))

    def test_it_is_published_in_params_because_it_changes_the_exit_level(self) -> None:
        assert _exits(giveback_frac=Decimal("0.5")).params()["giveback_frac"] == "0.5"


class TestGivebackFires:
    def test_it_closes_once_half_the_banked_move_is_handed_back(self) -> None:
        exits = _exits(giveback_frac=Decimal("0.5"))
        # Runs to 4500 (banked 100, well past the 2% gate), then retreats to
        # 4450 - exactly half of it.
        decision = _run(exits, _long(), [
            _bar(open_="4490", high="4500.00", low="4485", close="4495"),
            _bar(open_="4495", high="4495", low="4450.00", close="4460"),
        ])

        assert decision is not None
        assert decision.kind == "giveback"
        assert decision.side is Side.SELL

    def test_it_does_not_close_while_more_than_half_is_still_held(self) -> None:
        exits = _exits(giveback_frac=Decimal("0.5"))
        decision = _run(exits, _long(), [
            _bar(open_="4490", high="4500.00", low="4485", close="4495"),
            _bar(open_="4495", high="4495", low="4450.01", close="4460"),
        ])

        assert decision is None

    def test_a_short_closes_by_buying_back(self) -> None:
        exits = _exits(giveback_frac=Decimal("0.5"))
        decision = _run(exits, _short(), [
            _bar(open_="4310", high="4315", low="4300.00", close="4305"),
            _bar(open_="4305", high="4350.00", low="4305", close="4340"),
        ])

        assert decision is not None
        assert decision.kind == "giveback"
        assert decision.side is Side.BUY

    def test_the_activation_gate_holds_it_off_on_a_small_move(self) -> None:
        """The gate is the reason a trade four dollars up that gives back two is
        not closed for having surrendered half of its peak profit."""
        exits = _exits(giveback_frac=Decimal("0.5"))
        decision = _run(exits, _long(), [
            _bar(open_="4400", high="4404.00", low="4400", close="4403"),
            _bar(open_="4403", high="4403", low="4400.50", close="4401"),
        ])

        assert decision is None

    def test_it_is_off_when_the_fraction_is_zero(self) -> None:
        """The default everywhere, since the trail was measured and rejected
        (see `ema_bb.py`) - so every study run before it existed, and every one
        run after, still reports what it reported."""
        decision = _run(_exits(), _long(), [
            _bar(open_="4490", high="4500.00", low="4485", close="4495"),
            _bar(open_="4495", high="4495", low="4400.00", close="4405"),
        ])

        assert decision is None


class TestOrderingAgainstTheOtherExits:
    def test_the_flat_stop_still_wins_a_bar_that_crosses_both(self) -> None:
        """Unchanged doctrine: a bar's OHLC does not say which extreme printed
        first, so the pessimistic reading stands."""
        exits = _exits(stop_loss_pct=Decimal("0.5"), giveback_frac=Decimal("0.5"))
        decision = _run(exits, _long(), [
            _bar(open_="4490", high="4500.00", low="4485", close="4495"),
            _bar(open_="4495", high="4495", low="4370.00", close="4375"),
        ])

        assert decision is not None
        assert decision.kind == "stop"

    def test_the_nearer_of_the_two_trails_is_reported_giveback_nearer(self) -> None:
        """Peak 4500 on a 4400 entry. The give-back level is 4450; a 5% trail
        sits at 4275. Price retreating from the peak reaches 4450 first, so the
        give-back is what filled - and what the runner must price."""
        exits = _exits(trail_pct=Decimal("5"), giveback_frac=Decimal("0.5"))
        decision = _run(exits, _long(), [
            _bar(open_="4490", high="4500.00", low="4485", close="4495"),
            _bar(open_="4495", high="4495", low="4270.00", close="4280"),
        ])

        assert decision is not None
        assert decision.kind == "giveback"

    def test_the_nearer_of_the_two_trails_is_reported_pct_trail_nearer(self) -> None:
        """Same peak, but a 0.5% trail sits at 4477.50 - now the nearer of the
        two, so the same bar is reported as the percentage trail instead. The
        precedence follows the levels, not the source order."""
        exits = _exits(trail_pct=Decimal("0.5"), giveback_frac=Decimal("0.5"))
        decision = _run(exits, _long(), [
            _bar(open_="4490", high="4500.00", low="4485", close="4495"),
            _bar(open_="4495", high="4495", low="4440.00", close="4445"),
        ])

        assert decision is not None
        assert decision.kind == "trail"

    def test_a_short_resolves_the_two_trails_the_same_way(self) -> None:
        exits = _exits(trail_pct=Decimal("5"), giveback_frac=Decimal("0.5"))
        decision = _run(exits, _short(), [
            _bar(open_="4310", high="4315", low="4300.00", close="4305"),
            _bar(open_="4305", high="4530.00", low="4305", close="4520"),
        ])

        assert decision is not None
        assert decision.kind == "giveback"

    def test_going_flat_clears_the_trail(self) -> None:
        exits = _exits(giveback_frac=Decimal("0.5"))
        held = _long()
        exits.check(_bar(open_="4490", high="4500.00", low="4485", close="4495"), held)
        exits.check(_bar(open_="4495", high="4495", low="4450", close="4460"), None)

        # A new trail peaks at 4460, not the cleared 4500 - so it is not even
        # armed (1.4% banked, against a 2% gate). Had the old peak survived, its
        # level of 4450 would have closed this bar.
        assert exits.check(
            _bar(open_="4455", high="4460", low="4450.00", close="4452"), held
        ) is None
