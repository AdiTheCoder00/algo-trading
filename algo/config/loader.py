"""Configuration loading and hashing.

Precedence is CLI > env > YAML > defaults, and it is applied in one place so that
"where did this value come from?" always has one answer.

Numbers that represent money or percentages must be quoted strings in the YAML.
YAML's bare-number rule would otherwise parse `1000000.00` as a float, and a float
that has already lost precision cannot be recovered by converting it to Decimal
afterwards. The loader checks for this and refuses, rather than accepting a value
it knows to be slightly wrong.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from algo.config.schema import AppConfig
from algo.core.errors import ConfigError
from algo.core.ids import canonical_json, stable_hash

#: Config paths whose values must be quoted in YAML, because they become Decimals.
_MONEY_PATHS = (
    "risk.starting_equity",
    "risk.sizing.margin_pct",
    "risk.sizing.risk_pct",
    "risk.caps.max_total_margin_pct",
    "risk.kill_switch.daily_loss_limit_pct",
    "risk.kill_switch.max_drawdown_pct",
    "strategy.target_delta",
    "strategy.delta_tolerance",
    "strategy.exit.take_profit_value",
    "strategy.exit.stop_loss_value",
    "strategy.exit.min_stop_to_cost_ratio",
)

ENV_PREFIX = "ALGO_"

#: Env namespaces consumed *outside* the config schema — credentials read directly
#: by `credentials_from_env`, never injected into the resolved config. Injecting
#: them would leak secrets into the hashed config, and the schema forbids extras
#: precisely so that such a leak fails loudly instead of silently.
NON_CONFIG_ENV_PREFIXES = ("ALGO_SMARTAPI_", "ALGO_KOTAK_", "ALGO_TELEGRAM_")

#: Single env vars consumed outside the config schema, same reasoning as the
#: prefixes above but not a namespace - `algo/api/app.py`'s own bearer token
#: (`TOKEN_ENV`), documented in `.env.example` alongside every other var here.
#: Without this exclusion, setting it the way `.env.example` instructs makes
#: every CLI command that calls `load_config` fail with "api_token — Extra
#: inputs are not permitted", since it is read directly and is not part of
#: `AppConfig`.
NON_CONFIG_ENV_VARS = ("ALGO_API_TOKEN",)


def load_config(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Build the resolved, frozen configuration."""
    raw: dict[str, Any] = {}
    if path is not None:
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level")
        raw = loaded
        _reject_float_money(raw, path)

    for key, value in _env_overrides(os.environ if env is None else env).items():
        _set_path(raw, key, value)

    for key, value in (overrides or {}).items():
        _set_path(raw, key, value)

    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:
        # Brief §12: never swallow. Name the file so the message is actionable.
        raise ConfigError(f"invalid configuration{f' in {path}' if path else ''}: {exc}") from exc


def config_hash(config: AppConfig) -> str:
    """Stable hash of the resolved config, stamped into every run and signal id."""
    return stable_hash(config.model_dump(mode="json"))


def config_fingerprint(config: AppConfig) -> str:
    """Human-readable canonical form, written alongside run artefacts."""
    return canonical_json(config.model_dump(mode="json"))


def _reject_float_money(raw: dict[str, Any], path: Path) -> None:
    for dotted in _MONEY_PATHS:
        value = _get_path(raw, dotted)
        if isinstance(value, float):
            raise ConfigError(
                f"{path}: '{dotted}' is {value!r}, an unquoted YAML float. Money and "
                f"percentage values must be quoted strings so they convert to Decimal "
                f'exactly - write "{value}" instead.'
            )


def _env_overrides(env: Mapping[str, str]) -> dict[str, str]:
    """`ALGO_RISK__SIZING__FIXED_LOTS=2` -> `risk.sizing.fixed_lots = "2"`."""
    out: dict[str, str] = {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        if key in NON_CONFIG_ENV_VARS:
            continue
        if any(key.startswith(namespace) for namespace in NON_CONFIG_ENV_PREFIXES):
            continue
        dotted = key[len(ENV_PREFIX) :].lower().replace("__", ".")
        out[dotted] = value
    return out


def _get_path(mapping: dict[str, Any], dotted: str) -> Any:
    node: Any = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(mapping: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = mapping
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = value
