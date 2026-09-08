"""Shape test: 50 EMA + Bollinger bands on real XAUUSD bars, both readings.

`algo/strategy/ema_bb.py` implements the two incompatible things people mean by
"the 50 EMA and Bollinger band strategy" - `pullback` fades a band touch inside
the trend, `breakout` follows one. This runs both, so the comparison is measured
rather than assumed.

## Why this one goes through `run_cfd_backtest`

`measure_fvg_xauusd.py` is a bespoke simulator, because the expert it measures
has no Python counterpart to feed through the engine. This does not have that
excuse: `EmaBollinger` is a real `Strategy`, so it goes through
`algo/backtest/cfd_runner.py` - the same path the dashboard's research console
uses, and the reason D-130 extracted that module. Costs, drawdown and the stop
are therefore computed by shared, tested code rather than by a second copy.

## What is swept, and what that is worth

Two modes x three timeframes x three windows. That is a comparison, not an
optimisation: D-124 established that the bar interval is the dominant term for
this instrument (trade count halves per step while the round-trip spread is
charged per round trip), so reporting one timeframe would hide the effect that
matters most. D-131 established that tuning thresholds on this data fits noise,
so no threshold is tuned here - every parameter is a stated default.

**Reading the best cell as the result is exactly the error D-131 names.** The
question this answers is whether the pair shows an edge that survives changing
the window, not which cell is largest.

## The stop

`STOP_LOSS_PCT` 0.5, the project default, matching what the two ports run and
what D-124's matrix measured. The trail is off (`TRAIL_PCT` 0): D-124 found that
running the trail *without* a flat stop was negative in all six cells it tested,
and whether the two together beat either alone is an open question that this
study is not the place to settle.

Usage:
    python scripts/measure_ema_bb_xauusd.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, CfdResult, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.instrument import CfdId
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

XAUUSD = CfdId(symbol="XAUUSD")

#: One MT5 lot = 100 engine lots (ounces), matching `measure_macd_xauusd.py`
#: so the net figures here sit on the same scale as D-124's matrix.
LOTS = 100
STARTING_EQUITY = Decimal("100000")

STOP_LOSS_PCT = Decimal("0.5")
TRAIL_ACTIVATION_PCT = Decimal("2")
TRAIL_PCT = Decimal("0")

TIMEFRAMES = {
    "M15": (Timeframe(minutes=15), "TIMEFRAME_M15"),
    "M30": (Timeframe(minutes=30), "TIMEFRAME_M30"),
    "H1": (Timeframe(minutes=60), "TIMEFRAME_H1"),
}

#: D-140's three windows, so these numbers sit beside the scalper's and the
#: fair value gap expert's.
WINDOWS = [
    ("2026.06-08", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),
    ("2026.01-05", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
    ("2025.06-12", datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
]


def fetch_bars(tf: Timeframe, mt5_constant: str, *, count: int = 50_000) -> list[Bar]:
    """Real closed bars. Position 1, not 0 - the forming bar's close can still
    change, the same exclusion `Mt5BarFeed.closed_bars` makes."""
    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select("XAUUSD", True):
        raise SystemExit(f"could not select XAUUSD: {mt5.last_error()}")
    offset = measure_server_offset(mt5, "XAUUSD")
    raw = mt5.copy_rates_from_pos("XAUUSD", getattr(mt5, mt5_constant), 1, count)
    mt5.shutdown()
    if raw is None or len(raw) == 0:
        raise SystemExit(f"MT5 returned no {mt5_constant} bars")
    bars = [
        Bar(
            ts=datetime.fromtimestamp(int(row["time"]), UTC) - offset,
            timeframe=tf,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["tick_volume"]),
        )
        for row in raw
    ]
    bars.sort(key=lambda b: b.ts)
    return bars


def profit_factor(result: CfdResult) -> Decimal | None:
    """Gross wins over gross losses. `None` with no losing trade - a profit
    factor of "infinity" is a sample-size statement, not a performance one."""
    won = sum((t.net_pnl for t in result.trades if t.net_pnl > 0), Decimal("0"))
    lost = -sum((t.net_pnl for t in result.trades if t.net_pnl <= 0), Decimal("0"))
    if lost <= 0:
        return None
    return won / lost


def run_cell(bars: list[Bar], tf: Timeframe, mode: str) -> CfdResult:
    return run_cfd_backtest(
        bars,
        instrument=XAUUSD,
        timeframe=tf,
        strategy_factory=lambda: EmaBollinger(
            instrument=XAUUSD,
            mode=mode,
            stop_loss_pct=STOP_LOSS_PCT,
            trail_activation_pct=TRAIL_ACTIVATION_PCT,
            trail_pct=TRAIL_PCT,
        ),
        stop_loss_pct=STOP_LOSS_PCT,
        trail_activation_pct=TRAIL_ACTIVATION_PCT,
        trail_pct=TRAIL_PCT,
        lots=LOTS,
        starting_equity=STARTING_EQUITY,
        costs=CfdCosts(),
    )


def main() -> int:
    print("50 EMA + Bollinger(20, 2.0) on XAUUSD, both readings")
    print(
        f"{LOTS} engine lots (1.00 MT5 lot), ${STARTING_EQUITY} equity, "
        f"stop {STOP_LOSS_PCT}%, trail off. Costs: D-121 measured."
    )
    print("Warmup is served from bars BEFORE each window, so no window starts blind.\n")

    series = {name: fetch_bars(tf, const) for name, (tf, const) in TIMEFRAMES.items()}
    for name, bars in series.items():
        print(f"  {name}: {len(bars)} bars, {bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d}")

    header = (
        f"\n{'window':<12} {'tf':<4} {'mode':<9} {'trades':>7} {'win%':>6} "
        f"{'PF':>6} {'net $':>12} {'maxDD%':>7}"
    )
    print(header)
    print("-" * len(header))

    for label, start, end in WINDOWS:
        for tf_name, (tf, _const) in TIMEFRAMES.items():
            bars = series[tf_name]
            warm = EmaBollinger(instrument=XAUUSD).warmup_bars() + 5
            first = next((i for i, b in enumerate(bars) if b.ts >= start), None)
            if first is None:
                continue
            sliced = [b for b in bars[max(0, first - warm) :] if b.ts <= end]
            for mode in (PULLBACK, BREAKOUT):
                # Only trades opened inside the window are scored; the warmup
                # bars ahead of it exist to settle the EMA, not to be traded.
                result = run_cell(sliced, tf, mode)
                scored = [t for t in result.trades if t.entry_ts >= start]
                net = sum((t.net_pnl for t in scored), Decimal("0"))
                won = sum((t.net_pnl for t in scored if t.net_pnl > 0), Decimal("0"))
                lost = -sum((t.net_pnl for t in scored if t.net_pnl <= 0), Decimal("0"))
                pf = (won / lost) if lost > 0 else None
                wr = (
                    Decimal(sum(1 for t in scored if t.net_pnl > 0))
                    / Decimal(len(scored))
                    * 100
                    if scored
                    else None
                )
                dd = result.max_drawdown_pct
                print(
                    f"{label:<12} {tf_name:<4} {mode:<9} {len(scored):>7} "
                    f"{(f'{wr:.1f}' if wr is not None else '-'):>6} "
                    f"{(f'{pf:.2f}' if pf is not None else '-'):>6} "
                    f"{net:>12,.0f} "
                    f"{(f'{dd:.1f}' if dd is not None else '-'):>7}"
                )
        print()

    # ------------------------------------------------------------------
    # The falsification D-124 point 4 asks for. A trend-following signal
    # doing well while the underlying trended is not distinguishable, from
    # a single run, from genuine edge - so the trend itself is priced, and
    # the long/short split says whether the signal is doing anything a
    # long-only holder was not already getting.
    # ------------------------------------------------------------------
    print()
    print("### Falsification: is this edge, or is it just gold going up? ###")
    head = (
        f"{'window':<12} {'tf':<4} {'buy&hold $':>12} {'breakout $':>12} "
        f"{'long $':>11} {'short $':>11} {'longs':>6} {'shorts':>7}"
    )
    print(head)
    print("-" * len(head))
    for label, start, end in WINDOWS:
        for tf_name, (tf, _const) in TIMEFRAMES.items():
            bars = series[tf_name]
            warm = EmaBollinger(instrument=XAUUSD).warmup_bars() + 5
            first = next((i for i, b in enumerate(bars) if b.ts >= start), None)
            if first is None:
                continue
            sliced = [b for b in bars[max(0, first - warm) :] if b.ts <= end]
            inside = [b for b in sliced if b.ts >= start]
            if not inside:
                continue
            hold = (inside[-1].close - inside[0].close) * LOTS
            result = run_cell(sliced, tf, BREAKOUT)
            scored = [t for t in result.trades if t.entry_ts >= start]
            longs = [t for t in scored if t.side is Side.BUY]
            shorts = [t for t in scored if t.side is Side.SELL]
            ln = sum((t.net_pnl for t in longs), Decimal("0"))
            sn = sum((t.net_pnl for t in shorts), Decimal("0"))
            print(
                f"{label:<12} {tf_name:<4} {hold:>12,.0f} {ln + sn:>12,.0f} "
                f"{ln:>11,.0f} {sn:>11,.0f} {len(longs):>6} {len(shorts):>7}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
