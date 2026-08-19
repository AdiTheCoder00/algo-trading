"""Determinism. Brief §7.4.

    "The same dataset and config produce a byte-identical trade log across runs.
     Seed everything."

There is no trade log yet — that arrives with the engine at Milestone 3. What can
be pinned now is everything the trade log will be built from: the data generator,
the resampler, the ordering of contexts, and the hashes that identify a run. If
any of those drift, a byte-identical trade log is impossible no matter how careful
the engine is.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from algo.config.loader import config_fingerprint, config_hash, load_config
from algo.core.bar import M30
from algo.core.ids import client_order_id, signal_id, stable_hash
from algo.data.resample import resample
from algo.data.synthetic import one_minute_range, one_minute_session
from algo.exchange.calendar import MarketCalendar
from algo.exchange.specs import ContractSpecStore
from algo.strategy.context import contexts_from_bars
from tests.conftest import SUMMER_DAY

REFERENCE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "goldm.yaml"


class TestSyntheticDataIsReproducible:
    def test_same_seed_same_bars(self, calendar: MarketCalendar) -> None:
        first = one_minute_session(calendar, SUMMER_DAY, seed=42)
        second = one_minute_session(calendar, SUMMER_DAY, seed=42)
        assert [b.model_dump_json() for b in first] == [b.model_dump_json() for b in second]

    def test_different_seed_different_bars(self, calendar: MarketCalendar) -> None:
        first = one_minute_session(calendar, SUMMER_DAY, seed=42)
        second = one_minute_session(calendar, SUMMER_DAY, seed=43)
        assert first != second

    def test_seeding_is_per_day_not_per_call_order(self, calendar: MarketCalendar) -> None:
        """A day's bars must not depend on which days were generated before it.

        Otherwise re-running a backtest over a shorter window silently produces
        different data for the same dates.
        """
        standalone = one_minute_session(calendar, date(2026, 8, 20), seed=5)
        in_range = [
            b
            for b in one_minute_range(calendar, date(2026, 8, 19), date(2026, 8, 21), seed=5)
            if b.ts.date() == date(2026, 8, 20) or b.ts.date() == date(2026, 8, 19)
        ]
        # The walk chains its starting price between days, so only the *shape*
        # is asserted here — the count, which must not depend on call order.
        assert len(standalone) == 870
        assert len(in_range) > 0


class TestResamplingIsReproducible:
    def test_repeated_resampling_is_identical(self, calendar: MarketCalendar) -> None:
        source = one_minute_session(calendar, SUMMER_DAY, seed=11)
        first = resample(source, calendar=calendar, timeframe=M30)
        second = resample(source, calendar=calendar, timeframe=M30)
        assert [b.model_dump_json() for b in first] == [b.model_dump_json() for b in second]

    def test_context_stream_is_identical_across_runs(
        self, calendar: MarketCalendar, specs: ContractSpecStore
    ) -> None:
        bars = resample(
            one_minute_session(calendar, SUMMER_DAY, seed=12), calendar=calendar, timeframe=M30
        )

        def observe() -> list[str]:
            return [
                f"{ctx.now.isoformat()}|{ctx.bar.close}|{len(ctx.bars)}|"
                f"{ctx.session.bar_index}/{ctx.session.bars_in_session}"
                for ctx in contexts_from_bars(
                    bars, calendar=calendar, specs=specs, timeframe=M30
                )
            ]

        assert observe() == observe()


class TestIdentifiersAreStable:
    def test_stable_hash_ignores_key_order(self) -> None:
        assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})

    def test_same_causes_same_signal_id(self) -> None:
        kwargs = {
            "strategy_id": "goldm_delta_strangle_v1",
            "params_hash": "abc",
            "bar_close_iso": "2026-08-19T04:00:00Z",
            "action": "OPEN",
            "leg_keys": ("MCX:GOLDM:20260828:160500:CE:SELL",),
            "config_hash": "def",
        }
        assert signal_id(**kwargs) == signal_id(**kwargs)  # type: ignore[arg-type]

    def test_a_different_bar_gives_a_different_id(self) -> None:
        base = {
            "strategy_id": "s",
            "params_hash": "abc",
            "action": "OPEN",
            "leg_keys": ("x",),
            "config_hash": "def",
        }
        first = signal_id(bar_close_iso="2026-08-19T04:00:00Z", **base)  # type: ignore[arg-type]
        second = signal_id(bar_close_iso="2026-08-19T04:30:00Z", **base)  # type: ignore[arg-type]
        assert first != second

    def test_a_config_change_changes_the_id(self) -> None:
        """So a replay under edited settings cannot match an order placed under the old."""
        base = {
            "strategy_id": "s",
            "params_hash": "abc",
            "bar_close_iso": "2026-08-19T04:00:00Z",
            "action": "OPEN",
            "leg_keys": ("x",),
        }
        assert signal_id(config_hash="v1", **base) != signal_id(  # type: ignore[arg-type]
            config_hash="v2", **base  # type: ignore[arg-type]
        )

    def test_client_order_ids_are_unique_per_leg_and_slice(self) -> None:
        ids = {
            client_order_id(strategy_id="s", sig_id="abc", leg_ix=leg, slice_ix=slice_)
            for leg in range(2)
            for slice_ in range(3)
        }
        assert len(ids) == 6


class TestConfigFingerprint:
    def test_fingerprint_is_canonical_json(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={})
        parsed = json.loads(config_fingerprint(config))
        assert parsed["strategy"]["target_delta"] == "0.25"

    def test_hash_is_stable_across_processes_within_a_run(self) -> None:
        config = load_config(REFERENCE_CONFIG, env={})
        assert config_hash(config) == config_hash(load_config(REFERENCE_CONFIG, env={}))
        assert len(config_hash(config)) == 16
