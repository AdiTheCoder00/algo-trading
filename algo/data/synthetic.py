"""Deterministic synthetic bars, for proving the engine.

Brief §9 Milestone 1: "Prove correctness on synthetic data." These fixtures exist
to make the arithmetic checkable against a hand-computed answer — a session with a
known bar count, a series with a known high, a gap in a known place.

They prove the engine is right. They prove **nothing** about the strategy: a
generator always offers a fill and never shows a book that empties when you need
it. Anything produced from this module is labelled SYNTHETIC wherever it is
reported (assumption 6.1).

Everything here is seeded and integer-based, so two runs on two machines produce
byte-identical output — which is what makes the determinism test in §7.4 mean
something.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from algo.core.bar import M1, Bar, Timeframe
from algo.exchange.calendar import MarketCalendar


def one_minute_session(
    calendar: MarketCalendar,
    session_day: date,
    *,
    start_price: Decimal = Decimal("156640"),
    tick: Decimal = Decimal("0.50"),
    seed: int = 0,
    drift_ticks: int = 0,
    volume_per_bar: int = 10,
) -> list[Bar]:
    """One session of 1-minute bars, on the tick grid, as a seeded random walk.

    Prices are generated as integer tick counts and only then multiplied by the
    tick size, so every price is exactly on the grid by construction rather than
    by rounding after the fact.
    """
    opened = calendar.session_open(session_day)
    closed = calendar.session_close(session_day)
    minutes = int((closed - opened).total_seconds() // 60)

    rng = random.Random(f"{seed}:{session_day.isoformat()}")
    level = int(start_price / tick)

    bars: list[Bar] = []
    for i in range(1, minutes + 1):
        step = rng.choice((-2, -1, 0, 1, 2)) + drift_ticks
        opening = level
        closing = level + step
        wick = rng.randint(0, 2)
        high = max(opening, closing) + wick
        low = min(opening, closing) - wick
        bars.append(
            Bar(
                ts=opened + timedelta(minutes=i),
                timeframe=M1,
                open=opening * tick,
                high=high * tick,
                low=low * tick,
                close=closing * tick,
                volume=volume_per_bar,
            )
        )
        level = closing
    return bars


def one_minute_range(
    calendar: MarketCalendar,
    start: date,
    end: date,
    *,
    seed: int = 0,
    start_price: Decimal = Decimal("156640"),
) -> list[Bar]:
    """1-minute bars across every trading day in `[start, end]`."""
    bars: list[Bar] = []
    price = start_price
    for day in calendar.trading_days(start, end):
        session = one_minute_session(calendar, day, start_price=price, seed=seed)
        bars.extend(session)
        price = session[-1].close
    return bars


def flat_session(
    calendar: MarketCalendar,
    session_day: date,
    *,
    price: Decimal = Decimal("100"),
    timeframe: Timeframe = M1,
) -> list[Bar]:
    """A session where nothing moves. Used where the expected answer must be exact.

    A random walk is fine for shape tests, but for "does this aggregate correctly"
    a constant series makes the expected high, low, open and close obvious by
    inspection — which is the point of a fixture.
    """
    opened = calendar.session_open(session_day)
    closed = calendar.session_close(session_day)
    minutes = int((closed - opened).total_seconds() // 60)
    return [
        Bar(
            ts=opened + timedelta(minutes=i),
            timeframe=timeframe,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1,
        )
        for i in range(1, minutes + 1)
    ]
