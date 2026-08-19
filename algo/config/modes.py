"""The trading-mode gate. Brief §2.1.

    "Live trading is off by default. `live` requires an explicit env var
     TRADING_MODE=live and a --i-understand-this-is-real-money CLI flag.
     No default, no fallback, no 'if unset assume live.'"

Implemented as three independent conditions that must all agree. Any one of them
missing refuses the run, and the refusal says which one — because the failure mode
to avoid is someone disabling the check out of frustration at an unclear message.

`backtest` and `paper` need no ceremony: they cannot send an order to a real
account, so gating them would only train the operator to click through warnings.
"""

from __future__ import annotations

import os

from algo.core.enums import Mode
from algo.core.errors import ModeError

LIVE_ENV_VAR = "TRADING_MODE"
LIVE_FLAG = "--i-understand-this-is-real-money"


def resolve_mode(
    configured: Mode,
    *,
    env: dict[str, str] | None = None,
    real_money_flag: bool = False,
) -> Mode:
    """Return the mode to run in, or raise if the live gate is not fully satisfied."""
    environment = os.environ if env is None else env

    # Exact match, deliberately: no strip(), no lower(). This is the one check
    # standing between the engine and a real account, and "no fallback" (§2.1)
    # means no lenient interpretation either. `TRADING_MODE=LIVE ` with a stray
    # shell space is refused, and the error quotes the value back so the space is
    # visible rather than mysterious.
    raw = environment.get(LIVE_ENV_VAR)

    if configured is not Mode.LIVE:
        # Guard against the opposite mistake: env demanding live while the config
        # says otherwise. Rather than silently obeying either, say they disagree.
        if raw == Mode.LIVE.value:
            raise ModeError(
                f"{LIVE_ENV_VAR}=live is set but the configuration says mode: {configured}. "
                "Refusing to guess which one you meant."
            )
        return configured

    if raw != Mode.LIVE.value:
        raise ModeError(
            f"configuration requests live trading but {LIVE_ENV_VAR} is "
            f"{'unset' if raw is None else repr(raw)}. "
            f"Set {LIVE_ENV_VAR}=live exactly (lowercase, no surrounding spaces) to proceed."
        )
    if not real_money_flag:
        raise ModeError(
            f"configuration requests live trading but {LIVE_FLAG} was not passed. "
            "This flag exists so that starting a live session is always deliberate."
        )
    return Mode.LIVE


def is_live(mode: Mode) -> bool:
    return mode is Mode.LIVE


def can_place_real_orders(mode: Mode) -> bool:
    """Only `live` reaches a real account. Paper simulates fills on live data."""
    return mode is Mode.LIVE
