"""Wiring the real strategy to real history, instead of to synthetic bars.

`backtest` (the Milestone 3 falsification) proves the engine's cost arithmetic on
generated data and deliberately trades nothing resembling the actual strategy.
This module is the other half: build the inputs `BacktestEngine` needs — bars, a
chain provider, an expiry table — from a directory of MCX bhavcopy files, so the
delta-strangle strategy can run against every real monthly cycle the archive has,
not zero.

## Two ticks a day, not a grid

The engine expects a chain snapshot at every bar it is given, keyed by exact
timestamp. Bhavcopy is end-of-day, so there is no 09:30 print to check the entry
gate against and no ongoing intraday grid to evaluate exits on. Rather than
fabricate one, this builds exactly **two** bars per session:

* **entry**, stamped 09:30 IST, priced from the day's **open** — the closest real
  proxy to what the strategy's fixed entry gate would have seen.
* **close**, stamped at the real session close (23:30 IST during US daylight
  saving, 23:55 otherwise — read from a `MarketCalendar`, not hardcoded), priced
  from the day's **close**, high and low.

Every exit check in between is therefore skipped, not approximated. A stop that
would have triggered mid-session and reversed by the close is invisible here. That
is a real gap, and it is why `docs/decisions.md` and the bhavcopy module both call
this a **shape test**: whether the strategy has ever come out ahead across many
real cycles, not what it would actually have been filled at.

## What is invented, and where

Bhavcopy has no bid or ask, so `assume_spread` (algo/data/bhavcopy.py) is applied
to every constructed snapshot to give the engine something to fill against — and,
same as there, only to strikes that actually traded that day. A zero-volume strike
stays unquoted rather than silently becoming tradeable.

## What a gap in the data does

A session with no futures row is dropped outright — both its bars and its chain
snapshots — exactly as `build_snapshots` drops a chain with no forward (D-084).
The position, if one is open, simply is not marked or exited that day; the next
available session picks it back up. This is a silent equity-curve jump, not a
crash, and it is reported in `BhavcopyDataset.skipped_sessions` so a run says how
much of the archive it actually used.

A session that has a futures row but is missing chain data for the specific expiry
`ctx.nearest_expiry` asks for is different: that raises `DataError` out of the
engine. It is a genuine data gap the caller cannot honestly paper over — a
strategy that thinks it holds a position but cannot price it needs to stop, not
guess — so it is left to surface rather than caught here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from algo.core.bar import Bar, Timeframe
from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Exchange
from algo.core.errors import DataError
from algo.core.instrument import FutureId, OptionId
from algo.core.quote import Quote
from algo.core.timeutil import ist_to_utc
from algo.data.bhavcopy import BhavcopyRow, assume_spread, nearest_futures_expiry
from algo.exchange.calendar import MarketCalendar
from algo.exchange.expiries import (
    ExpiryCalendar,
    ExpirySet,
    InstrumentMasterExpiries,
    LastFridayRule,
)
from algo.pricing.chain_greeks import enrich

#: The 09:30 IST close of the first 30-minute bar — the strategy's only entry
#: gate. Unaffected by US DST: MCX opens at 09:00 IST year-round (D-014 is about
#: the close, not the open).
ENTRY_TIME_IST = time(9, 30)

#: Matches the default used elsewhere for a chain built without a live quote for
#: it (algo/data/synthetic_chain.py). A placeholder, like every rate in this
#: system until it is checked against a real broker figure.
DEFAULT_RISK_FREE_RATE = 0.065

#: Cosmetic only. `Bar.timeframe` and the calendar's `bar_boundaries` count feed
#: dashboard/session reporting; nothing here trades on a 30-minute grid — see the
#: module docstring. Kept at 30 so that reporting matches the configured cadence
#: rather than inventing a second, unexplained number.
REPORTING_TIMEFRAME = Timeframe(minutes=30)


@dataclass(frozen=True)
class BhavcopyDataset:
    """Everything `BacktestEngine` needs, built from one directory of bhavcopy files."""

    bars: list[Bar]
    chain_snapshots: list[OptionChainSnapshot]
    expiries: ExpiryCalendar
    instrument: FutureId
    sessions_used: int
    skipped_sessions: list[str] = field(default_factory=list)


def build_dataset(
    rows: list[BhavcopyRow],
    *,
    symbol: str,
    calendar: MarketCalendar,
    exchange: Exchange = Exchange.MCX,
    half_spread: Decimal = Decimal("5"),
    min_volume: int = 1,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> BhavcopyDataset:
    """Turn parsed bhavcopy rows into bars, chain snapshots and an expiry table.

    `half_spread` and `min_volume` are the same assumed-spread controls as
    `algo.data.bhavcopy.assume_spread` — an explicit, visible input rather than a
    default nobody looked at, because on a thin GOLDM strangle the spread is the
    dominant cost.
    """
    resolver = nearest_futures_expiry(rows, symbol=symbol)
    expiry_table = _build_expiry_table(rows, symbol=symbol, resolver=resolver, calendar=calendar)

    futures_by_day = _futures_by_day(rows, symbol=symbol)
    options_by_day = _options_by_day(rows, symbol=symbol)
    session_days = sorted(options_by_day)

    bars: list[Bar] = []
    snapshots: list[OptionChainSnapshot] = []
    skipped: list[str] = []
    instrument: FutureId | None = None

    for day in session_days:
        futures_row = futures_by_day.get(day)
        if futures_row is None:
            skipped.append(f"{day}: no futures row, session dropped")
            continue
        if instrument is None:
            instrument = FutureId(underlying=symbol, expiry=futures_row.expiry, exchange=exchange)

        entry_ts = ist_to_utc(day, ENTRY_TIME_IST)
        close_ts = calendar.session_close(day)

        bars.append(
            Bar(
                ts=entry_ts,
                timeframe=REPORTING_TIMEFRAME,
                open=futures_row.open,
                high=futures_row.open,
                low=futures_row.open,
                close=futures_row.open,
                volume=futures_row.volume,
            )
        )
        bars.append(
            Bar(
                ts=close_ts,
                timeframe=REPORTING_TIMEFRAME,
                open=futures_row.open,
                high=futures_row.high,
                low=futures_row.low,
                close=futures_row.close,
                volume=futures_row.volume,
            )
        )

        for option_expiry, day_rows in options_by_day[day].items():
            expires_at = calendar.session_close(option_expiry)
            underlying_future = FutureId(
                underlying=symbol, expiry=resolver(option_expiry), exchange=exchange
            )
            entry_snapshot = _snapshot(
                day_rows,
                ts=entry_ts,
                underlying=symbol,
                underlying_future=underlying_future,
                option_expiry=option_expiry,
                futures_price=futures_row.open,
                price_of=lambda r: r.open,
            )
            close_snapshot = _snapshot(
                day_rows,
                ts=close_ts,
                underlying=symbol,
                underlying_future=underlying_future,
                option_expiry=option_expiry,
                futures_price=futures_row.close,
                price_of=lambda r: r.close,
            )
            for snapshot in (entry_snapshot, close_snapshot):
                priced = assume_spread(snapshot, half_spread=half_spread, min_volume=min_volume)
                snapshots.append(enrich(priced, expires_at=expires_at, r=risk_free_rate))

    if instrument is None:
        raise DataError(
            f"no usable {symbol} session found in this data: every day with option "
            f"rows was missing a matching futures row ({len(skipped)} skipped). "
            "Nothing to build a backtest from."
        )

    return BhavcopyDataset(
        bars=sorted(bars, key=lambda b: b.ts),
        chain_snapshots=snapshots,
        expiries=expiry_table,
        instrument=instrument,
        sessions_used=len(session_days) - len(skipped),
        skipped_sessions=skipped,
    )


def _build_expiry_table(
    rows: list[BhavcopyRow],
    *,
    symbol: str,
    resolver: Callable[[date], date],
    calendar: MarketCalendar,
) -> ExpiryCalendar:
    """The expiry table the engine needs, cross-checked against the last-Friday
    rule (D-023) — not trusted blindly, because the bhavcopy file is exactly the
    kind of external data that rule exists to catch an error in.

    The cross-check is advisory here (`strict=False`), not fatal: MCX rolls a
    holiday-collided expiry back to the previous trading day, and this project has
    no sourced MCX holiday calendar yet (`allow_unverified_calendar` in
    config/goldm.yaml) — so a real, holiday-driven shift would otherwise halt a
    run over a false mismatch. Once a real holiday calendar exists this should
    turn strict.
    """
    option_expiries = sorted({r.expiry for r in rows if r.symbol == symbol and r.is_option})
    table: dict[tuple[str, int, int], ExpirySet] = {
        (symbol, expiry.year, expiry.month): ExpirySet(
            option_expiry=expiry,
            futures_expiry=resolver(expiry),
        )
        for expiry in option_expiries
    }
    return ExpiryCalendar(
        authority=InstrumentMasterExpiries(table),
        rule=LastFridayRule(calendar),
        strict=False,
    )


def _futures_by_day(rows: list[BhavcopyRow], *, symbol: str) -> dict[date, BhavcopyRow]:
    """The nearest-expiry future for each session — the same pairing rule
    `algo.data.bhavcopy.futures_close` uses, kept alongside open/high/low here
    because the entry/close bars need all four, not just the close."""
    by_day: dict[date, BhavcopyRow] = {}
    for row in rows:
        if row.symbol != symbol or row.is_option:
            continue
        current = by_day.get(row.trade_date)
        if current is None or row.expiry < current.expiry:
            by_day[row.trade_date] = row
    return by_day


def _options_by_day(
    rows: list[BhavcopyRow], *, symbol: str
) -> dict[date, dict[date, list[BhavcopyRow]]]:
    by_day: dict[date, dict[date, list[BhavcopyRow]]] = {}
    for row in rows:
        if row.symbol != symbol or not row.is_option or row.strike is None or row.right is None:
            continue
        by_day.setdefault(row.trade_date, {}).setdefault(row.expiry, []).append(row)
    return by_day


def _snapshot(
    day_rows: list[BhavcopyRow],
    *,
    ts: datetime,
    underlying: str,
    underlying_future: FutureId,
    option_expiry: date,
    futures_price: Decimal,
    price_of: Callable[[BhavcopyRow], Decimal],
) -> OptionChainSnapshot:
    chain_rows = [
        ChainRow(
            option=OptionId(
                underlying_future=underlying_future,
                option_expiry=option_expiry,
                strike=row.strike,  # type: ignore[arg-type]
                right=row.right,  # type: ignore[arg-type]
            ),
            quote=Quote(
                exchange_ts=ts,
                received_ts=ts,
                ltp=price_of(row),
                volume=row.volume,
                open_interest=row.open_interest,
            ),
        )
        for row in day_rows
    ]
    chain_rows.sort(key=lambda r: (r.strike, r.right.value))
    return OptionChainSnapshot(
        ts=ts,
        underlying=underlying,
        option_expiry=option_expiry,
        futures_price=futures_price,
        rows=tuple(chain_rows),
    )
