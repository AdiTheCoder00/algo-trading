"""The write-ahead order journal. Brief §2.3 — never fire-and-forget.

The journal is what makes an order idempotent across a crash. The sequence is:

    1. record the **intent** to place an order          (state JOURNALLED)
    2. mark it SENT — *before* the network call is made
    3. call the broker
    4. mark it ACKNOWLEDGED with the broker's own id

Step 2 comes before step 3 deliberately, and it is the whole design. If the
process dies between 2 and 3, the journal says SENT and we genuinely do not know
whether the broker received it — so reconciliation asks. If instead the state were
written *after* the call, a crash in between would leave JOURNALLED while the
broker held a live order, and the obvious recovery — "not sent yet, send it" —
would double the position.

The safe ambiguity is "might have been sent". The unsafe one is "looks unsent".

SQLite in WAL mode, with every `Decimal` stored as TEXT. Storing a price as REAL
would round it on the way in, and this file is the record used to reconstruct
state after a crash — the one place a rounded number is least recoverable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from algo.core.enums import OrderState
from algo.core.errors import DomainError
from algo.core.fill import Fill
from algo.core.order import Order
from algo.core.timeutil import ensure_utc, iso

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    signal_id       TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    state           TEXT NOT NULL,
    broker_order_id TEXT,
    filled_qty      TEXT NOT NULL DEFAULT '0',
    average_price   TEXT,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS orders_signal ON orders(signal_id);

CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    ts              TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS fills_order ON fills(client_order_id);
"""

#: An order in one of these states is finished; nothing more will happen to it.
TERMINAL = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
)


