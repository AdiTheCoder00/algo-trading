"""The Fibonacci-pivot / five-EMA cascade on BTCUSD.

The sibling study `measure_pivot_ema_cascade_xauusd.py` scores the same rule on
gold, and D-157 records what it found. This is the same rule, the same runner
and the same tables on a different instrument - and it exists as its own file
rather than a `--symbol` flag on that one because almost nothing about the
costs, the size or the session carries across.

## What is different, and why each difference is stated rather than inherited

**Size.** One engine lot is one BTC, not gold's hundred ounces. Every net figure
below is therefore "per 1 BTC", which at these prices is a large position; read
it as a scale, not a recommendation.

**Costs are assumed, not measured.** D-121's numbers came out of a real Vantage
account's XAUUSD dealing history. Nothing equivalent exists here for crypto, so
`HALF_SPREAD` is an assumption and the study prints a spread sweep beside the
headline instead of pretending one number is the truth. Swap is set to zero
points, which is *optimistic*: a real crypto CFD charges financing, and this
study does not. Commission is zero and unverified, the same posture
`CfdChargeModel`'s docstring takes.

**The session is a genuine question, not a detail.** Pivots are drawn from the
previous session, and crypto has no session - it trades continuously through
weekends. So "which day" is a choice, and D-157 is the reason it cannot be made
quietly: on gold, moving the session boundary by one hour flipped the sign of
every window. Both readings are therefore run here and printed side by side:

* `forex` - the same 17:00 New York cut the gold study uses, which is what a
  broker's BTCUSD CFD chart will show if the broker stamps it in server time.
* `utc` - the plain UTC calendar day, which is what an exchange chart shows.

If the two disagree, that is the finding, exactly as it was for gold.

Usage:
    python scripts/measure_pivot_ema_cascade_btcusd.py --csv M5=bars/btcusd_m5.csv
    python scripts/measure_pivot_ema_cascade_btcusd.py --csv M5=... --csv M15=...
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from algo.backtest.cfd_runner import CfdCosts, CfdResult, run_cfd_backtest
from algo.backtest.signal_replay import forex_session_of
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.instrument import CfdId
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.data.csv_feed import read_csv_bars
from algo.strategy.pivot_ema_cascade import PivotEmaCascade

BTCUSD = CfdId(symbol="BTCUSD")

#: One engine lot is one BTC. The gold study's 100 is a broker lot of ounces and
#: means nothing here.
LOTS = 1
STARTING_EQUITY = Decimal("100000")

STOP_LOSS_PCT = Decimal("0.5")
TRAIL_ACTIVATION_PCT = Decimal("2")
TRAIL_PCT = Decimal("0")

#: An ASSUMPTION, and the sweep below exists because of it. Roughly $20 of
#: round-trip spread on a ~$100k instrument, which is the order of magnitude a
#: retail crypto CFD quotes; a real account's dealing history would replace it.
HALF_SPREAD = Decimal("10")
SPREAD_SWEEP = (Decimal("2"), Decimal("10"), Decimal("25"), Decimal("50"))

TIMEFRAMES = {
    "M5": (Timeframe(minutes=5), "TIMEFRAME_M5"),
    "M15": (Timeframe(minutes=15), "TIMEFRAME_M15"),
    "M30": (Timeframe(minutes=30), "TIMEFRAME_M30"),
}

#: D-140's three windows, unchanged, so these sit beside the gold numbers.
WINDOWS = [
    ("2026.06-08", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),
    ("2026.01-05", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
    ("2025.06-12", datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
]

SESSION_BARS = 288


def utc_session_of(ts: datetime) -> date:
    """The plain UTC calendar day - what an exchange chart cuts on.

    A continuously traded market has no session close to inherit, so this is the
    honest alternative to borrowing forex's: it is a stated convention rather
    than one carried over from an instrument that stops trading on Friday.
    """
    return ts.date()


SESSIONS: dict[str, Callable[[datetime], date]] = {
    "forex": forex_session_of,
    "utc": utc_session_of,
}


def costs(half_spread: Decimal) -> CfdCosts:
    """BTC's charges: a stated spread, no financing, no commission.

    Zero swap is optimistic and deliberate rather than careless - inventing a
    financing rate would put a made-up number into every overnight trade, and
    an optimistic cost model that still loses money is a stronger result than a
    pessimistic one that does.
    """
    return CfdCosts(
        half_spread=half_spread,
        swap=SwapModel(
            long_points=Decimal("0"), short_points=Decimal("0"), point_value=Decimal("1")
        ),
        commission=CfdChargeModel(commission_per_lot=Decimal("0"), verified=False),
    )


def run_cell(bars: list[Bar], tf: Timeframe, *, half_spread: Decimal = HALF_SPREAD) -> CfdResult:
    return run_cfd_backtest(
        bars,
        instrument=BTCUSD,
        timeframe=tf,
        strategy_factory=lambda: PivotEmaCascade(
            instrument=BTCUSD,
            stop_loss_pct=STOP_LOSS_PCT,
            trail_activation_pct=TRAIL_ACTIVATION_PCT,
            trail_pct=TRAIL_PCT,
            giveback_frac=Decimal("0"),  # D-154
        ),
        stop_loss_pct=STOP_LOSS_PCT,
        trail_activation_pct=TRAIL_ACTIVATION_PCT,
        trail_pct=TRAIL_PCT,
        lots=LOTS,
        starting_equity=STARTING_EQUITY,
        costs=costs(half_spread),
        session_of=SESSIONS[_SESSION],
    )


#: Set once by `main` before any cell runs. A module-level switch rather than a
#: parameter threaded through every call site, because it must be impossible for
#: one table in a run to use a different session rule from another.
_SESSION = "forex"


def warmup_for() -> int:
    return PivotEmaCascade(instrument=BTCUSD).warmup_bars() + SESSION_BARS


def slice_for(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    first = next((i for i, b in enumerate(bars) if b.ts >= start), None)
    if first is None:
        return []
    return [b for b in bars[max(0, first - warmup_for()) :] if b.ts <= end]


def scored_trades(bars: list[Bar], tf: Timeframe, start: datetime, **kw: Decimal) -> list:
    return [t for t in run_cell(bars, tf, **kw).trades if t.entry_ts >= start]


def results_table(series: dict[str, list[Bar]]) -> None:
    header = (
        f"\n{'window':<12} {'tf':<4} {'trades':>7} {'win%':>6} "
        f"{'PF':>6} {'net $':>12} {'maxDD%':>7} {'spread $':>10}"
    )
    print(header)
    print("-" * len(header))
    for label, start, end in WINDOWS:
        for tf_name, (tf, _c) in TIMEFRAMES.items():
            if tf_name not in series:
                continue
            sliced = slice_for(series[tf_name], start, end)
            if not sliced:
                continue
            result = run_cell(sliced, tf)
            scored = [t for t in result.trades if t.entry_ts >= start]
            net = sum((t.net_pnl for t in scored), Decimal("0"))
            won = sum((t.net_pnl for t in scored if t.net_pnl > 0), Decimal("0"))
            lost = -sum((t.net_pnl for t in scored if t.net_pnl <= 0), Decimal("0"))
            pf = (won / lost) if lost > 0 else None
            wr = (
                Decimal(sum(1 for t in scored if t.net_pnl > 0)) / Decimal(len(scored)) * 100
                if scored
                else None
            )
            spread = sum((t.spread_paid for t in scored), Decimal("0"))
            dd = result.max_drawdown_pct
            print(
                f"{label:<12} {tf_name:<4} {len(scored):>7} "
                f"{(f'{wr:.1f}' if wr is not None else '-'):>6} "
                f"{(f'{pf:.2f}' if pf is not None else '-'):>6} "
                f"{net:>12,.0f} "
                f"{(f'{dd:.1f}' if dd is not None else '-'):>7} "
                f"{spread:>10,.0f}"
            )
        print()


def falsification_table(series: dict[str, list[Bar]]) -> None:
    print("### Falsification: is this edge, or is it just bitcoin moving? ###")
    head = (
        f"{'window':<12} {'tf':<4} {'buy&hold $':>12} {'strategy $':>12} "
        f"{'long $':>11} {'short $':>11} {'longs':>6} {'shorts':>7}"
    )
    print(head)
    print("-" * len(head))
    for label, start, end in WINDOWS:
        for tf_name, (tf, _c) in TIMEFRAMES.items():
            if tf_name not in series:
                continue
            sliced = slice_for(series[tf_name], start, end)
            inside = [b for b in sliced if b.ts >= start]
            if not inside:
                continue
            hold = (inside[-1].close - inside[0].close) * LOTS
            scored = scored_trades(sliced, tf, start)
            longs = [t for t in scored if t.side is Side.BUY]
            shorts = [t for t in scored if t.side is Side.SELL]
            ln = sum((t.net_pnl for t in longs), Decimal("0"))
            sn = sum((t.net_pnl for t in shorts), Decimal("0"))
            print(
                f"{label:<12} {tf_name:<4} {hold:>12,.0f} {ln + sn:>12,.0f} "
                f"{ln:>11,.0f} {sn:>11,.0f} {len(longs):>6} {len(shorts):>7}"
            )
        print()


def spread_sweep(series: dict[str, list[Bar]]) -> None:
    """What the assumed spread is worth, since it is assumed.

    A result that only exists at the tightest spread is a result about the
    spread. Printing the sweep makes that visible instead of leaving it to a
    reader who does not know `HALF_SPREAD` was a guess.
    """
    if "M5" not in series:
        return
    print("### M5 net $ against the assumed half-spread (it is an assumption) ###")
    head = f"{'window':<12} " + " ".join(f"{f'half {s}':>12}" for s in SPREAD_SWEEP)
    print(head)
    print("-" * len(head))
    tf = TIMEFRAMES["M5"][0]
    for label, start, end in WINDOWS:
        sliced = slice_for(series["M5"], start, end)
        if not sliced:
            continue
        cells = []
        for half in SPREAD_SWEEP:
            scored = scored_trades(sliced, tf, start, half_spread=half)
            cells.append(sum((t.net_pnl for t in scored), Decimal("0")))
        print(f"{label:<12} " + " ".join(f"{c:>12,.0f}" for c in cells))
    print()


def exit_reasons(series: dict[str, list[Bar]]) -> None:
    if "M5" not in series:
        return
    print("### How trades ended (M5 only) ###")
    tf = TIMEFRAMES["M5"][0]
    for label, start, end in WINDOWS:
        sliced = slice_for(series["M5"], start, end)
        if not sliced:
            continue
        by_kind: dict[str, int] = {}
        for trade in scored_trades(sliced, tf, start):
            kind = (
                "stop"
                if trade.exit_reason.startswith("stop loss")
                else "trail"
                if trade.exit_reason.startswith(("trailing stop", "give-back"))
                else "10/20 EMA"
                if "back " in trade.exit_reason
                else "open at end"
            )
            by_kind[kind] = by_kind.get(kind, 0) + 1
        breakdown = ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items()))
        print(f"{label:<12} {len(scored_trades(sliced, tf, start)):>5} trades: {breakdown or '-'}")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv", action="append", default=[], metavar="TF=PATH",
        help="read one timeframe's bars from a CSV, e.g. M5=bars/btcusd_m5.csv. Repeatable.",
    )
    parser.add_argument(
        "--session", choices=sorted(SESSIONS), default=None,
        help="run only this session rule instead of both (see the module docstring)",
    )
    return parser.parse_args()


def main() -> int:
    global _SESSION
    args = parse_args()
    csv_paths: dict[str, Path] = {}
    for entry in args.csv:
        name, _, raw = entry.partition("=")
        if name not in TIMEFRAMES or not raw:
            raise SystemExit(f"--csv wants TF=PATH with TF in {list(TIMEFRAMES)}, got {entry!r}")
        csv_paths[name] = Path(raw)
    if not csv_paths:
        raise SystemExit("this study reads CSV only: pass --csv M5=path (see --help)")

    series = {name: read_csv_bars(p, TIMEFRAMES[name][0]) for name, p in csv_paths.items()}

    print("Fibonacci pivots + 10/20/50/100/200 EMA cascade on BTCUSD")
    print(
        f"{LOTS} BTC per trade, ${STARTING_EQUITY} equity, stop {STOP_LOSS_PCT}%, trail off."
    )
    print(
        f"Costs are ASSUMED, not measured: half-spread ${HALF_SPREAD}, zero swap "
        "(optimistic), zero unverified commission. See the spread sweep."
    )
    print(f"Warmup {warmup_for()} bars; only trades opened INSIDE a window are scored.\n")
    for name, bars in series.items():
        print(f"  {name}: {len(bars):,} bars, {bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d}")

    for session in [args.session] if args.session else sorted(SESSIONS):
        _SESSION = session
        rule = (
            "17:00 New York, as the gold study cuts it"
            if session == "forex"
            else "the plain UTC calendar day, as an exchange chart cuts it"
        )
        print(f"\n{'=' * 78}\n== SESSION RULE: {session} - {rule}\n{'=' * 78}")
        results_table(series)
        falsification_table(series)
        spread_sweep(series)
        exit_reasons(series)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
