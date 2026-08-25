"""Wiring the real strategy to real, recent SmartAPI history.

`algo/backtest/bhavcopy_runner.py` gives ~100 past monthly cycles at two ticks a
day. This gives real 30-minute bars - genuine intrabar movement, not a daily
proxy - but only for **one cycle**: whichever option contract is currently
listed and not yet expired. Angel One's own candle API cannot serve a contract
that has already expired (see `algo/data/bhavcopy.py`'s module docstring for
the evidence), so a wider window is structurally impossible here. The two
sources are complements, not alternatives: bhavcopy answers "has this ever
worked, across many cycles"; this answers "what would this month's trade
actually have looked like, at real intraday resolution".

## Verified, not assumed, thin liquidity away from the front month

Pulled real candles on 2026-08-25 to check this before writing a line of the
merge logic: a near-the-money strike on the then-front-month (28 Aug) contract
came back with ~131 of 133 thirty-minute bars genuinely traded (real O/H/L/C
movement, real volume, every bar). The *same style* strike one month out (25
Sep, not yet front month) came back essentially flat - identical open, high,
low and close on almost every bar, near-zero volume. Liquidity concentrates
entirely in the current cycle; there is no benefit to fetching anything else.

## One request per contract, and that is the constraint that shapes this module

The candle API is per-instrument. A full chain snapshot at every bar needs one
`fetch_bar_history` call per strike per side - the GOLDM 28 Aug ladder alone
lists 133 strikes (266 contracts). Fetching all of them, sequentially, against
a live broker session, is neither fast nor courteous to a real rate limit. So:

* The strikes actually fetched are bounded to a **band** around the price range
  the underlying actually traded across the window, padded by `strike_band_pct`
  - not the whole ladder. A 0.25-delta wing sits several percent from spot, so
  the padding has to clear that gap; it does not have to clear the whole board.
* Calls are paced with `rate_limit_s` between them, and a bounded retry with
  backoff on `RetryableBrokerError` - the network failures the SDK itself
  raises for exactly this kind of throttling.
* `max_contracts` refuses to proceed past a count nobody chose on purpose,
  rather than silently spending several minutes and a chunk of a real account's
  rate-limit budget because a wide band times a long window multiplied out.

## What is still invented, same as bhavcopy

The candle API returns trade prices, not a book. `assume_spread` (from
`algo.data.bhavcopy`, generic over any `OptionChainSnapshot` - built there,
reused here rather than duplicated) still has to synthesise a bid/ask before
anything is tradeable, and still only for strikes that actually printed volume
in that bar. That part of the picture has not changed; only the granularity of
what is being assumed *from* has.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from algo.core.bar import Bar, Timeframe
from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Exchange
from algo.core.errors import DataError, RetryableBrokerError
from algo.core.instrument import FutureId, OptionId
from algo.core.quote import Quote
from algo.data.bhavcopy import assume_spread
from algo.data.smartapi_feed import CandleTransport, fetch_bar_history
from algo.exchange.calendar import MarketCalendar
from algo.exchange.expiries import (
    ExpiryCalendar,
    ExpirySet,
    InstrumentMasterExpiries,
    LastFridayRule,
)
from algo.exchange.master import InstrumentMaster, MasterRow, right_of
from algo.pricing.chain_greeks import enrich

#: Default padding beyond the underlying's observed high/low over the window.
#: A 0.25-delta wing sits several percent from spot; this has to clear that gap
#: with room to spare, not hug the money.
DEFAULT_STRIKE_BAND_PCT = Decimal("0.10")

#: A conservative pace for Angel One's historical-candle endpoint. Not sourced
#: from published documentation - chosen to be comfortably slow rather than
#: discovered by throttling a real account.
DEFAULT_RATE_LIMIT_S = 0.35

#: Refuses to proceed past this many contracts without the caller raising it on
#: purpose. At the default pace, 120 contracts is already ~45 seconds of
#: sequential real API calls.
DEFAULT_MAX_CONTRACTS = 120

DEFAULT_RISK_FREE_RATE = 0.065

#: How many times a throttled or transient call is retried before giving up,
#: and the backoff between attempts. Safe here in a way it is not for order
#: placement (brief's router never auto-retries, D-056/D-092): a candle read
#: has no side effect, so retrying it is just patience, not risk.
_RETRY_BACKOFFS_S = (1.0, 3.0, 8.0)


@dataclass(frozen=True)
class SmartApiDataset:
    """Everything `BacktestEngine` needs, built from real SmartAPI history."""

    bars: list[Bar]
    chain_snapshots: list[OptionChainSnapshot]
    expiries: ExpiryCalendar
    instrument: FutureId
    contracts_fetched: int
    contracts_skipped_empty: list[str] = field(default_factory=list)


def _with_retry(label: str, call: Callable[[], list[Bar]]) -> list[Bar]:
    last_exc: RetryableBrokerError | None = None
    for backoff in (0.0, *_RETRY_BACKOFFS_S):
        if backoff:
            time.sleep(backoff)
        try:
            return call()
        except RetryableBrokerError as exc:
            last_exc = exc
            continue
    raise DataError(
        f"{label}: candle API kept failing after {len(_RETRY_BACKOFFS_S) + 1} "
        f"attempts ({last_exc})"
    ) from last_exc


def build_dataset(
    transport: CandleTransport,
    master: InstrumentMaster,
    *,
    symbol: str,
    option_expiry: date,
    calendar: MarketCalendar,
    since: datetime,
    until: datetime,
    exchange: Exchange = Exchange.MCX,
    timeframe: Timeframe | None = None,
    strike_band_pct: Decimal = DEFAULT_STRIKE_BAND_PCT,
    rate_limit_s: float = DEFAULT_RATE_LIMIT_S,
    max_contracts: int = DEFAULT_MAX_CONTRACTS,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    on_progress: Callable[[str], None] | None = None,
) -> SmartApiDataset:
    """Real bars and a real chain, for one currently-listed GOLDM cycle.

    `since`/`until` are UTC instants; they should not reach earlier than when
    this `option_expiry` was actually first listed, or every request in that
    dead stretch returns nothing and the run is slower for no benefit.
    """
    from algo.core.bar import M30

    tf = timeframe or M30
    report = on_progress or (lambda _msg: None)

    future_row = _nearest_future(master, symbol, exchange, on_or_after=option_expiry)
    # future_rows() only ever returns rows with a real expiry (algo/exchange/master.py);
    # asserted rather than trusted silently, and it narrows the type for mypy.
    assert future_row.expiry is not None
    future = FutureId(underlying=symbol, expiry=future_row.expiry, exchange=exchange)

    report(f"fetching {symbol} futures bars ({future_row.tradingsymbol})")
    futures_bars = _with_retry(
        "futures",
        lambda: fetch_bar_history(
            transport, master, future, timeframe=tf, since=since, until=until, exchange=exchange
        ),
    )
    if not futures_bars:
        raise DataError(
            f"no {future_row.tradingsymbol} bars in [{since}, {until}] - nothing to build a "
            "chain against (options are priced off the future; without it every delta "
            "would be invented)"
        )

    lo = min(b.low for b in futures_bars)
    hi = max(b.high for b in futures_bars)
    band_lo = lo * (1 - strike_band_pct)
    band_hi = hi * (1 + strike_band_pct)

    candidates = [
        row
        for row in master.option_rows(symbol, exchange, option_expiry)
        if row.strike is not None and band_lo <= row.strike <= band_hi
    ]
    if not candidates:
        raise DataError(
            f"no listed strikes fall inside [{band_lo:.0f}, {band_hi:.0f}] "
            f"(underlying traded [{lo}, {hi}], padded {strike_band_pct:%}) - "
            "widen strike_band_pct"
        )
    if len(candidates) > max_contracts:
        raise DataError(
            f"{len(candidates)} contracts fall inside the strike band - more than "
            f"max_contracts={max_contracts}. Narrow strike_band_pct, shorten the date "
            "range, or raise max_contracts on purpose."
        )

    report(
        f"fetching {len(candidates)} option contracts across "
        f"[{band_lo:.0f}, {band_hi:.0f}] (rate-limited to {rate_limit_s}s apart)"
    )
    by_ts: dict[datetime, dict[OptionId, Bar]] = {}
    skipped_empty: list[str] = []
    for index, row in enumerate(candidates):
        if index:
            time.sleep(rate_limit_s)
        option = _option_id(future, option_expiry, row, exchange)
        if option is None:
            continue
        # `_with_retry` calls this immediately, in this same iteration - never
        # stored for later - so closing over the loop variables is safe here.
        def _fetch(option: OptionId = option) -> list[Bar]:
            return fetch_bar_history(
                transport, master, option, timeframe=tf, since=since, until=until, exchange=exchange
            )

        bars = _with_retry(row.tradingsymbol, _fetch)
        if not bars:
            skipped_empty.append(row.tradingsymbol)
            continue
        for bar in bars:
            by_ts.setdefault(bar.ts, {})[option] = bar
        if (index + 1) % 20 == 0 or index + 1 == len(candidates):
            report(f"  {index + 1}/{len(candidates)} contracts fetched")

    futures_by_ts = {b.ts: b for b in futures_bars}
    expires_at = calendar.session_close(option_expiry)
    snapshots: list[OptionChainSnapshot] = []
    for ts, future_price in ((b.ts, b.close) for b in futures_bars):
        legs = by_ts.get(ts)
        if not legs:
            continue
        rows = [
            ChainRow(
                option=opt,
                quote=Quote(
                    exchange_ts=ts,
                    received_ts=ts,
                    ltp=bar.close,
                    volume=bar.volume,
                ),
            )
            for opt, bar in legs.items()
        ]
        rows.sort(key=lambda r: (r.strike, r.right.value))
        snapshot = OptionChainSnapshot(
            ts=ts,
            underlying=symbol,
            option_expiry=option_expiry,
            futures_price=future_price,
            rows=tuple(rows),
        )
        priced = assume_spread(snapshot, half_spread=Decimal("5"))
        snapshots.append(enrich(priced, expires_at=expires_at, r=risk_free_rate))
    del futures_by_ts  # only ts/close were needed, already captured above

    expiries = ExpiryCalendar(
        authority=InstrumentMasterExpiries(
            {
                (symbol, option_expiry.year, option_expiry.month): ExpirySet(
                    option_expiry=option_expiry, futures_expiry=future_row.expiry
                )
            }
        ),
        rule=LastFridayRule(calendar),
        strict=False,  # no sourced MCX holiday calendar yet - see D-094
    )

    return SmartApiDataset(
        bars=futures_bars,
        chain_snapshots=snapshots,
        expiries=expiries,
        instrument=future,
        contracts_fetched=len(candidates) - len(skipped_empty),
        contracts_skipped_empty=skipped_empty,
    )


def _nearest_future(
    master: InstrumentMaster, symbol: str, exchange: Exchange, *, on_or_after: date
) -> MasterRow:
    later = [
        r for r in master.future_rows(symbol, exchange) if r.expiry and r.expiry >= on_or_after
    ]
    if later:
        return min(later, key=lambda r: r.expiry)  # type: ignore[arg-type,return-value]
    all_futures = master.future_rows(symbol, exchange)
    if not all_futures:
        raise DataError(f"no {symbol} futures contracts listed on {exchange}")
    return max(all_futures, key=lambda r: r.expiry)  # type: ignore[arg-type,return-value]


def _option_id(
    future: FutureId, option_expiry: date, row: MasterRow, exchange: Exchange
) -> OptionId | None:
    right = right_of(row.tradingsymbol)
    if right is None or row.strike is None:
        return None
    return OptionId(
        underlying_future=future,
        option_expiry=option_expiry,
        strike=row.strike,
        right=right,
        exchange=exchange,
    )
