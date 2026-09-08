"""PRE-REGISTERED walk-forward: is long-only EMA/BB breakout real, or was it a slice?

THIS FILE WAS WRITTEN AND COMMITTED BEFORE THE TEST WAS RUN. The hypothesis,
the data, the parameters and the pass/fail thresholds below are all fixed in
advance, and the verdict is COMPUTED by `verdict()` rather than judged after
the fact. If a threshold here disagrees with the result, the result wins and
the hypothesis is rejected - that is the entire point of writing it down first.
Check `git log` on this file: it is committed one commit before the entry that
reports what happened.

## The hypothesis

D-152 measured `EmaBollinger` in `breakout` mode and found its shorts negative
in eight of nine cells while its longs were positive in all nine. Long-only on
M30 would have returned $128,431 against buy-and-hold's $116,459 - the most
attractive number in that study and the least trustworthy, because it was found
by slicing the same three windows the rest of the study used. D-131 already
established that slicing this data fits noise.

**H1 (primary):** `EmaBollinger(mode=breakout, long_only=True)` has positive
expectancy on XAUUSD, net of D-121 costs, on data D-152 never saw.

**H2 (secondary):** it beats buy-and-hold over the same span.

## The data, and why it is genuinely out of sample

D-152 used 2025-06-01 to 2026-08-31. Everything strictly BEFORE 2025-06-01 is
untouched by it:

- **H1: 2018-03 to 2025-05-31** - the primary. About seven years, and it is the
  longest unseen span the broker retains.
- **M30: 2022-06 to 2025-05-31** - secondary, reported but not decisive.

Two timeframes is two chances at a false positive, so the primary is named here
rather than chosen later. H1 is primary because it has the most unseen history,
NOT because D-152 liked it - D-152's better timeframe was M30, and picking M30
now because it scored well there is the error this whole exercise exists to
avoid.

## Parameters - fixed, and nothing is being fitted

EMA 50, Bollinger 20/2.0, stop 0.5%, trail off, 100 engine lots, $100k, D-121
costs. These are `measure_ema_bb_xauusd.py`'s values, unchanged.

The walk-forward therefore serves a narrower purpose than usual. There is no
parameter to optimise, so the number that matters is the module's BASELINE
column - fixed parameters, validated on each out-of-sample window. The
optimiser column and its small stop-loss grid are carried only to answer the
secondary question D-131 predicts the answer to: does choosing a stop per
window beat leaving it alone? If it does not, that is the finding.

## The decision rule, fixed in advance

PASS requires ALL of:

  1. pooled out-of-sample profit factor >= 1.10 - a margin over 1.0, because
     slippage is not modelled anywhere in this project and a PF of 1.02 is
     indistinguishable from paying one more tick per fill;
  2. pooled out-of-sample net P&L > 0;
  3. at least 60% of walk-forward folds positive on the BASELINE column;
  4. beats buy-and-hold over the same span on EITHER absolute net P&L OR on
     net-per-unit-of-drawdown - the second is there because a strategy in the
     market part of the time should not have to beat a permanent long on
     absolute return to be worth having;
  5. at least 100 pooled out-of-sample trades. Below that the run reports
     INCONCLUSIVE rather than PASS or FAIL - too few trades is not evidence of
     absence.

Anything short of all five is a FAIL, and a FAIL means stop work on long-only
EMA/BB, not "try a different threshold".

Usage:
    python scripts/walkforward_ema_bb_xauusd.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, run_cfd_backtest
from algo.backtest.cfd_walkforward import run_cfd_walk_forward
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side
from algo.core.instrument import CfdId
from algo.data.mt5_feed import measure_server_offset
from algo.strategy.ema_bb import BREAKOUT, EmaBollinger

XAUUSD = CfdId(symbol="XAUUSD")
LOTS = 100
STARTING_EQUITY = Decimal("100000")

STOP_LOSS_PCT = Decimal("0.5")
TRAIL_ACTIVATION_PCT = Decimal("2")
TRAIL_PCT = Decimal("0")

#: Everything before D-152's earliest window. Nothing at or after this date is
#: touched by this study.
OOS_END = datetime(2025, 5, 31, tzinfo=UTC)

PRIMARY = ("H1", Timeframe(minutes=60), "TIMEFRAME_H1")
SECONDARY = ("M30", Timeframe(minutes=30), "TIMEFRAME_M30")

#: Pre-registered thresholds. Do not edit these after seeing a result.
MIN_PROFIT_FACTOR = Decimal("1.10")
MIN_FOLD_WIN_RATE = Decimal("0.60")
MIN_OOS_TRADES = 100

IN_SAMPLE_DAYS = 365
OUT_OF_SAMPLE_DAYS = 90


def fetch_bars(tf: Timeframe, mt5_constant: str, *, count: int = 50_000) -> list[Bar]:
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
    return [b for b in bars if b.ts <= OOS_END]


def build(params: object) -> EmaBollinger:
    """The candidate, from one `ParameterSet`. Only the stop varies."""
    stop = Decimal(params.get("stop_loss_pct", str(STOP_LOSS_PCT)))  # type: ignore[attr-defined]
    return EmaBollinger(
        instrument=XAUUSD,
        mode=BREAKOUT,
        long_only=True,
        stop_loss_pct=stop,
        trail_activation_pct=TRAIL_ACTIVATION_PCT,
        trail_pct=TRAIL_PCT,
    )


def pooled(bars: list[Bar], tf: Timeframe) -> tuple[Decimal, Decimal | None, int, Decimal]:
    """One straight run over the whole span: net, profit factor, trades, maxDD%."""
    result = run_cfd_backtest(
        bars,
        instrument=XAUUSD,
        timeframe=tf,
        strategy_factory=lambda: EmaBollinger(
            instrument=XAUUSD,
            mode=BREAKOUT,
            long_only=True,
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
    won = sum((t.net_pnl for t in result.trades if t.net_pnl > 0), Decimal("0"))
    lost = -sum((t.net_pnl for t in result.trades if t.net_pnl <= 0), Decimal("0"))
    pf = (won / lost) if lost > 0 else None
    dd = result.max_drawdown_pct or Decimal("0")
    # Shorts must not appear at all; a long-only run that opened one would mean
    # the flag is not doing what the hypothesis says it does.
    assert not [t for t in result.trades if t.side is Side.SELL], "long_only opened a short"
    return result.net_pnl, pf, len(result.trades), dd


def verdict(
    *,
    profit_factor: Decimal | None,
    net: Decimal,
    fold_win_rate: Decimal | None,
    trades: int,
    beats_hold: bool,
) -> str:
    """The pre-registered rule, computed. No judgement call lives here."""
    if trades < MIN_OOS_TRADES:
        return f"INCONCLUSIVE - {trades} out-of-sample trades, under the {MIN_OOS_TRADES} floor"
    failures = []
    if profit_factor is None or profit_factor < MIN_PROFIT_FACTOR:
        failures.append(f"profit factor {profit_factor} < {MIN_PROFIT_FACTOR}")
    if net <= 0:
        failures.append(f"net {net:,.0f} is not positive")
    if fold_win_rate is None or fold_win_rate < MIN_FOLD_WIN_RATE:
        failures.append(f"fold win rate {fold_win_rate} < {MIN_FOLD_WIN_RATE}")
    if not beats_hold:
        failures.append("does not beat buy-and-hold on net or on net/drawdown")
    if failures:
        return "FAIL - " + "; ".join(failures)
    return "PASS"


def study(label: str, tf: Timeframe, const: str, *, primary: bool) -> None:
    bars = fetch_bars(tf, const)
    if not bars:
        print(f"{label}: no bars in the out-of-sample span")
        return
    span = f"{bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d}"
    print(f"\n=== {label} ({'PRIMARY' if primary else 'secondary'}) - {len(bars)} bars, {span} ===")

    net, pf, trades, dd = pooled(bars, tf)
    hold_net = (bars[-1].close - bars[0].close) * LOTS
    # Buy-and-hold's own drawdown, on the same bars, so the risk-adjusted
    # comparison is like for like rather than against an assumed zero.
    peak = bars[0].close
    hold_dd = Decimal("0")
    for b in bars:
        peak = max(peak, b.close)
        if peak > 0:
            hold_dd = max(hold_dd, (peak - b.close) / peak * 100)

    per_dd = (net / dd) if dd > 0 else None
    hold_per_dd = (hold_net / hold_dd) if hold_dd > 0 else None
    beats_hold = net > hold_net or (
        per_dd is not None and hold_per_dd is not None and per_dd > hold_per_dd
    )

    print(f"  pooled OOS : net {net:>12,.0f} | PF {pf if pf is None else f'{pf:.2f}'} "
          f"| {trades} trades | maxDD {dd:.1f}%")
    print(f"  buy & hold : net {hold_net:>12,.0f} | maxDD {hold_dd:.1f}%")
    if per_dd is not None and hold_per_dd is not None:
        print(f"  net per 1% of drawdown: strategy {per_dd:,.0f} vs hold {hold_per_dd:,.0f}")

    report = run_cfd_walk_forward(
        bars,
        factory=build,
        instrument=XAUUSD,
        timeframe=tf,
        axes={"stop_loss_pct": ["0.3", "0.5", "0.8"]},
        base={
            "stop_loss_pct": str(STOP_LOSS_PCT),
            "trail_activation_pct": str(TRAIL_ACTIVATION_PCT),
            "trail_pct": str(TRAIL_PCT),
        },
        lots=LOTS,
        in_sample_days=IN_SAMPLE_DAYS,
        out_of_sample_days=OUT_OF_SAMPLE_DAYS,
        starting_equity=STARTING_EQUITY,
    )
    baselines = [
        r.baseline_out_of_sample for r in report.results if r.baseline_out_of_sample is not None
    ]
    positive = [m for m in baselines if m.net_pnl > 0]
    fold_rate = (Decimal(len(positive)) / Decimal(len(baselines))) if baselines else None

    print(f"  folds      : {len(report.results)} windows "
          f"({IN_SAMPLE_DAYS}d in / {OUT_OF_SAMPLE_DAYS}d out)")
    rate_txt = "" if fold_rate is None else f" ({fold_rate * 100:.0f}%)"
    base_net = report.baseline_net_pnl
    base_txt = "n/a" if base_net is None else f"{base_net:,.0f}"
    print(f"  baseline   : {len(positive)}/{len(baselines)} folds positive{rate_txt}"
          f" | net {base_txt}")
    print(f"  optimised  : net {report.out_of_sample_net_pnl:,.0f} "
          f"| beat doing nothing: {report.optimisation_beat_doing_nothing}")
    print(f"  feasibility: {report.feasibility}")

    call = verdict(
        profit_factor=pf, net=net, fold_win_rate=fold_rate, trades=trades, beats_hold=beats_hold
    )
    print(f"\n  >>> {'PRIMARY ' if primary else 'secondary '}VERDICT: {call}")


def main() -> int:
    print("PRE-REGISTERED TEST - thresholds fixed before running; see the module docstring.")
    print(
        f"Hypothesis: EmaBollinger(breakout, long_only) has edge on data "
        f"before {OOS_END:%Y-%m-%d}."
    )
    print(
        f"Pass needs PF >= {MIN_PROFIT_FACTOR}, net > 0, "
        f">= {MIN_FOLD_WIN_RATE:.0%} folds positive, a win over buy-and-hold, "
        f"and at least {MIN_OOS_TRADES} trades."
    )
    for label, tf, const in (PRIMARY, SECONDARY):
        study(label, tf, const, primary=(label == PRIMARY[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
