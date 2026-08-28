"""What a command needs from config, resolved once for both paths.

Three CLI commands each carried their own copy of "read these fifteen settings
out of the config, or use these defaults when there is no config" - about a
hundred and twenty lines of near-duplicate, and every new risk setting had to be
added in **six** places.

That is not a style complaint. Adding `flatten_on_trip` (D-115) meant the same
edit three times, the three `else` branches were missed, and one of them shipped
as an `UnboundLocalError` that a 790-test suite did not see (D-117). A shape that
produces that bug once will produce it again.

**The defaults are the schema's own.** The old `else` branches disagreed with
each other - `backtest` fell back to no margin cap and no kill switch while the
bhavcopy commands fell back to a 50% cap and a live switch - so a run without
`--config` behaved differently depending on which command you typed, in a
risk-relevant way that nothing documented. There is now one answer, and it is the
one `config/goldm.yaml` would give.

Kill-switch *parameters* are carried rather than a built `KillSwitch`, so this
module stays free of any dependency on the risk layer; the caller constructs one
if it wants one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from algo.config.schema import AppConfig


@dataclass(frozen=True, slots=True)
class RunSettings:
    """Every risk and strategy knob a run takes from configuration."""

    starting_equity: Decimal
    lots: int
    max_concurrent_positions: int
    max_lots_per_underlying: int
    margin_cap_pct: Decimal | None
    daily_loss_limit_pct: Decimal
    max_consecutive_losses: int
    max_drawdown_pct: Decimal
    flatten_on_trip: bool
    force_exit_sessions_before_expiry: int
    block_new_entries_within_dte: int
    stop_viability_threshold: Decimal | None
    on_stop_viability_breach: str
    mode: str

    @classmethod
    def from_config(cls, config: AppConfig) -> RunSettings:
        return cls(
            starting_equity=config.risk.starting_equity,
            lots=config.risk.sizing.fixed_lots,
            max_concurrent_positions=config.risk.caps.max_concurrent_positions,
            max_lots_per_underlying=config.risk.caps.max_lots_per_underlying,
            margin_cap_pct=config.risk.caps.max_total_margin_pct,
            daily_loss_limit_pct=config.risk.kill_switch.daily_loss_limit_pct,
            max_consecutive_losses=config.risk.kill_switch.max_consecutive_losses,
            max_drawdown_pct=config.risk.kill_switch.max_drawdown_pct,
            flatten_on_trip=config.risk.kill_switch.flatten_on_trip,
            force_exit_sessions_before_expiry=(
                config.risk.devolvement.force_exit_sessions_before_expiry
            ),
            block_new_entries_within_dte=(
                config.risk.devolvement.block_new_entries_within_dte
            ),
            stop_viability_threshold=config.strategy.exit.min_stop_to_cost_ratio,
            on_stop_viability_breach=config.strategy.exit.on_stop_viability_breach,
            mode=config.mode.value,
        )

    @classmethod
    def defaults(cls) -> RunSettings:
        """What a run with no `--config` uses.

        Built from the schema rather than written out again, so "the default" has
        exactly one definition and cannot drift from the reference config.
        `instruments` is the only field with no default; GOLDM is the project's
        only instrument and is never read from here.
        """
        return cls.from_config(
            AppConfig(mode="backtest", instruments=[{"underlying": "GOLDM"}])  # type: ignore[arg-type]
        )
