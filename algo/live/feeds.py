"""Adapters between what the venue gives and what `LiveLoop` needs.

Two small classes, both deliberately dull. The interesting decisions live in
`LiveLoop` and `BacktestEngine.decide`; the job here is to convert without
adding judgement of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from algo.core.bar import Bar
from algo.core.clock import Clock
from algo.core.errors import DataError
from algo.core.fill import Charges, Fill
from algo.core.instrument import InstrumentId
from algo.execution.broker import Broker


class IterableBarFeed:
    """Adapts anything iterable-of-`Bar` to the `BarFeed` protocol.

    `SmartApiBarFeed` re-reads today's closed candles on each iteration, which is
    already the semantics `LiveLoop` wants - it takes the newest and ignores the
    rest. Errors are surfaced rather than swallowed: a feed that returns an empty
    list on failure is indistinguishable from a quiet market, and those two must
    not look the same to a loop deciding whether to trade.
    """

    __slots__ = ("_source",)

    def __init__(self, source: Iterable[Bar] | Callable[[], Iterable[Bar]]) -> None:
        self._source = source

    def closed_bars(self) -> Sequence[Bar]:
        source = self._source
        bars = list(source() if callable(source) else source)
        bars.sort(key=lambda b: b.ts)
        return bars


class BrokerFillFeed:
    """Fills the broker has reported since the last call.

    **Charges are zero here and that is not an oversight.** The broker's
    execution report carries a price and a quantity, not a contract note, so
    filling in a modelled charge would put an estimate into the same field a real
    one belongs in and there would be no way to tell them apart later. `Fill`
    already carries `is_modelled`; a live fill is not modelled, so its charges
    stay empty until something authoritative supplies them (Q6).

    The watermark moves only over fills actually returned. A fill arriving late,
    stamped before the watermark, would otherwise be skipped forever - so the
    cursor tracks the newest fill *seen*, and a small overlap is re-requested and
    de-duplicated by id rather than trusted to arrive in order.
    """

    __slots__ = ("_broker", "_clock", "_instruments", "_overlap", "_seen", "_since")

    def __init__(
        self,
        *,
        broker: Broker,
        clock: Clock,
        instruments: dict[str, InstrumentId],
        since: datetime | None = None,
        overlap: timedelta = timedelta(minutes=5),
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._instruments = instruments
        self._since = since if since is not None else clock.now()
        self._overlap = overlap
        self._seen: set[str] = set()

    def new_fills(self) -> Sequence[Fill]:
        snapshots = self._broker.executions(since=self._since - self._overlap)
        out: list[Fill] = []
        for snap in sorted(snapshots, key=lambda s: s.ts):
            if snap.fill_id in self._seen:
                continue
            instrument = self._instruments.get(snap.instrument_key)
            if instrument is None:
                # Refusing is the only safe option: booking a fill against the
                # wrong instrument silently corrupts the portfolio, and skipping
                # it silently leaves a real position invisible.
                raise DataError(
                    f"broker reported a fill for {snap.instrument_key}, which this "
                    "session does not know about; refusing to book it against a "
                    "guess"
                )
            self._seen.add(snap.fill_id)
            out.append(
                Fill(
                    fill_id=snap.fill_id,
                    client_order_id=snap.client_order_id,
                    signal_id="",
                    instrument=instrument,
                    side=snap.side,
                    lots=snap.lots,
                    qty=snap.qty,
                    price=snap.price,
                    ts=snap.ts,
                    charges=_NO_CHARGES,
                    slippage=Decimal("0"),
                    is_modelled=False,
                )
            )
            if snap.ts > self._since:
                self._since = snap.ts
        return out


#: A real fill's charges are not known from an execution report. See
#: `BrokerFillFeed` - this is an absence, deliberately not an estimate.
_NO_CHARGES = Charges(
    brokerage=Decimal("0"),
    ctt=Decimal("0"),
    exchange_txn=Decimal("0"),
    sebi_fee=Decimal("0"),
    stamp_duty=Decimal("0"),
    gst=Decimal("0"),
)
