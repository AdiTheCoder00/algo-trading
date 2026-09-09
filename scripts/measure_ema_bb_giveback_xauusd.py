"""Does the give-back trail help `EmaBollinger`? Measured, not assumed.

`algo/strategy/trailing_profit_stop.py` gained a second trail: `giveback_frac`
closes a position once it has handed back that fraction of the best unrealised
profit it ever showed. It was requested as trade management for `EmaBollinger`,
whose only exits were otherwise the flat stop and the middle band - so a winner
ran to the middle band and gave back whatever it had made on the way.

This measures whether that helps. It does not - D-154 has the result and the
reasoning for switching the trail off rather than tuning it.

## The asymmetry this study is really about

The give-back level sits at `activation * (1 - frac)` of profit at the moment it
arms - so with `frac = 0.5` a trail armed at 0.25% first becomes able to fire at
**0.125% of profit**, while the flat stop still lets a loser run to **0.5%**.
That is a 1:4 reward-to-risk floor on every trade the trail touches, and no entry
rule fixes it. The activation gate is therefore not a tuning knob here, it is the
whole question: a give-back trail is only coherent when it arms well above the
stop distance, and this sweeps it from a quarter of the stop to four times it to
show where - if anywhere - that crossover pays for itself.

## What is swept, and what that is worth

Both modes x three timeframes x three windows x a baseline plus four activation
gates. That is a comparison, not an optimisation, and D-131's warning applies
exactly as it does to `measure_ema_bb_xauusd.py`: **reading the best cell as the
result is the error**. The question is whether the trail beats its own baseline
consistently, not which gate is largest somewhere.

The baseline in every cell is the same strategy with `giveback_frac = 0` - i.e.
what D-152 measured - so each cell is a paired comparison against itself rather
than against a number from another study.

## The stop, and why it is not swept too

`STOP_LOSS_PCT` 0.5, as in `measure_ema_bb_xauusd.py` and both MT5 ports. Sweeping
the stop as well would turn a comparison into a 2-D search over the same data,
which is the shape D-131 rejected. The stop is held at the project default and
the trail is asked to earn its place beside it.

Usage:
    python scripts/measure_ema_bb_giveback_xauusd.py
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, CfdResult, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.instrument import CfdId
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, PULLBACK, EmaBollinger

XAUUSD = CfdId(symbol="XAUUSD")

#: One MT5 lot = 100 engine lots (ounces), matching `measure_ema_bb_xauusd.py`
#: so these net figures sit on the same scale as D-152's matrix.
LOTS = 100
STARTING_EQUITY = Decimal("100000")

STOP_LOSS_PCT = Decimal("0.5")
TRAIL_PCT = Decimal("0")
GIVEBACK_FRAC = Decimal("0.5")

#: A quarter of the stop, half of it, twice it, four times it. The point of the
#: range is the crossover at `activation = stop`, not any single value in it.
ACTIVATIONS = [Decimal("0.25"), Decimal("0.5"), Decimal("1"), Decimal("2")]

TIMEFRAMES = {
    "M15": (Timeframe(minutes=15), "TIMEFRAME_M15"),
    "M30": (Timeframe(minutes=30), "TIMEFRAME_M30"),
    "H1": (Timeframe(minutes=60), "TIMEFRAME_H1"),
}

#: D-140's three windows, the same ones D-152 used.
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


def profit_factor(trades: list) -> Decimal | None:
    """Gross wins over gross losses. `None` with no losing trade - a profit
    factor of "infinity" is a sample-size statement, not a performance one."""
    won = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal("0"))
    lost = -sum((t.net_pnl for t in trades if t.net_pnl <= 0), Decimal("0"))
    if lost <= 0:
        return None
    return won / lost


def exit_kind(reason: str) -> str:
    """Which exit closed the trade, from the reason prefix `cfd_runner` also
    keys its fill pricing off."""
    if reason.startswith("stop loss"):
        return "stop"
    if reason.startswith("trailing stop"):
        return "trail"
    if reason.startswith("give-back stop"):
        return "give"
    return "band"


def run_cell(
    bars: list[Bar], tf: Timeframe, mode: str, activation: Decimal, frac: Decimal
) -> CfdResult:
    return run_cfd_backtest(
        bars,
        instrument=XAUUSD,
        timeframe=tf,
        strategy_factory=lambda: EmaBollinger(
            instrument=XAUUSD,
            mode=mode,
            stop_loss_pct=STOP_LOSS_PCT,
            trail_activation_pct=activation,
            trail_pct=TRAIL_PCT,
            giveback_frac=frac,
        ),
        stop_loss_pct=STOP_LOSS_PCT,
        trail_activation_pct=activation,
        trail_pct=TRAIL_PCT,
        # The runner prices a give-back exit at the level the strategy triggered
        # on, so this must match the strategy's own fraction or every closed
        # trade would be filled at the bar close instead.
        giveback_frac=frac,
        lots=LOTS,
        starting_equity=STARTING_EQUITY,
        costs=CfdCosts(),
    )


def score(result: CfdResult, start: datetime) -> tuple[Decimal, Decimal | None, int, str]:
    """Net, profit factor, trade count and the exit mix - counting only trades
    OPENED inside the window, since the warmup bars ahead of it exist to settle
    the EMA rather than to be traded."""
    scored = [t for t in result.trades if t.entry_ts >= start]
    net = sum((t.net_pnl for t in scored), Decimal("0"))
    kinds = Counter(exit_kind(t.exit_reason or "") for t in scored)
    mix = " ".join(f"{k}:{v}" for k, v in sorted(kinds.items()))
    return net, profit_factor(scored), len(scored), mix


def main() -> int:
    print("EmaBollinger + give-back trail on XAUUSD, against its own baseline.")
    print(
        f"stop {STOP_LOSS_PCT}% | giveback_frac {GIVEBACK_FRAC} | lots {LOTS} | "
        f"D-121 costs\n"
    )
    print(
        "A trail armed at `a` first fires at a*(1-frac) of profit. Below the stop "
        f"of {STOP_LOSS_PCT}% that is a losing floor by construction - watch the "
        "0.25 and 0.5 columns.\n"
    )

    beats = 0
    cells = 0
    for tf_name, (tf, const) in TIMEFRAMES.items():
        bars = fetch_bars(tf, const)
        warm = EmaBollinger(instrument=XAUUSD).warmup_bars() + 5
        for mode in (BREAKOUT, PULLBACK):
            print(f"=== {tf_name} / {mode} " + "=" * 46)
            print(
                f"{'window':<11} {'gate':>6} {'net $':>12} {'PF':>6} {'trades':>7}  "
                f"exit mix"
            )
            for label, start, end in WINDOWS:
                first = next((i for i, b in enumerate(bars) if b.ts >= start), None)
                if first is None:
                    continue
                sliced = [b for b in bars[max(0, first - warm) :] if b.ts <= end]
                if len(sliced) < warm + 10:
                    continue

                base_net, base_pf, base_n, base_mix = score(
                    run_cell(sliced, tf, mode, ACTIVATIONS[0], Decimal("0")), start
                )
                pf_txt = f"{base_pf:.2f}" if base_pf is not None else "n/a"
                print(
                    f"{label:<11} {'OFF':>6} {base_net:>12,.0f} {pf_txt:>6} "
                    f"{base_n:>7}  {base_mix}"
                )

                for activation in ACTIVATIONS:
                    net, pf, n, mix = score(
                        run_cell(sliced, tf, mode, activation, GIVEBACK_FRAC), start
                    )
                    pf_txt = f"{pf:.2f}" if pf is not None else "n/a"
                    flag = "  <-- beats baseline" if net > base_net else ""
                    print(
                        f"{'':<11} {float(activation):>6.2f} {net:>12,.0f} {pf_txt:>6} "
                        f"{n:>7}  {mix}{flag}"
                    )
                    cells += 1
                    if net > base_net:
                        beats += 1
                print()

    print(
        f"The trail beat its own baseline in {beats} of {cells} cells "
        f"({100 * beats / cells:.0f}%)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
