"""The Dukascopy tick archive reader.

The archive on disk is the only source of XAUUSD history long enough to say
anything about a five-minute strategy across regimes, and nothing in the file
format is self-describing: the month in the path is zero-indexed, the body is
raw LZMA with no container, and prices are integers in points. Every one of
those is a way to be quietly wrong by a month, an hour or a factor of a
thousand, so each has a test that pins it.

These tests came with `algo/data/dukascopy.py` from the study that first wrote
it (`claude/xauusd-strategy-backtest-afe122`); they are lifted here rather than
rewritten, so the module arrives with the coverage it already had.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from algo.core.bar import Bar, Timeframe
from algo.data.dukascopy import (
    Flow,
    Tick,
    _bucket_close,
    bars_from_ticks,
    bars_with_microstructure,
    bars_with_spread,
    decode_path,
    load_flow,
    regrid_bars,
    save_flow,
)

M5 = Timeframe(minutes=5)
H1 = Timeframe(minutes=60)


def _series(*, count: int, start: Decimal, step: Decimal) -> list[Bar]:
    """A rising five-minute ramp, for the regridding checks."""
    base = datetime(2024, 1, 2, 0, 5, tzinfo=UTC)
    bars = []
    price = start
    for i in range(count):
        close = price + step
        bars.append(
            Bar(
                ts=base + timedelta(minutes=5 * i),
                timeframe=M5,
                open=price,
                high=max(price, close) + Decimal("0.2"),
                low=min(price, close) - Decimal("0.2"),
                close=close,
                volume=10,
            )
        )
        price = close
    return bars


def test_dukascopy_paths_are_zero_indexed_by_month() -> None:
    assert decode_path(Path("XAUUSD/2024/00/02/13h_ticks.bi5")) == datetime(
        2024, 1, 2, 13, tzinfo=UTC
    )
    assert decode_path(Path("XAUUSD/2024/11/31/23h_ticks.bi5")) == datetime(
        2024, 12, 31, 23, tzinfo=UTC
    )


def test_a_tick_on_the_boundary_closes_the_bar_it_ends() -> None:
    """`(open, close]` - a tick at 10:05:00.000 belongs to the bar stamped
    10:05, not to the one stamped 10:10."""
    assert _bucket_close(
        datetime(2024, 1, 2, 10, 5, tzinfo=UTC), timedelta(minutes=5)
    ) == datetime(2024, 1, 2, 10, 5, tzinfo=UTC)
    assert _bucket_close(
        datetime(2024, 1, 2, 10, 5, 0, 1000, tzinfo=UTC), timedelta(minutes=5)
    ) == datetime(2024, 1, 2, 10, 10, tzinfo=UTC)


def test_regridding_preserves_the_extremes() -> None:
    m5 = _series(count=240, start=Decimal("2000"), step=Decimal("0.3"))
    h1 = regrid_bars(m5, H1)
    assert len(h1) == 20
    assert h1[0].open == m5[0].open
    assert h1[0].close == m5[11].close
    assert h1[0].high == max(b.high for b in m5[:12])
    assert h1[0].low == min(b.low for b in m5[:12])


def test_bars_from_ticks_and_regridding_agree() -> None:
    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    ticks = [
        Tick(
            ts=base + timedelta(seconds=30 * i),
            bid=Decimal(2000 + (i % 11)),
            ask=Decimal(2000 + (i % 11)) + Decimal("0.4"),
        )
        for i in range(1, 2000)
    ]
    direct = bars_from_ticks(ticks, H1)
    stepped = regrid_bars(bars_from_ticks(ticks, M5), H1)
    assert [(b.ts, b.open, b.high, b.low, b.close) for b in direct] == [
        (b.ts, b.open, b.high, b.low, b.close) for b in stepped
    ]


def test_flow_counts_mid_moves_and_survives_a_round_trip(tmp_path: Path) -> None:
    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    # Up, up, flat, down: two upticks, one downtick, one tick that moved nothing.
    mids = [Decimal("2000"), Decimal("2001"), Decimal("2002"), Decimal("2002"), Decimal("2001")]
    ticks = [
        Tick(ts=base + timedelta(seconds=30 * i), bid=mid, ask=mid + Decimal("0.4"))
        for i, mid in enumerate(mids, start=1)
    ]
    bars, spreads, flows = bars_with_microstructure(ticks, M5)
    assert len(bars) == len(spreads) == len(flows) == 1
    assert flows[0].ticks == 5
    assert flows[0].upticks == 2
    assert flows[0].downticks == 1
    assert flows[0].imbalance == pytest.approx((2 - 1) / 3)
    assert spreads[0] == Decimal("0.4")

    path = save_flow(bars, flows, tmp_path / "flow.csv")
    restored = load_flow(path)
    assert restored[bars[0].ts] == flows[0]


def test_a_flat_bar_has_no_imbalance_rather_than_a_division_by_zero() -> None:
    assert Flow(ticks=100, upticks=0, downticks=0).imbalance == 0.0


def test_microstructure_and_spread_builders_agree_on_bars() -> None:
    """Two entry points over the same ticks must not disagree about the bars."""
    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    ticks = [
        Tick(
            ts=base + timedelta(seconds=20 * i),
            bid=Decimal(2000 + (i % 13)),
            ask=Decimal(2000 + (i % 13)) + Decimal("0.4"),
        )
        for i in range(1, 400)
    ]
    plain_bars, plain_spreads = bars_with_spread(ticks, M5)
    micro_bars, micro_spreads, _flows = bars_with_microstructure(ticks, M5)
    assert [b.ts for b in plain_bars] == [b.ts for b in micro_bars]
    assert [(b.open, b.high, b.low, b.close) for b in plain_bars] == [
        (b.open, b.high, b.low, b.close) for b in micro_bars
    ]
    assert plain_spreads == micro_spreads


def test_bars_are_the_mid_of_bid_and_ask() -> None:
    """The convention the cost model depends on.

    A bid bar would put half the spread inside every long entry price *and*
    charge it again as a cost. `bars_with_spread` returns the mid and reports
    the spread separately so it is charged exactly once.
    """
    base = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    ticks = [
        Tick(ts=base + timedelta(seconds=60), bid=Decimal("2000"), ask=Decimal("2000.50")),
        Tick(ts=base + timedelta(seconds=120), bid=Decimal("2001"), ask=Decimal("2001.50")),
    ]
    bars, spreads = bars_with_spread(ticks, M5)
    assert len(bars) == 1
    assert bars[0].open == Decimal("2000.25")
    assert bars[0].close == Decimal("2001.25")
    assert spreads[0] == Decimal("0.50")
