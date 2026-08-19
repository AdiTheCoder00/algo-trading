"""Deterministic identifiers. Brief §2.3 — every order is idempotent.

A signal id is a hash of what caused the signal: the strategy, its parameters, the
bar that produced it, and the resolved config. Feed the same bar to the same
configuration twice and you get the same id, so a crash-replay cannot create a
second order for the same intent.

`blake2b` rather than a UUID because it must be reproducible across processes and
machines; a random id would make crash recovery guesswork. `sort_keys` and a fixed
separator set because Python's dict ordering must not leak into the hash.
"""

from __future__ import annotations

import json
from hashlib import blake2b
from typing import Any

_DIGEST_BYTES = 8  # 16 hex characters — ample for one account's order flow


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable JSON: sorted keys, no incidental whitespace, non-ASCII escaped."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(payload: dict[str, Any]) -> str:
    """16-character hex digest of `payload`, stable across runs and machines."""
    return blake2b(canonical_json(payload).encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def signal_id(
    *,
    strategy_id: str,
    params_hash: str,
    bar_close_iso: str,
    action: str,
    leg_keys: tuple[str, ...],
    config_hash: str,
) -> str:
    """Identifier for one signal, derived only from its causes."""
    return stable_hash(
        {
            "strategy_id": strategy_id,
            "params_hash": params_hash,
            "bar_close": bar_close_iso,
            "action": action,
            "legs": list(leg_keys),
            "config_hash": config_hash,
        }
    )


def client_order_id(*, strategy_id: str, sig_id: str, leg_ix: int, slice_ix: int = 0) -> str:
    """Broker-facing order identifier.

    Includes the slice index because an order larger than the exchange's per-order
    cap is split, and each slice must be independently idempotent — otherwise a
    retry after a partial slice failure duplicates the slices that did succeed.
    """
    return f"{strategy_id}.{sig_id}.{leg_ix}.{slice_ix}"
