"""The state the dashboard reads.

The engine writes here; the API only reads. That separation is deliberate and it
is the point of having a store at all rather than handing the API a reference to
the running engine.

**The API process must not be able to perturb the engine.** A web framework holding
a live `Portfolio` is one bug away from mutating trading state to serve an HTTP
request, and a dashboard that can move a position is not a dashboard. So the two
processes share a SQLite file and nothing else.

The one thing that must travel the other way — the kill switch — travels as a
*request*, not an action. The API writes a row saying a halt was asked for; the
engine reads it on its next bar and trips its own switch. The API never touches the
switch itself, so a dead API cannot leave the engine in a half-tripped state, and a
dead engine cannot swallow a halt request — it is still sitting in the table when
the engine comes back.

Every `Decimal` is stored as TEXT, for the same reason as the order journal: this
file is a record, and a rounded number in a record is not recoverable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from algo.core.timeutil import ensure_utc, iso

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS equity (
    ts          TEXT PRIMARY KEY,
    equity      TEXT NOT NULL,
    cash        TEXT NOT NULL,
    realised    TEXT NOT NULL,
    unrealised  TEXT NOT NULL,
    charges     TEXT NOT NULL,
    open_positions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    instrument_key TEXT PRIMARY KEY,
    lots           INTEGER NOT NULL,
    qty            TEXT NOT NULL,
    average_price  TEXT NOT NULL,
    mark           TEXT,
    unrealised     TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id   TEXT PRIMARY KEY,
    opened_at  TEXT NOT NULL,
    closed_at  TEXT,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id  TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    strategy   TEXT NOT NULL,
    action     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    context    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kill_switch_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reason       TEXT NOT NULL,
    flatten      INTEGER NOT NULL DEFAULT 0,
    acted_on_at  TEXT
);

-- A snapshot, not a log, same reasoning as positions: the dashboard wants to
-- know what the chain looks like right now, not a bar-by-bar archive of every
-- chain the run ever saw. One row, replaced each time it changes.
CREATE TABLE IF NOT EXISTS chain_snapshot (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    payload  TEXT NOT NULL
);

--- A strategy's own state, so a restart does not silently lose it. One row per
--- strategy. `params_hash` is a guard, not a label: state saved under a
--- different parameter set is never handed back (D-110).
CREATE TABLE IF NOT EXISTS strategy_state (
    strategy_id  TEXT PRIMARY KEY,
    params_hash  TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS trades_opened ON trades(opened_at);
"""


class EquityRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    equity: Decimal
    cash: Decimal
    realised: Decimal
    unrealised: Decimal
    charges: Decimal
    open_positions: int


class PositionRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_key: str
    lots: int
    qty: Decimal
    average_price: Decimal
    mark: Decimal | None = None
    unrealised: Decimal | None = None
    updated_at: datetime


