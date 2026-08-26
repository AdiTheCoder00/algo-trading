"""The live option chain, priced and greeked, for the trading loop.

`KotakChainFeed` returns quotes and nothing else - every row comes back with
`iv=None, delta=None`, because a market-data poll carries prices, not greeks.
`DeltaStrangle` selects strikes *by delta*, so a chain handed to it unenriched
would match no strike at all and the strategy would silently never trade. This
module is the piece between them.

## It answers two questions from one poll

The engine asks a chain provider "what does the ladder look like" and a price
source "what is this leg worth". In a backtest those are two objects built from
the same list of snapshots. Live, they must come from the *same poll* or the
strategy could choose a strike from one instant and be marked at another, so one
object implements both.

## Staleness is refused, not tolerated

`ChainPriceSource` keys marks by exact timestamp, which works when snapshots are
built at bar closes and cannot work when they arrive whenever a poll returns. So
this answers with the most recent snapshot instead - and refuses once that
snapshot is older than `max_staleness_s`.

Returning None makes `require_mark` raise and the loop stop, which is the
intended behaviour: `prices.py` already argues that marking a missing price at
zero "would show a short option as fully profitable on exactly the bar the feed
dropped out - the most dangerous possible direction for that error". A stale
price is the same error with a smaller number on it.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from algo.core.chain import OptionChainSnapshot
from algo.core.errors import DataError
from algo.core.timeutil import ist_to_utc
from algo.data.kotak_feed import KotakChainFeed
from algo.pricing.chain_greeks import enrich

#: MCX's evening close in IST. Options settle at the session close on their
#: expiry date, and Black-76 needs that instant to compute time to expiry.
_EXPIRY_CLOSE_IST = time(23, 30)


class LiveChainProvider:
    """A polled option chain, enriched with greeks. Chain provider and price source."""

    __slots__ = (
        "_feed",
        "_last",
        "_max_staleness_s",
        "_prices",
        "_risk_free_rate",
    )

    def __init__(
        self,
        *,
        feed: KotakChainFeed,
        risk_free_rate: float = 0.065,
        max_staleness_s: float = 120.0,
    ) -> None:
        self._feed = feed
        self._risk_free_rate = risk_free_rate
        self._max_staleness_s = max_staleness_s
        self._last: OptionChainSnapshot | None = None
        self._prices: dict[str, Decimal] = {}

    @property
    def latest(self) -> OptionChainSnapshot | None:
        return self._last

    def refresh(self, option_expiry: date) -> OptionChainSnapshot:
        """Poll once, enrich, and keep it as the current view.

        Driven by the loop, once per bar - not on every `chain_at` call. A single
        `decide` asks for the chain more than once (the strategy, then the
        dashboard snapshot, then each mark), and polling per question would both
        hammer the venue and let two answers inside one decision disagree.
        """
        snapshot = self._feed.poll(option_expiry)
        enriched = enrich(
            snapshot,
            expires_at=ist_to_utc(option_expiry, _EXPIRY_CLOSE_IST),
            r=self._risk_free_rate,
        )
        self._last = enriched
        # The mid, not the last trade - the same choice `ChainPriceSource` makes,
        # and for the same reason: a stale print must not set the price on an
        # illiquid strike, which is exactly the strike this strategy sells.
        self._prices = {}
        for row in enriched.rows:
            reference = row.quote.mid if row.quote.mid is not None else row.quote.ltp
            if reference is not None and reference > 0:
                self._prices[row.option.key] = reference
        return enriched

    # ------------------------------------------------------- chain provider
    def chain_at(
        self, underlying: str, option_expiry: date, ts: datetime
    ) -> OptionChainSnapshot | None:
        snapshot = self._fresh(ts)
        if snapshot is None:
            return None
        if snapshot.underlying != underlying or snapshot.option_expiry != option_expiry:
            # A different cycle than the one polled. Returning the wrong chain
            # would have the strategy select strikes in an expiry it did not ask
            # about, so this is None - "no chain for that", not "here is one".
            return None
        return snapshot

    # ---------------------------------------------------------- price source
    def mark(self, key: str, ts: datetime) -> Decimal | None:
        return self._prices.get(key) if self._fresh(ts) is not None else None

    def fill_reference(self, key: str, ts: datetime) -> Decimal | None:
        return self.mark(key, ts)

    # ----------------------------------------------------------------- guts
    def _fresh(self, ts: datetime) -> OptionChainSnapshot | None:
        """The current snapshot, or None once it is too old to price against."""
        if self._last is None:
            return None
        age = abs((ts - self._last.ts).total_seconds())
        if age > self._max_staleness_s:
            return None
        return self._last

    def require_fresh(self, ts: datetime) -> OptionChainSnapshot:
        """Like `_fresh`, but says why when there is nothing usable.

        For the loop's own check before it decides: a caller that wants to stop
        on a stale chain needs the reason, not a None it has to interpret.
        """
        snapshot = self._fresh(ts)
        if snapshot is not None:
            return snapshot
        if self._last is None:
            raise DataError("no chain has been polled yet")
        age = abs((ts - self._last.ts).total_seconds())
        raise DataError(
            f"the last chain poll is {age:.0f}s old at {ts}, past the "
            f"{self._max_staleness_s:.0f}s limit; refusing to price against it"
        )
