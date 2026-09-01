"""Live option-chain snapshots via Kotak Neo quotes. Brief §2.8, Milestone 1.5.

Implements the `ChainFeed` interface (feed.py) the engine already speaks, so
the live path differs from backtest and paper only in which implementation is
plugged in.

**The chain is a poll, not a subscription.** `quotes()` needs only the consumer
key — no TOTP session — and is polled for the futures token plus every option
token of the expiry in question, chunked under the API's per-call limit. Each
poll yields one `OptionChainSnapshot`, aligned to the poll instant. The engine
takes the latest snapshot at a bar close — which is exactly the alignment
`ChainFeed` promises ("aligned to bar closes").

Quotes are built tolerantly: bid/ask come from the best-5 data when present;
anything unreadable leaves the row quotable-but-not-tradeable (EMPTY_BOOK)
rather than being invented. Brief §6 — never fill against a phantom price.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.clock import Clock
from algo.core.enums import Exchange, Right
from algo.core.errors import DataError, RetryableBrokerError
from algo.core.instrument import FutureId, OptionId
from algo.core.quote import DepthLevel, Quote
from algo.data.live import SessionWindow
from algo.exchange.master import InstrumentMaster, MasterRow

#: The API rejects calls asking for more tokens than this (no documented Kotak
#: cap, but the request is one URL — chunking keeps it sane and matches the
#: Angel feed's discipline).
MAX_TOKENS_PER_CALL = 50


@runtime_checkable
class KotakFeedTransport(Protocol):
    """The market-data surface of Kotak Neo. Fakes implement the same protocol.

    Returns the raw quotes payload (a bare list on success); the feed decides
    what is an error.
    """

    def quotes(self, exchange_segments: list[dict[str, str]]) -> Any: ...


class NeoQuotesTransport:
    """The real transport: wraps `NeoAPI.quotes` from the official SDK.

    Quotes authenticate on the consumer key alone — no 2FA session — so this
    transport is separate from the trade transport in `algo/execution/kotak.py`.
    """

    __slots__ = ("_api",)

    def __init__(self, consumer_key: str) -> None:
        # The SDK ships py.typed without stubs and mypy's analysis of it does
        # not see its lazy exports, so the import below is ignored explicitly.
        from neo_api_client import NeoAPI  # type: ignore[attr-defined]

        self._api = NeoAPI(consumer_key=consumer_key, environment="prod")

    def quotes(self, exchange_segments: list[dict[str, str]]) -> Any:
        return self._api.quotes(instrument_tokens=exchange_segments, quote_type="all")


class _QuoteBuilder:
    """Builds `Quote` objects from the API's market-data payloads, tolerantly.

    Every field is looked up with fallbacks and any unreadable number leaves the
    field None. `status` then classifies the row honestly (EMPTY_BOOK, STALE, ...)
    instead of us inventing a tradable price.
    """

    __slots__ = ("_clock",)

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def build(self, payload: dict[str, Any]) -> Quote:
        received = self._clock.now()
        exchange_ts = _quote_time(payload) or received

        def depth(key: str) -> tuple[DepthLevel, ...]:
            nested = payload.get("depth")
            raw = nested.get(key) if isinstance(nested, dict) else payload.get(key)
            if not isinstance(raw, list):
                return ()
            levels: list[DepthLevel] = []
            for level in raw:
                if not isinstance(level, dict):
                    continue
                price = _decimal(level.get("price"))
                quantity = _int(level.get("quantity"))
                if price is None or quantity is None:
                    continue
                levels.append(
                    DepthLevel(
                        price=price,
                        quantity=quantity,
                        orders=_int(level.get("orders") or level.get("order_count")),
                    )
                )
            return tuple(levels[:5])

        bids = depth("buy")
        asks = depth("sell")
        bid = bids[0].price if bids else _decimal(payload.get("bid"))
        ask = asks[0].price if asks else _decimal(payload.get("ask"))

        return Quote(
            exchange_ts=exchange_ts,
            received_ts=received,
            bid=bid,
            ask=ask,
            bid_qty=bids[0].quantity if bids else _int(payload.get("bid_qty")),
            ask_qty=asks[0].quantity if asks else _int(payload.get("ask_qty")),
            ltp=_decimal(payload.get("ltp") or payload.get("last_traded_price")),
            volume=_int(payload.get("volume") or payload.get("vol")) or 0,
            open_interest=_int(payload.get("open_int") or payload.get("openInterest")),
            bids=bids,
            asks=asks,
        )


class KotakChainFeed:
    """Option-chain snapshots for one expiry, polled from market data.

    One poll = one snapshot: the futures quote and every option quote are read in
    the same request cycle so `futures_price` and the rows share an instant —
    the invariant chain.py:3-7 depends on.
    """

    __slots__ = (
        "_clock",
        "_exchange",
        "_master",
        "_poll_interval_s",
        "_quote_builder",
        "_session",
        "_transport",
        "_underlying",
    )

    def __init__(
        self,
        *,
        transport: KotakFeedTransport,
        master: InstrumentMaster,
        underlying: str,
        clock: Clock,
        session: SessionWindow,
        poll_interval_s: float = 5.0,
        exchange: Exchange = Exchange.MCX,
    ) -> None:
        self._transport = transport
        self._master = master
        self._underlying = underlying
        self._clock = clock
        self._session = session
        self._poll_interval_s = poll_interval_s
        self._quote_builder = _QuoteBuilder(clock)
        self._exchange = exchange

    @property
    def underlying(self) -> str:
        return self._underlying

    def _rows_for(self, option_expiry: date) -> tuple[MasterRow, tuple[MasterRow, ...]]:
        underlying_row = self._master.future_for_option_expiry(
            self._underlying, self._exchange, option_expiry
        )
        if underlying_row is None:
            raise DataError(
                f"no {self._underlying} futures contract in the master snapshot; "
                "a chain needs the underlying's own token"
            )
        option_rows = self._master.option_rows(self._underlying, self._exchange, option_expiry)
        if not option_rows:
            raise DataError(
                f"no {self._underlying} options listed for expiry {option_expiry} "
                "in the master snapshot"
            )
        # The contract this option cycle settles into, not the front month and not
        # the farthest listed — see `InstrumentMaster.future_for_option_expiry`.
        # This row is both the chain's price anchor and the `underlying_future` of
        # every OptionId in the snapshot, so picking the wrong one moves the strikes
        # the strategy selects as well as mislabelling the instrument.
        return underlying_row, option_rows

    def poll(self, option_expiry: date) -> OptionChainSnapshot:
        """One snapshot, now.

        `snapshots` owns its own cadence and sleeps between polls, which suits a
        recorder. A trading loop already has a cadence - its bars - and must not
        be handed a second one, so it drives this instead.
        """
        underlying_row, option_rows = self._rows_for(option_expiry)
        return self._poll(underlying_row, option_rows)

    def snapshots(self, option_expiry: date) -> Iterator[OptionChainSnapshot]:
        """Poll until the session ends, yielding one snapshot per poll.

        The caller decides when to stop reading (a bar close); the iterator
        simply keeps producing snapshots while the session is live.
        """
        underlying_row, option_rows = self._rows_for(option_expiry)

        while self._session.is_live(self._clock.now()):
            yield self._poll(underlying_row, option_rows)
            time.sleep(self._poll_interval_s)

    def _poll(
        self, underlying_row: MasterRow, option_rows: tuple[MasterRow, ...]
    ) -> OptionChainSnapshot:
        assert underlying_row.expiry is not None
        ts = self._clock.now()
        rows: list[ChainRow] = []
        futures_price: Decimal | None = None
        underlying_quote_payload: dict[str, Any] | None = None
        for chunk_start in range(0, len(option_rows), MAX_TOKENS_PER_CALL - 1):
            chunk = option_rows[chunk_start : chunk_start + MAX_TOKENS_PER_CALL - 1]
            tokens = [
                {
                    "exchange_segment": "mcx_fo",
                    "instrument_token": underlying_row.symboltoken,
                }
            ]
            tokens.extend(
                {"exchange_segment": "mcx_fo", "instrument_token": row.symboltoken}
                for row in chunk
            )
            payloads = self._quotes(tokens)
            underlying_payload = _payload_for(payloads, underlying_row.symboltoken)
            if underlying_payload is None:
                raise RetryableBrokerError(
                    "market-data poll returned no quote for the underlying futures "
                    "token — the chain is incomplete and cannot be trusted"
                )
            price = _decimal(
                underlying_payload.get("ltp") or underlying_payload.get("last_traded_price")
            )
            if price is None or price <= 0:
                raise RetryableBrokerError(
                    "market-data poll returned no positive futures price; refusing "
                    "to build a chain without the anchor strike"
                )
            futures_price = price
            underlying_quote_payload = underlying_payload
            for row in chunk:
                quote_payload = _payload_for(payloads, row.symboltoken)
                if quote_payload is None:
                    continue
                right = Right.CE if row.tradingsymbol.upper().endswith("CE") else Right.PE
                assert row.expiry is not None and row.strike is not None
                quote = self._quote_builder.build(quote_payload)
                rows.append(
                    ChainRow(
                        option=_option_id(
                            self._underlying,
                            futures_expiry=underlying_row.expiry,
                            option_expiry=row.expiry,
                            strike=row.strike,
                            right=right,
                        ),
                        quote=quote,
                        iv=None,
                        delta=None,
                        priced_from=(
                            "MID" if quote.bid is not None and quote.ask is not None
                            else "LTP" if quote.ltp is not None
                            else ""
                        ),
                    )
                )
        if futures_price is None or underlying_quote_payload is None:
            raise RetryableBrokerError("market-data poll returned nothing at all")
        rows.sort(key=lambda r: (r.strike, r.right.value))
        expiry = option_rows[0].expiry
        if expiry is None:  # option_rows() filters by expiry, so this is unreachable
            raise DataError("master snapshot lists an option row without an expiry")
        return OptionChainSnapshot(
            ts=ts,
            underlying=self._underlying,
            option_expiry=expiry,
            futures_price=futures_price,
            futures_quote=self._quote_builder.build(underlying_quote_payload),
            rows=tuple(rows),
        )

    def _quotes(self, tokens: list[dict[str, str]]) -> list[dict[str, Any]]:
        try:
            payload = self._transport.quotes(tokens)
        except Exception as exc:
            raise RetryableBrokerError(f"market-data poll failed: {exc}") from exc
        if isinstance(payload, dict):
            raise RetryableBrokerError(
                "market-data poll failed: "
                f"{payload.get('Error') or payload.get('error') or payload}"
            )
        if not isinstance(payload, list):
            raise RetryableBrokerError(
                f"market-data poll returned unreadable payload: {payload!r}"
            )
        return [entry for entry in payload if isinstance(entry, dict)]


def _payload_for(payloads: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """Find a quote by its token, tolerating the `SEGMENT|token` prefix the API
    sometimes writes into `exchange_token`."""
    for entry in payloads:
        exchange_token = str(entry.get("exchange_token") or "")
        if exchange_token == token or exchange_token.endswith(f"|{token}"):
            return entry
    return None


def _option_id(
    underlying: str,
    *,
    futures_expiry: date,
    option_expiry: date,
    strike: Decimal,
    right: Right,
) -> OptionId:
    # `futures_expiry` and `option_expiry` are deliberately separate dates -
    # the option expires first - per `algo/core/instrument.py`'s own warning
    # against conflating them; `futures_expiry` must come from the real
    # futures contract row, never from the option's own expiry.
    return OptionId(
        underlying_future=FutureId(underlying=underlying, expiry=futures_expiry),
        option_expiry=option_expiry,
        strike=strike,
        right=right,
    )


def _quote_time(payload: dict[str, Any]) -> datetime | None:
    for key in ("lstup_time", "exchange_timestamp", "exch_ts", "timestamp"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            try:
                return datetime.fromtimestamp(float(value), tz=UTC)
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromtimestamp(float(value), tz=UTC)
            except (OverflowError, OSError, ValueError):
                continue
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None