class SignalRow(BaseModel):
    """A signal and, crucially, why it fired.

    Brief §5: "I need to know why a trade fired six weeks later." That is what
    this table exists for — the reason is a column, not a log line that rotates
    away.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str
    ts: datetime
    strategy: str
    action: str
    reason: str
    context: dict[str, str]


class NoteRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    message: str


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    requested_at: datetime
    requested_by: str
    reason: str
    flatten: bool
    acted_on_at: datetime | None


class StateStore:
    """Shared SQLite state. The engine writes; the API reads."""

    __slots__ = ("_conn", "_path")

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the monitoring API opens one StateStore per
        # request as a FastAPI sync dependency (algo/api/app.py), and FastAPI's
        # threadpool executor is free to run that request's open and its later
        # close on different worker threads - confirmed live, not theoretical:
        # "SQLite objects created in a thread can only be used in that same
        # thread" on /positions the first time two panels were added to the
        # dashboard and both endpoints got hit back to back. Safe here because
        # each StateStore instance is still only ever touched by the one
        # request that created it, never concurrently by two - this relaxes
        # sqlite3's same-thread check, it does not remove the single-owner
        # discipline that check_same_thread is actually protecting.
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------- writes (engine)
    def record_equity(self, row: EquityRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?,?)",
                (
                    iso(row.ts),
                    str(row.equity),
                    str(row.cash),
                    str(row.realised),
                    str(row.unrealised),
                    str(row.charges),
                    row.open_positions,
                ),
            )

    def replace_positions(self, rows: list[PositionRow]) -> None:
        """Positions are a snapshot, not a log — an emptied book must empty here."""
        with self._tx() as conn:
            conn.execute("DELETE FROM positions")
            conn.executemany(
                "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        r.instrument_key,
                        r.lots,
                        str(r.qty),
                        str(r.average_price),
                        str(r.mark) if r.mark is not None else None,
                        str(r.unrealised) if r.unrealised is not None else None,
                        iso(r.updated_at),
                    )
                    for r in rows
                ],
            )

    def record_chain_snapshot(self, payload: dict[str, Any]) -> None:
        """The option chain as of the most recent bar. A snapshot, not a log -
        `replace_positions`'s reasoning applies here too: an operator wants to
        know what the chain looks like right now, not page through every chain
        a run ever saw."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO chain_snapshot (id, payload) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (json.dumps(payload, sort_keys=True, default=str),),
            )

    def record_strategy_state(
        self, *, strategy_id: str, params_hash: str, state: Mapping[str, str]
    ) -> None:
        """Persist a strategy's own state so a restart does not lose it.

        `params_hash` is stored alongside, not for information: `strategy_state`
        refuses to hand back state saved under different parameters. The signal
        id already depends on the parameter set for exactly this reason - a
        replay after a config edit must not match an order placed under the old
        settings - and cadence state carries the same hazard.
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO strategy_state (strategy_id, params_hash, payload) "
                "VALUES (?,?,?) ON CONFLICT(strategy_id) DO UPDATE SET "
                "params_hash = excluded.params_hash, payload = excluded.payload",
                (strategy_id, params_hash, json.dumps(dict(state), sort_keys=True)),
            )

    def strategy_state(self, *, strategy_id: str, params_hash: str) -> dict[str, str] | None:
        """What `record_strategy_state` saved, or None.

        None when nothing was saved **and** when what was saved belongs to a
        different parameter set. The caller cannot tell those apart, deliberately:
        both mean "you have no usable prior state", and a caller that treated a
        parameter change as recoverable would be reasoning about a cadence
        recorded by a different strategy.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT params_hash, payload FROM strategy_state WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        if row is None or row[0] != params_hash:
            return None
        loaded: dict[str, str] = json.loads(row[1])
        return loaded

    def record_signal(self, row: SignalRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?)",
                (
                    row.signal_id,
                    iso(row.ts),
                    row.strategy,
                    row.action,
                    row.reason,
                    json.dumps(dict(row.context), sort_keys=True),
                ),
            )

    def record_trade(self, trade_id: str, opened_at: datetime, closed_at: datetime | None,
                     payload: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trades VALUES (?,?,?,?)",
                (
                    trade_id,
                    iso(opened_at),
                    iso(closed_at) if closed_at else None,
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )

    def record_note(self, ts: datetime, message: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO notes (ts, message) VALUES (?,?)", (iso(ts), message)
            )

    def set_health(self, key: str, value: str, *, at: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO health VALUES (?,?,?)", (key, value, iso(at))
            )

    # ------------------------------------------------------------- reads (API)
    def equity_curve(self, limit: int = 1000) -> list[EquityRow]:
        with closing(
            self._conn.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,))
        ) as cursor:
            rows = [
                EquityRow(
                    ts=_dt(r["ts"]),
                    equity=Decimal(r["equity"]),
                    cash=Decimal(r["cash"]),
                    realised=Decimal(r["realised"]),
                    unrealised=Decimal(r["unrealised"]),
                    charges=Decimal(r["charges"]),
                    open_positions=r["open_positions"],
                )
                for r in cursor.fetchall()
            ]
        return list(reversed(rows))

    def positions(self) -> list[PositionRow]:
        with closing(
            self._conn.execute("SELECT * FROM positions ORDER BY instrument_key")
        ) as cursor:
            return [
                PositionRow(
                    instrument_key=r["instrument_key"],
                    lots=r["lots"],
                    qty=Decimal(r["qty"]),
                    average_price=Decimal(r["average_price"]),
                    mark=Decimal(r["mark"]) if r["mark"] else None,
                    unrealised=Decimal(r["unrealised"]) if r["unrealised"] else None,
                    updated_at=_dt(r["updated_at"]),
                )
                for r in cursor.fetchall()
            ]

    def chain_snapshot(self) -> dict[str, Any] | None:
        with closing(
            self._conn.execute("SELECT payload FROM chain_snapshot WHERE id = 1")
        ) as cursor:
            row = cursor.fetchone()
            return json.loads(row["payload"]) if row else None

    def signals(self, limit: int = 50) -> list[SignalRow]:
        with closing(
            self._conn.execute("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))
        ) as cursor:
            return [
                SignalRow(
                    signal_id=r["signal_id"],
                    ts=_dt(r["ts"]),
                    strategy=r["strategy"],
                    action=r["action"],
                    reason=r["reason"],
                    context=json.loads(r["context"]),
                )
                for r in cursor.fetchall()
            ]

    def trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(
            self._conn.execute(
                "SELECT payload FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
            )
        ) as cursor:
            return [json.loads(r["payload"]) for r in cursor.fetchall()]

    def notes(self, limit: int = 50) -> list[NoteRow]:
        with closing(
            self._conn.execute("SELECT ts, message FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        ) as cursor:
            return [NoteRow(ts=_dt(r["ts"]), message=r["message"]) for r in cursor.fetchall()]

    def health(self) -> dict[str, str]:
        with closing(self._conn.execute("SELECT key, value FROM health")) as cursor:
            return {r["key"]: r["value"] for r in cursor.fetchall()}

    # ------------------------------------------------------------ kill switch
    def request_kill_switch(
        self, *, requested_by: str, reason: str, flatten: bool, at: datetime
    ) -> int:
        """Record that a halt was asked for. Does **not** halt anything itself.

        The engine acts on this on its next bar. Keeping it a request rather than
        an action means a dead API cannot leave the engine half-tripped, and a
        dead engine cannot swallow the request — it is still here when it returns.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO kill_switch_requests (requested_at, requested_by, reason, "
                "flatten, acted_on_at) VALUES (?,?,?,?,NULL)",
                (iso(ensure_utc(at)), requested_by, reason, 1 if flatten else 0),
            )
            return int(cursor.lastrowid or 0)

    def pending_kill_switch_requests(self) -> list[KillSwitchRequest]:
        with closing(
            self._conn.execute(
                "SELECT * FROM kill_switch_requests WHERE acted_on_at IS NULL ORDER BY id"
            )
        ) as cursor:
            return [_to_request(r) for r in cursor.fetchall()]

    def mark_kill_switch_acted(self, request_id: int, *, at: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE kill_switch_requests SET acted_on_at=? WHERE id=?",
                (iso(ensure_utc(at)), request_id),
            )

    def kill_switch_requests(self, limit: int = 20) -> list[KillSwitchRequest]:
        with closing(
            self._conn.execute(
                "SELECT * FROM kill_switch_requests ORDER BY id DESC LIMIT ?", (limit,)
            )
        ) as cursor:
            return [_to_request(r) for r in cursor.fetchall()]


def _to_request(row: sqlite3.Row) -> KillSwitchRequest:
    return KillSwitchRequest(
        id=row["id"],
        requested_at=_dt(row["requested_at"]),
        requested_by=row["requested_by"],
        reason=row["reason"],
        flatten=bool(row["flatten"]),
        acted_on_at=_dt(row["acted_on_at"]) if row["acted_on_at"] else None,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
