"""Cadence state across a restart (D-110).

`DeltaStrangle`'s docstring has said since Milestone 4 that the traded-cycle set
"is genuine strategy state and must be persisted for a live restart to behave
correctly". This is that, and the reason it matters is narrow but expensive: a
flat account looks identical whether this cycle was traded and closed or never
entered at all, so a restart that forgets will sell a second strangle into a
cycle it has already traded.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.errors import DomainError
from algo.persistence.state import StateStore
from algo.strategy.delta_strangle import DeltaStrangle

AUG = date(2026, 8, 28)
SEP = date(2026, 9, 25)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    with StateStore(tmp_path / "state.db") as s:
        yield s


def _strangle(**kwargs: object) -> DeltaStrangle:
    return DeltaStrangle(underlying="GOLDM", **kwargs)  # type: ignore[arg-type]


class TestTheCadenceSurvivesARoundTrip:
    def test_an_untraded_strategy_saves_nothing_meaningful(self) -> None:
        assert _strangle().state() == {"traded_cycles": ""}

    def test_traded_cycles_round_trip(self) -> None:
        before = _strangle()
        before._traded_cycles = {AUG, SEP}

        after = _strangle()
        after.restore(before.state())

        assert after._traded_cycles == {AUG, SEP}

    def test_the_saved_form_is_sorted_and_stable(self) -> None:
        """Two strategies holding the same cycles must serialise identically -
        otherwise the stored payload churns on every write for no reason."""
        one, two = _strangle(), _strangle()
        one._traded_cycles = {SEP, AUG}
        two._traded_cycles = {AUG, SEP}

        assert one.state() == two.state()
        assert one.state()["traded_cycles"] == "2026-08-28,2026-09-25"

    def test_restoring_nothing_leaves_the_set_empty(self) -> None:
        strategy = _strangle()

        strategy.restore({})

        assert strategy._traded_cycles == set()

    def test_an_unknown_key_is_tolerated(self) -> None:
        """A newer build's state must not crash an older strategy."""
        strategy = _strangle()

        strategy.restore({"traded_cycles": "2026-08-28", "something_new": "x"})

        assert strategy._traded_cycles == {AUG}


class TestCorruptStateIsRefused:
    def test_it_raises_rather_than_starting_empty(self) -> None:
        """Starting with an empty cadence set is precisely the state that lets a
        cycle be entered twice, so a garbled payload must stop the process."""
        strategy = _strangle()

        with pytest.raises(DomainError, match="entered a second time"):
            strategy.restore({"traded_cycles": "not-a-date"})

    def test_a_partial_corruption_still_raises(self) -> None:
        strategy = _strangle()

        with pytest.raises(DomainError):
            strategy.restore({"traded_cycles": "2026-08-28,rubbish"})


class TestTheStoreGuardsOnParameters:
    """A cadence recorded under different parameters is a different strategy's."""

    def test_state_comes_back_for_a_matching_hash(self, store: StateStore) -> None:
        strategy = _strangle()
        strategy._traded_cycles = {AUG}
        store.record_strategy_state(
            strategy_id=strategy.strategy_id,
            params_hash=strategy.params_hash(),
            state=strategy.state(),
        )

        loaded = store.strategy_state(
            strategy_id=strategy.strategy_id, params_hash=strategy.params_hash()
        )

        assert loaded == {"traded_cycles": "2026-08-28"}

    def test_a_changed_parameter_hides_the_state(self, store: StateStore) -> None:
        saved = _strangle()
        saved._traded_cycles = {AUG}
        store.record_strategy_state(
            strategy_id=saved.strategy_id,
            params_hash=saved.params_hash(),
            state=saved.state(),
        )

        # Same strategy id, different settings - the operator edited the config.
        changed = _strangle(target_delta=Decimal("0.30"))
        assert changed.params_hash() != saved.params_hash()

        assert (
            store.strategy_state(
                strategy_id=changed.strategy_id, params_hash=changed.params_hash()
            )
            is None
        )

    def test_nothing_saved_reads_as_none(self, store: StateStore) -> None:
        strategy = _strangle()

        assert (
            store.strategy_state(
                strategy_id=strategy.strategy_id, params_hash=strategy.params_hash()
            )
            is None
        )

    def test_a_second_write_replaces_the_first(self, store: StateStore) -> None:
        strategy = _strangle()
        for cycles in ({AUG}, {AUG, SEP}):
            strategy._traded_cycles = cycles
            store.record_strategy_state(
                strategy_id=strategy.strategy_id,
                params_hash=strategy.params_hash(),
                state=strategy.state(),
            )

        loaded = store.strategy_state(
            strategy_id=strategy.strategy_id, params_hash=strategy.params_hash()
        )

        assert loaded == {"traded_cycles": "2026-08-28,2026-09-25"}


class TestTheRestartActuallyBlocksASecondEntry:
    """The whole point, asserted end to end rather than by parts."""

    def test_a_restored_strategy_refuses_a_cycle_it_already_traded(
        self, store: StateStore
    ) -> None:
        traded = _strangle()
        traded._traded_cycles = {AUG}
        store.record_strategy_state(
            strategy_id=traded.strategy_id,
            params_hash=traded.params_hash(),
            state=traded.state(),
        )

        # A brand new process, same config.
        restarted = _strangle()
        assert AUG not in restarted._traded_cycles, "fresh strategy starts empty"

        saved = store.strategy_state(
            strategy_id=restarted.strategy_id, params_hash=restarted.params_hash()
        )
        assert saved is not None
        restarted.restore(saved)

        assert AUG in restarted._traded_cycles, (
            "without this the restarted process sells a second strangle "
            "into a cycle it has already traded"
        )
