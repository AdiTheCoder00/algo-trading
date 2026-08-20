"""Live bars via Angel One SmartAPI. Brief §2.8, Milestone 1.5.

Implements the `BarFeed` interface (feed.py), so the live path differs from
backtest and paper only in which implementation is plugged in. SmartAPI's job
in this engine is **closed bars only** — live trading (orders, chain, master)
runs on Kotak Neo; SmartAPI serves the candle API that Kotak lacks.

**Bars come from the candle API, not the WebSocket.** The candle endpoint returns
*closed* bars only if we ask for a window that ended before now — which keeps the
"feeds yield closed bars, strictly increasing" guarantee (feed.py:7) true by
construction. A WebSocket tick stream would be aggregated by `TickBarBuilder`
(`algo/data/live.py`), which is pure and unit-testable, and which only ever
emits a bar once its interval has closed.

The SmartAPI session, credentials and transport also live here: the session's
only remaining purpose is to serve candles, so it sits with its consumer rather
than in the (now Kotak-only) execution layer.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from algo.core.bar import Bar, Timeframe
from algo.core.clock import Clock
from algo.core.enums import Exchange
from algo.core.errors import DataError, RetryableBrokerError
from algo.core.instrument import InstrumentId
from algo.core.timeutil import IST
from algo.data.live import SessionWindow
from algo.exchange.master import InstrumentMaster

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class SmartApiCredentials(BaseModel):
    """The four values a SmartAPI session needs. Loaded from env, never config."""

    model_config = _FROZEN

    api_key: str
    client_id: str
    password: str
    totp_seed: str

    def has_all(self) -> bool:
        return all(
            (self.api_key, self.client_id, self.password, self.totp_seed)
        )

    def missing(self) -> tuple[str, ...]:
        """Which required credentials are absent, by env-var name."""
        required = {
            "ALGO_SMARTAPI_API_KEY": self.api_key,
            "ALGO_SMARTAPI_CLIENT_ID": self.client_id,
            "ALGO_SMARTAPI_PASSWORD": self.password,
            "ALGO_SMARTAPI_TOTP_SEED": self.totp_seed,
        }
        return tuple(name for name, value in required.items() if not value)


def credentials_from_env(env: dict[str, str] | None = None) -> SmartApiCredentials:
    """Read `ALGO_SMARTAPI_*` from the environment.

    Deliberately separate from the hashed config (schema.py): credentials must
    never flow into `config_hash`, which is stamped into every signal id and run
    artefact.
    """
    import os

    source = os.environ if env is None else env

    def pick(name: str) -> str:
        return source.get(f"ALGO_SMARTAPI_{name}", "")

    return SmartApiCredentials(
        api_key=pick("API_KEY"),
        client_id=pick("CLIENT_ID"),
        password=pick("PASSWORD"),
        totp_seed=pick("TOTP_SEED"),
    )


@runtime_checkable
class CandleTransport(Protocol):
    """The market-data surface SmartAPI exposes to the bar feed.

    The chain and broker lived on the same SmartAPI session once; they are
    Kotak's now, and this protocol is exactly what candles need and nothing
    more. `SmartConnectTransport` satisfies it; fakes implement it too.
    """

    def candles(self, params: dict[str, Any]) -> dict[str, Any]: ...


class SmartConnectTransport:
    """The real transport: wraps `SmartConnect` from the official SDK."""

    __slots__ = ("_api",)

    def __init__(self, api_key: str) -> None:
        from SmartApi import SmartConnect

        self._api = SmartConnect(api_key)

    def connect(self, client_id: str, password: str, totp: str) -> None:
        """Generate the session; needed before any candle call."""
        self._api.generateSession(client_id, password, totp)

    def candles(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._api.getCandleData(params)
        if response is None:
            raise RetryableBrokerError("candle call returned nothing")
        return {str(k): v for k, v in response.items()}


class SmartApiBarFeed:
    """Closed 30-minute bars for one instrument, from the candle API.

    Yields bars for `[since, until)`, dropping the final in-progress candle by
    requesting a window that ends `minutes` before `until` — the candle API
    includes the still-running candle if you ask for it, and a partial candle is
    not a closed bar.
    """

    __slots__ = (
        "_clock",
        "_exchange",
        "_instrument",
        "_master",
        "_session",
        "_timeframe",
        "_transport",
    )

    def __init__(
        self,
        *,
        transport: CandleTransport,
        master: InstrumentMaster,
        instrument: InstrumentId,
        timeframe: Timeframe,
        clock: Clock,
        session: SessionWindow,
        exchange: Exchange = Exchange.MCX,
    ) -> None:
        self._transport = transport
        self._master = master
        self._instrument = instrument
        self._timeframe = timeframe
        self._clock = clock
        self._session = session
        self._exchange = exchange

    @property
    def instrument(self) -> InstrumentId:
        return self._instrument

    @property
    def timeframe(self) -> Timeframe:
        return self._timeframe

    def __iter__(self) -> Iterator[Bar]:
        try:
            row = self._master.row_for(self._instrument)
        except DataError as exc:
            raise DataError(f"bar feed: {exc}") from exc

        now = self._clock.now()
        session_day = self._session.day_for(now)
        until = min(
            now - timedelta(minutes=self._timeframe.minutes),
            self._session.close_at(session_day),
        )
        since = self._session.open_at(session_day)
        if until <= since:
            return

        try:
            response = self._transport.candles(
                {
                    "exchange": self._exchange.value,
                    "symboltoken": row.symboltoken,
                    "interval": f"{self._timeframe.minutes}_MINUTE",
                    "fromdate": since.strftime("%Y-%m-%d %H:%M"),
                    "todate": until.strftime("%Y-%m-%d %H:%M"),
                }
            )
        except Exception as exc:
            raise RetryableBrokerError(f"candle API failed: {exc}") from exc
        if not response.get("status", False):
            raise RetryableBrokerError(
                f"candle API failed: "
                f"{response.get('message') or response.get('errorcode') or 'no message'}"
            )
        raw_bars = response.get("data")
        if not isinstance(raw_bars, list) or not raw_bars:
            return

        # The API returns ascending order; enforce it on the raw response —
        # a reordered feed is exactly the silent look-ahead the engine's
        # checks exist for, and sorting would paper over it.
        previous = None
        for raw in raw_bars:
            ts_raw = raw[0] if isinstance(raw, (list, tuple)) and raw else None
            if previous is not None and ts_raw is not None and str(ts_raw) < str(previous):
                raise DataError("candle API returned non-increasing timestamps")
            if ts_raw is not None:
                previous = ts_raw

        bars: list[Bar] = []
        for raw in raw_bars:
            bar = _bar_from_candle(raw, self._timeframe, partial=False)
            if bar is not None and since < bar.ts <= until:
                bars.append(bar)
        if not bars:
            return
        bars.sort(key=lambda b: b.ts)
        yield from bars


def _bar_from_candle(raw: Any, timeframe: Timeframe, *, partial: bool) -> Bar | None:
    """One `[ts, open, high, low, close, volume]` row -> a Bar, or None.

    The candle API labels bars in IST wall time; the engine holds UTC, so the
    label is converted through the named zone.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 5:
        return None
    try:
        # No timezone in the format; IST is attached deliberately.
        naive = datetime.fromisoformat(str(raw[0]))
    except ValueError:
        return None
    ts = naive.replace(tzinfo=IST).astimezone(UTC)
    open_v = _decimal(raw[1])
    high_v = _decimal(raw[2])
    low_v = _decimal(raw[3])
    close_v = _decimal(raw[4])
    if open_v is None or high_v is None or low_v is None or close_v is None:
        return None
    volume = _int(raw[5]) or 0 if len(raw) > 5 else 0
    if low_v > high_v:
        return None
    return Bar(
        ts=ts,
        timeframe=timeframe,
        open=open_v,
        high=high_v,
        low=low_v,
        close=close_v,
        volume=volume,
        is_partial=partial,
    )


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
        return int(value)
    except (TypeError, ValueError):
        return None
