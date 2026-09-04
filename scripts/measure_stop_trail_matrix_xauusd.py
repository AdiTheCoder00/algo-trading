"""The exit configuration D-127 named and never measured: a flat stop AND a trail.

D-125 and D-126 measured a flat entry-anchored stop with the trail off. D-127
measured a 2%-armed, 0.5% trail with the flat stop off, found all six cells
negative, and said in its own closing paragraph that the interesting question
was a different one:

    Whether a flat stop *and* a 2%-activated trail together - the flat one
    bounding the loser side this measurement left open, the trail locking in
    the winner side once a trade earns it - beats either alone is a real,
    different question this run does not answer.

This script answers it. Six exit configurations x three timeframes x two
strategies, on one window, so every cell is comparable to every other and to
the four rows already published.

## Why a new script rather than another constant edit

`measure_macd_xauusd.py` carries the flat stop and the trail as module
constants, which is what made D-125/D-126/D-127 each a separate edit-and-run.
That is fine for one setting at a time and useless for a matrix. This drives
`run_cfd_backtest` - the shared core extracted from that script in D-130, which
already takes `stop_loss_pct`, `trail_activation_pct` and `trail_pct` as
arguments - so no constant is edited and all thirty-six cells come from one
run of one code path.

## The reproduction check is half the point

Four of the six configurations have published numbers. They are quoted in
`PUBLISHED` below and printed as a delta beside each reproduced cell, because a
new harness agreeing with the old one is the only thing that makes its *new*
cells worth reading. Two known reasons a delta will not be zero, both stated so
a difference is not mistaken for a bug:

- **The window is not identical.** D-124 through D-127 ran 2024-07-17 to
  2026-08-28. MT5 serves 50,000 bars per request, so M15 now reaches back only
  to about 2024-07-24 - a week short. The end is clamped to the published
  2026-08-28 so the windows differ at one edge only, and the window actually
  used is printed.
- **The trail gained an entry floor.** `trailing_profit_stop.py` now clamps an
  armed trail so it can never sit worse than the entry price ("cost to cost").
  Whether that was in force for D-127's numbers is not recorded there. If the
  trail-only row differs materially and the stop-only rows do not, that floor
  is the first thing to suspect.

A large delta on a *stop* row would be a genuine problem and should be chased
before anything in the new rows is believed.

Fixed 100 engine lots (one MT5 lot), the measured Vantage cost stack, no risk
scaling - the same terms as every other measurement here (D-089, D-121).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdResult, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_history import fetch_history, resolve_server_offset
from algo.live.mt5_runner import strategy_for

SYMBOL = "XAUUSD"
LOTS = 100
BARS_PER_REQUEST = 50_000

TIMEFRAMES: dict[str, Timeframe] = {
    "M15": Timeframe(minutes=15),
    "M30": Timeframe(minutes=30),
    "H1": Timeframe(minutes=60),
}

#: The end of the window D-124 through D-127 used. Clamping to it keeps the
#: comparison honest at one edge; the start is whatever 50,000 bars reaches.
PUBLISHED_END = datetime(2026, 8, 28, 23, 59, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Exit:
    """One exit configuration, and the entry that already published it."""

    label: str
    stop_pct: Decimal
    trail_activation_pct: Decimal
    trail_pct: Decimal
    #: The D-entry these numbers can be checked against, or None for a cell
    #: nobody has run before - which is the whole reason this script exists.
    published: str | None


EXITS: tuple[Exit, ...] = (
    Exit("no stop, no trail", Decimal("0"), Decimal("0"), Decimal("0"), "D-124"),
    Exit("0.5% stop only", Decimal("0.5"), Decimal("0"), Decimal("0"), "D-125"),
    Exit("1.0% stop only", Decimal("1.0"), Decimal("0"), Decimal("0"), "D-126"),
    Exit("2%/0.5% trail only", Decimal("0"), Decimal("2"), Decimal("0.5"), "D-127"),
    Exit("0.5% stop + 2%/0.5% trail", Decimal("0.5"), Decimal("2"), Decimal("0.5"), None),
    Exit("1.0% stop + 2%/0.5% trail", Decimal("1.0"), Decimal("2"), Decimal("0.5"), None),
)

#: Net P&L as published, quoted from docs/decisions.md. Keyed by
#: (strategy, D-entry, timeframe). Only for the delta column - nothing here
#: feeds a calculation.
PUBLISHED: dict[tuple[str, str, str], Decimal] = {
    ("macd", "D-124", "M15"): Decimal("-230052.41"),
    ("macd", "D-124", "M30"): Decimal("97653.20"),
    ("macd", "D-124", "H1"): Decimal("190185.54"),
    ("macd", "D-125", "M15"): Decimal("-77668.32"),
    ("macd", "D-125", "M30"): Decimal("77835.84"),
    ("macd", "D-125", "H1"): Decimal("132919.15"),
    ("macd", "D-126", "M15"): Decimal("-246375.46"),
    ("macd", "D-126", "M30"): Decimal("44000.66"),
    ("macd", "D-126", "H1"): Decimal("77915.80"),
    ("macd", "D-127", "M15"): Decimal("-406480.37"),
    ("macd", "D-127", "M30"): Decimal("-245940.96"),
    ("macd", "D-127", "H1"): Decimal("-136984.56"),
    ("breakout", "D-124", "M15"): Decimal("50983.08"),
    ("breakout", "D-124", "M30"): Decimal("102206.55"),
    ("breakout", "D-124", "H1"): Decimal("162297.62"),
    ("breakout", "D-125", "M15"): Decimal("-14778.90"),
    ("breakout", "D-125", "M30"): Decimal("52466.78"),
    ("breakout", "D-125", "H1"): Decimal("136477.21"),
    ("breakout", "D-126", "M15"): Decimal("131882.10"),
    ("breakout", "D-126", "M30"): Decimal("71710.76"),
    ("breakout", "D-126", "H1"): Decimal("79592.87"),
    ("breakout", "D-127", "M15"): Decimal("-162229.22"),
    ("breakout", "D-127", "M30"): Decimal("-144519.44"),
    ("breakout", "D-127", "H1"): Decimal("-48683.50"),
}


def fetch_all() -> tuple[dict[str, list[Bar]], str]:
    """Every timeframe, trimmed to the window they all cover.

    Each request hits MT5's 50,000-bar cap independently, so a faster
    timeframe's history spans far less calendar time than a slower one's.
    Comparing them unfiltered would conflate "this timeframe" with "this period
    happened to trend" - the confound D-124 identified and fixed, applied again
    here for the same reason.
    """
    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select(SYMBOL, True):
        mt5.shutdown()
        raise SystemExit(f"could not select {SYMBOL}: {mt5.last_error()}")
    resolved = resolve_server_offset(mt5, SYMBOL)
    raw = {
        label: fetch_history(
            mt5,
            symbol=SYMBOL,
            timeframe=tf,
            count=BARS_PER_REQUEST,
            offset=resolved.offset,
        )
        for label, tf in TIMEFRAMES.items()
    }
    mt5.shutdown()

    start = max(bars[0].ts for bars in raw.values())
    end = min(min(bars[-1].ts for bars in raw.values()), PUBLISHED_END)
    if start >= end:
        raise SystemExit(f"no overlapping window: {start} >= {end}")
    trimmed = {
        label: [b for b in bars if start <= b.ts <= end] for label, bars in raw.items()
    }
    span = (end - start).days / 365.25
    return trimmed, f"{start:%Y-%m-%d} .. {end:%Y-%m-%d} ({span:.2f} yr)"


def measure(bars: list[Bar], tf: Timeframe, strategy: str, exit_cfg: Exit) -> CfdResult:
    instrument = CfdId(symbol=SYMBOL)
    return run_cfd_backtest(
        bars,
        instrument=instrument,
        timeframe=tf,
        strategy_factory=lambda: strategy_for(
            strategy,
            instrument=instrument,
            stop_loss_pct=exit_cfg.stop_pct,
            trail_activation_pct=exit_cfg.trail_activation_pct,
            trail_pct=exit_cfg.trail_pct,
        ),
        stop_loss_pct=exit_cfg.stop_pct,
        trail_activation_pct=exit_cfg.trail_activation_pct,
        trail_pct=exit_cfg.trail_pct,
        lots=LOTS,
    )


def _money(value: Decimal) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.0f}"


def report(strategy: str, cells: dict[tuple[str, str], CfdResult], window: str) -> None:
    print()
    print(f"=== {strategy} === {window}, {LOTS} engine lots, measured Vantage costs")
    labels = list(TIMEFRAMES)
    head = f"{'exit configuration':<30}"
    for label in labels:
        head += f"{label + ' net':>14}{'trades':>8}{'vs pub':>12}"
    print(head)
    for exit_cfg in EXITS:
        row = f"{exit_cfg.label:<30}"
        for label in labels:
            result = cells[(exit_cfg.label, label)]
            row += f"{_money(result.net_pnl):>14}{len(result.trades):>8}"
            key = (strategy, exit_cfg.published or "", label)
            if exit_cfg.published and key in PUBLISHED:
                row += f"{_money(result.net_pnl - PUBLISHED[key]):>12}"
            else:
                row += f"{'new cell':>12}"
        print(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="flat stop and trail, together")
    parser.add_argument(
        "strategies", nargs="*", default=None, help="macd and/or breakout (default both)"
    )
    args = parser.parse_args()
    chosen = args.strategies or ["macd", "breakout"]
    unknown = [s for s in chosen if s not in ("macd", "breakout")]
    if unknown:
        raise SystemExit(f"unknown strategy {unknown}; available: macd, breakout")

    bars_by_tf, window = fetch_all()
    for label, bars in bars_by_tf.items():
        print(f"{label}: {len(bars):,} bars")

    for strategy in chosen:
        cells: dict[tuple[str, str], CfdResult] = {}
        for exit_cfg in EXITS:
            for label, tf in TIMEFRAMES.items():
                cells[(exit_cfg.label, label)] = measure(
                    bars_by_tf[label], tf, strategy, exit_cfg
                )
        report(strategy, cells, window)

    print()
    print("'vs pub' is this run minus the figure in the D-entry named by the row.")
    print("A large delta on a stop row is a problem; see the module docstring for the")
    print("two known reasons a delta is not zero.")


if __name__ == "__main__":
    main()