class JournalEntry(BaseModel):
    """One order's recorded life, as the journal knows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    signal_id: str
    strategy_id: str
    instrument_key: str
    state: OrderState
    broker_order_id: str | None
    filled_qty: Decimal
    average_price: Decimal | None
    last_error: str
    created_at: datetime
    updated_at: datetime
    order: Order

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def may_have_reached_the_broker(self) -> bool:
        """True once the send was attempted.

        The distinction reconciliation turns on. `JOURNALLED` means the call was
        never made; anything later means it might have been, and the broker has to
        be asked rather than assumed.
        """
        return self.state is not OrderState.JOURNALLED


class OrderJournal:
    """Durable, idempotent record of every order this system has intended."""

    __slots__ = ("_conn", "_path")

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> OrderJournal:
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

    # ----------------------------------------------------------------- writes
    def record_intent(self, order: Order, *, at: datetime) -> JournalEntry:
        """Write the intent to place `order`, or return what is already recorded.

        Idempotent on `client_order_id`. Replaying the same signal after a crash
        finds the existing row and changes nothing — which is the guarantee that
        makes replay safe rather than merely likely to work.
        """
        moment = iso(ensure_utc(at))
        existing = self.get(order.client_order_id)
        if existing is not None:
            return existing

        with self._tx() as conn:
            conn.execute(
                "INSERT INTO orders (client_order_id, signal_id, strategy_id, "
                "instrument_key, state, broker_order_id, filled_qty, average_price, "
                "last_error, created_at, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order.client_order_id,
                    order.signal_id,
                    order.client_order_id.split(".")[0],
                    order.instrument.key,
                    OrderState.JOURNALLED.value,
                    None,
                    "0",
                    None,
                    "",
                    moment,
                    moment,
                    order.model_dump_json(),
                ),
            )
        recorded = self.get(order.client_order_id)
        if recorded is None:  # pragma: no cover - insert just succeeded
            raise DomainError(f"journal lost {order.client_order_id} immediately after writing it")
        return recorded

    def mark_sent(self, client_order_id: str, *, at: datetime) -> None:
        """Record that the send is about to be attempted.

        Called **before** the network call. See the module docstring — the
        ordering is the point.
        """
        self._transition(client_order_id, OrderState.SENT, at=at)

    def mark_acknowledged(
        self, client_order_id: str, broker_order_id: str, *, at: datetime
    ) -> None:
        self._transition(
            client_order_id, OrderState.ACKNOWLEDGED, at=at, broker_order_id=broker_order_id
        )

    def mark_state(
        self,
        client_order_id: str,
        state: OrderState,
        *,
        at: datetime,
        filled_qty: Decimal | None = None,
        average_price: Decimal | None = None,
        error: str = "",
        broker_order_id: str | None = None,
    ) -> None:
        self._transition(
            client_order_id,
            state,
            at=at,
            filled_qty=filled_qty,
            average_price=average_price,
            error=error,
            broker_order_id=broker_order_id,
        )

    def _transition(
        self,
        client_order_id: str,
        state: OrderState,
        *,
        at: datetime,
        filled_qty: Decimal | None = None,
        average_price: Decimal | None = None,
        error: str = "",
        broker_order_id: str | None = None,
    ) -> None:
        entry = self.get(client_order_id)
        if entry is None:
            raise DomainError(
                f"cannot move {client_order_id} to {state}: it was never journalled. "
                "Every order is written before it is sent — this means something "
                "bypassed the router."
            )
        if entry.is_terminal and state != entry.state:
            raise DomainError(
                f"{client_order_id} is already {entry.state}; refusing to move it to "
                f"{state}. A terminal order does not change state, and pretending "
                "otherwise is how a filled order gets sent twice."
            )
        with self._tx() as conn:
            conn.execute(
                "UPDATE orders SET state=?, updated_at=?, "
                "broker_order_id=COALESCE(?, broker_order_id), "
                "filled_qty=COALESCE(?, filled_qty), "
                "average_price=COALESCE(?, average_price), "
                "last_error=CASE WHEN ?='' THEN last_error ELSE ? END "
                "WHERE client_order_id=?",
                (
                    state.value,
                    iso(ensure_utc(at)),
                    broker_order_id,
                    str(filled_qty) if filled_qty is not None else None,
                    str(average_price) if average_price is not None else None,
                    error,
                    error,
                    client_order_id,
                ),
            )

    def record_fill(self, fill: Fill) -> bool:
        """Store a fill. Returns False when this fill was already recorded.

        Idempotent on `fill_id`, so a reconnect that replays the day's executions
        cannot double-count one. This is the specific mechanism behind the
        "no duplicate fills" requirement in brief §11.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO fills (fill_id, client_order_id, instrument_key, "
                "ts, payload) VALUES (?,?,?,?,?)",
                (
                    fill.fill_id,
                    fill.client_order_id,
                    fill.instrument.key,
                    iso(fill.ts),
                    fill.model_dump_json(),
                ),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ reads
    def get(self, client_order_id: str) -> JournalEntry | None:
        with closing(
            self._conn.execute(
                "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
            )
        ) as cursor:
            row = cursor.fetchone()
        return _to_entry(row) if row else None

    def open_entries(self) -> list[JournalEntry]:
        """Every order that has not reached a terminal state, oldest first."""
        placeholders = ",".join("?" for _ in TERMINAL)
        with closing(
            self._conn.execute(
                f"SELECT * FROM orders WHERE state NOT IN ({placeholders}) "
                "ORDER BY created_at, client_order_id",
                tuple(s.value for s in sorted(TERMINAL)),
            )
        ) as cursor:
            return [_to_entry(row) for row in cursor.fetchall()]

    def all_entries(self) -> list[JournalEntry]:
        with closing(
            self._conn.execute("SELECT * FROM orders ORDER BY created_at, client_order_id")
        ) as cursor:
            return [_to_entry(row) for row in cursor.fetchall()]

    def fills(self, client_order_id: str | None = None) -> list[Fill]:
        query = "SELECT payload FROM fills"
        params: tuple[str, ...] = ()
        if client_order_id is not None:
            query += " WHERE client_order_id=?"
            params = (client_order_id,)
        query += " ORDER BY ts, fill_id"
        with closing(self._conn.execute(query, params)) as cursor:
            return [Fill.model_validate_json(row["payload"]) for row in cursor.fetchall()]

    def has_fill(self, fill_id: str) -> bool:
        with closing(
            self._conn.execute("SELECT 1 FROM fills WHERE fill_id=?", (fill_id,))
        ) as cursor:
            return cursor.fetchone() is not None


def _to_entry(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        client_order_id=row["client_order_id"],
        signal_id=row["signal_id"],
        strategy_id=row["strategy_id"],
        instrument_key=row["instrument_key"],
        state=OrderState(row["state"]),
        broker_order_id=row["broker_order_id"],
        filled_qty=Decimal(row["filled_qty"]),
        average_price=Decimal(row["average_price"]) if row["average_price"] else None,
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        order=Order.model_validate(json.loads(row["payload"])),
    )
