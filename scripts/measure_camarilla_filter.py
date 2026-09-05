"""What does a Camarilla entry filter do to the Donchian breakout?

`mt5/Experts/AlgoGold/GoldCamarillaBreakout.mq5` carries an entry filter: refuse
any entry whose fill price sits inside a no-trade region built from the ten
Camarilla levels. The expert is the only place it exists - there is no Python
counterpart, the same as the salvage and scale-in rules beside it - so nothing
in `tests/` covers whether the filter is a good idea, only that the arithmetic
compiles.

This script answers the other question. It replicates each candidate filter as a
wrapper around the real `TrendlineBreakout` and runs identical bars through
`run_cfd_backtest`, so two arms differ in exactly one thing.

## The regions measured

Camarilla's own reading of its levels, which is where the shapes come from:
L3..H3 is the balance zone the day rotates inside, H4/L4 are the breakout
triggers, and beyond them is trend territory.

- `bands:P`      thin band of P% of the nearest-neighbour gap around each of
                 the ten levels. What the expert ships today.
- `inner3`       no entry anywhere between L3 and H3.
- `inner4`       no entry anywhere between L4 and H4.
- `dir3`         longs only above H3, shorts only below L3.
- `dir4`         longs only above H4, shorts only below L4 - the textbook
                 Camarilla breakout rule.

## The placebo is the point

A filter that blocks 8% of entries and moves P&L by five figures has not been
shown to work; it has been shown that a few trades dominate the sample. So
`--placebo N` reruns each arm N times blocking the *same number* of entries at
random instants, and reports the spread of those deltas. A real filter's delta
has to sit outside that spread to mean anything. Reading the arm without the
placebo beside it is how a noisy result gets published as an edge.

## The delta is the result, not the level

The cost stack this repo verified (D-121, 54 real deals) is the **XAUUSD** one.
FixedVol100 and BTCUSD are priced here from what the terminal reports today:
half the current spread as a constant, and the percentage-mode swap converted to
a nightly points figure (both report `swap_mode == 5`, annual percent, which
`SwapModel` does not model natively). Good enough to rank two arms against each
other; not good enough to quote as this strategy's P&L on those symbols.

Two further limits, stated so a reader need not find them:

- **The fill price is the bar close, not the ask.** The expert tests the live
  ask/bid; a bar backtest has no such thing.
- **Suppressed entries are dropped, not deferred.** That is what the expert
  does too, so the arms stay comparable - but a filter that merely *delays* an
  entry would look worse here than it deserves.

The level formulas are MetaQuotes' "Camarilla Channel.mq5", verbatim, rebuilt
here for the same reason the expert ports them instead of calling `iCustom`:
they depend on one completed daily bar and nothing else.
"""

from __future__ import annotations

import argparse
import itertools
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from algo.backtest.cfd_runner import CfdCosts, CfdResult, run_cfd_backtest
from algo.core.bar import Bar, Timeframe
from algo.core.enums import Side, SignalAction
from algo.core.instrument import CfdId
from algo.core.signal import Signal
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.data.mt5_history import fetch_history, resolve_server_offset
from algo.live.mt5_runner import strategy_for
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext

DAY = Timeframe(minutes=1440)

#: The expert's own defaults, so the measured arm is the one that ships.
LOOKBACK = 20
STOP_LOSS_PCT = Decimal("0.5")
TRAIL_ACTIVATION_PCT = Decimal("2.0")
TRAIL_PCT = Decimal("0")

BARS_PER_REQUEST = 50_000

#: Ascending, which is the order `levels_from_bar` sorts into.
LEVEL_NAMES = ("L5", "L4", "L3", "L2", "L1", "H1", "H2", "H3", "H4", "H5")


# --------------------------------------------------------------------------
# Camarilla levels
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DayLevels:
    """The ten levels for one trading day, with per-level band half-widths."""

    by_name: dict[str, Decimal]
    ordered: tuple[Decimal, ...]
    halves: tuple[Decimal, ...]

    def in_band(self, price: Decimal) -> bool:
        """Inside the thin band around any single level."""
        return any(
            half > 0 and abs(price - level) <= half
            for level, half in zip(self.ordered, self.halves, strict=True)
        )

    def inside(self, price: Decimal, lower: str, upper: str) -> bool:
        return self.by_name[lower] <= price <= self.by_name[upper]


def levels_from_bar(prev: Bar, *, zone_pct: Decimal) -> DayLevels | None:
    """The indicator's ten levels off one completed daily bar.

    `None` for a bar that cannot produce them - a zero low would divide by zero
    in H5, and a non-positive range collapses every level onto the close. The
    expert refuses the same two cases.
    """
    if prev.low <= 0 or prev.high <= prev.low:
        return None

    rng = prev.high - prev.low
    close = prev.close
    h5 = (prev.high / prev.low) * close
    r = Decimal("1.1")

    named = {
        "L5": close - (h5 - close),
        "L4": close - rng * r / 2,
        "L3": close - rng * r / 4,
        "L2": close - rng * r / 6,
        "L1": close - rng * r / 12,
        "H1": close + rng * r / 12,
        "H2": close + rng * r / 6,
        "H3": close + rng * r / 4,
        "H4": close + rng * r / 2,
        "H5": h5,
    }

    ordered = tuple(sorted(named.values()))
    halves: list[Decimal] = []
    for i, value in enumerate(ordered):
        gaps = []
        if i + 1 < len(ordered):
            gaps.append(ordered[i + 1] - value)
        if i - 1 >= 0:
            gaps.append(value - ordered[i - 1])
        halves.append(min(gaps) * zone_pct / 100)

    return DayLevels(by_name=named, ordered=ordered, halves=tuple(halves))


def level_table(daily: list[Bar], *, zone_pct: Decimal) -> dict[date, DayLevels]:
    """Map each session date to the levels built from the day *before* it."""
    table: dict[date, DayLevels] = {}
    for previous, current in itertools.pairwise(daily):
        built = levels_from_bar(previous, zone_pct=zone_pct)
        if built is not None:
            table[current.ts.date()] = built
    return table


# --------------------------------------------------------------------------
# The filters, as strategy wrappers
# --------------------------------------------------------------------------
class FilteredStrategy(Strategy):
    """Delegates to `inner`, dropping entries `self.block` rejects.

    Only `SignalAction.OPEN` is ever suppressed. Closes - the flat stop, the
    trail, and the opposite-side breakout that flattens - pass through
    untouched, which is the expert's rule too: refusing to *leave* a position
    because price is near a pivot is how a small loss becomes a large one.
    """

    def __init__(self, inner: Strategy) -> None:
        super().__init__()
        self._inner = inner
        self.suppressed = 0
        self.entries_seen = 0

    def block(self, side: Side, price: Decimal, on: date) -> bool:
        raise NotImplementedError

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        signals = self._inner.on_bar(ctx)
        if not signals:
            return signals

        bar = ctx.bar
        kept: list[Signal] = []
        for signal in signals:
            if signal.action is not SignalAction.OPEN:
                kept.append(signal)
                continue
            self.entries_seen += 1
            if self.block(signal.legs[0].direction, bar.close, bar.ts.date()):
                self.suppressed += 1
                continue
            kept.append(signal)
        return kept

    def warmup_bars(self) -> int:
        return self._inner.warmup_bars()

    def params(self) -> dict[str, str]:
        return dict(self._inner.params())


class CamarillaFiltered(FilteredStrategy):
    """One of the Camarilla no-trade regions."""

    def __init__(self, inner: Strategy, table: dict[date, DayLevels], mode: str) -> None:
        super().__init__(inner)
        self._table = table
        self._mode = mode

    def block(self, side: Side, price: Decimal, on: date) -> bool:
        levels = self._table.get(on)
        if levels is None:
            # No completed daily bar for this session - the expert lets the
            # entry through and says so rather than refusing all day.
            return False

        if self._mode == "bands":
            return levels.in_band(price)
        if self._mode == "inner3":
            return levels.inside(price, "L3", "H3")
        if self._mode == "inner4":
            return levels.inside(price, "L4", "H4")
        if self._mode in ("dir3", "dir4"):
            up, down = ("H3", "L3") if self._mode == "dir3" else ("H4", "L4")
            if side is Side.BUY:
                return price <= levels.by_name[up]
            return price >= levels.by_name[down]
        raise SystemExit(f"unknown mode {self._mode!r}")


class RandomFiltered(FilteredStrategy):
    """Blocks entries at a fixed rate - the placebo for any real filter."""

    def __init__(self, inner: Strategy, rate: float, seed: int) -> None:
        super().__init__(inner)
        self._rate = rate
        self._rng = random.Random(seed)

    def block(self, side: Side, price: Decimal, on: date) -> bool:
        return self._rng.random() < self._rate


# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------
def costs_for(symbol: str, *, lots: int, price: Decimal) -> tuple[CfdCosts, str]:
    """The venue's charges for `symbol`, and a one-line note on their pedigree."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise SystemExit(f"no symbol info for {symbol}")

    half_spread = Decimal(str(info.spread)) * Decimal(str(info.point)) / 2
    per_point_broker = Decimal(str(info.trade_tick_value)) * (
        Decimal(str(info.point)) / Decimal(str(info.trade_tick_size))
    )
    point_value = per_point_broker / Decimal(lots)

    if info.swap_mode == 1:  # POINTS - what SwapModel documents
        swap = SwapModel(
            long_points=Decimal(str(info.swap_long)),
            short_points=Decimal(str(info.swap_short)),
            point_value=point_value,
        )
        note = "swap: broker points, as published"
    else:
        def nightly_points(annual_pct: float) -> Decimal:
            money = price * Decimal(str(annual_pct)) / 100 / Decimal("365")
            return money / per_point_broker

        swap = SwapModel(
            long_points=nightly_points(info.swap_long),
            short_points=nightly_points(info.swap_short),
            point_value=point_value,
        )
        note = f"swap: mode {info.swap_mode} (annual %), converted at price {price}"

    return (
        CfdCosts(
            half_spread=half_spread,
            swap=swap,
            commission=CfdChargeModel.vantage_standard(),
        ),
        f"half-spread {half_spread} (live {info.spread} pts); {note}",
    )


# --------------------------------------------------------------------------
# Driving
# --------------------------------------------------------------------------
@dataclass
class Arm:
    label: str
    result: CfdResult
    suppressed: int
    entries_seen: int


def run_arm(
    bars: list[Bar],
    *,
    label: str,
    instrument: CfdId,
    timeframe: Timeframe,
    lots: int,
    costs: CfdCosts,
    make: object | None,
) -> Arm:
    """One arm. `make=None` is the unfiltered baseline."""
    built: list[FilteredStrategy] = []

    def factory() -> object:
        inner = strategy_for(
            "breakout",
            instrument=instrument,
            stop_loss_pct=STOP_LOSS_PCT,
            trail_activation_pct=TRAIL_ACTIVATION_PCT,
            trail_pct=TRAIL_PCT,
            lookback=LOOKBACK,
        )
        if make is None:
            return inner
        wrapped = make(inner)  # type: ignore[operator]
        built.append(wrapped)
        return wrapped

    result = run_cfd_backtest(
        bars,
        instrument=instrument,
        timeframe=timeframe,
        strategy_factory=factory,
        stop_loss_pct=STOP_LOSS_PCT,
        trail_activation_pct=TRAIL_ACTIVATION_PCT,
        trail_pct=TRAIL_PCT,
        lots=lots,
        costs=costs,
    )
    return Arm(
        label=label,
        result=result,
        suppressed=sum(w.suppressed for w in built),
        entries_seen=sum(w.entries_seen for w in built),
    )


def print_row(arm: Arm, baseline: CfdResult | None) -> None:
    wr = arm.result.win_rate
    delta = "-" if baseline is None else f"{arm.result.net_pnl - baseline.net_pnl:+.2f}"
    blocked = "-" if baseline is None else str(arm.suppressed)
    win = "-" if wr is None else f"{wr:.1f}"
    print(f"  {arm.label:<14} {len(arm.result.trades):>7} {win:>7} "
          f"{arm.result.net_pnl:>13.2f} {delta:>14} {blocked:>9}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="FixedVol100,BTCUSD,XAUUSD")
    parser.add_argument("--timeframes", default="M15,H1")
    parser.add_argument("--modes", default="bands:5,inner3,inner4,dir3,dir4")
    parser.add_argument("--bars", type=int, default=BARS_PER_REQUEST)
    parser.add_argument("--placebo", type=int, default=0,
                        help="Rerun each arm N times blocking the same share at random")
    args = parser.parse_args()

    labels = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}
    timeframes = [(t, Timeframe(minutes=labels[t])) for t in args.timeframes.split(",")]

    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")

    try:
        for symbol in args.symbols.split(","):
            if not mt5.symbol_select(symbol, True):
                print(f"\n{symbol}: cannot select - {mt5.last_error()}")
                continue

            info = mt5.symbol_info(symbol)
            lots = max(int(info.trade_contract_size), 1)
            tick = mt5.symbol_info_tick(symbol)
            price = Decimal(str(tick.bid)) if tick else Decimal("1")
            costs, pedigree = costs_for(symbol, lots=lots, price=price)
            offset = resolve_server_offset(mt5, symbol).offset
            instrument = CfdId(symbol=symbol)
            daily = fetch_history(mt5, symbol=symbol, timeframe=DAY, count=3000, offset=offset)

            print("\n" + "=" * 100)
            print(f"{symbol}   contract {info.trade_contract_size} -> {lots} engine lots")
            print(f"  costs: {pedigree}")

            for label, timeframe in timeframes:
                try:
                    bars = fetch_history(mt5, symbol=symbol, timeframe=timeframe,
                                         count=args.bars, offset=offset)
                except Exception as exc:  # noqa: BLE001 - report and carry on
                    print(f"\n  {label}: no history ({exc})")
                    continue

                base = run_arm(bars, label="filter OFF", instrument=instrument,
                               timeframe=timeframe, lots=lots, costs=costs, make=None)

                print(f"\n  {label}  {len(bars)} bars  "
                      f"{bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d}")
                print(f"  {'arm':<14} {'trades':>7} {'win %':>7} "
                      f"{'net P&L':>13} {'vs baseline':>14} {'blocked':>9}")
                print_row(base, None)

                for spec in args.modes.split(","):
                    mode, _, pct = spec.partition(":")
                    table = level_table(daily, zone_pct=Decimal(pct or "5"))

                    def make(inner, table=table, mode=mode):
                        return CamarillaFiltered(inner, table, mode)

                    arm = run_arm(bars, label=spec, instrument=instrument,
                                  timeframe=timeframe, lots=lots, costs=costs, make=make)
                    print_row(arm, base.result)

                    if args.placebo and arm.entries_seen:
                        rate = arm.suppressed / arm.entries_seen
                        deltas = []
                        for seed in range(args.placebo):
                            def rmake(inner, rate=rate, seed=seed):
                                return RandomFiltered(inner, rate, seed)

                            shuffled = run_arm(bars, label="placebo",
                                               instrument=instrument, timeframe=timeframe,
                                               lots=lots, costs=costs, make=rmake)
                            deltas.append(
                                float(shuffled.result.net_pnl - base.result.net_pnl)
                            )
                        real = float(arm.result.net_pnl - base.result.net_pnl)
                        beat = sum(1 for d in deltas if d >= real)
                        print(f"  {'  placebo':<14} blocking {rate * 100:.1f}% at "
                              f"random, {len(deltas)} runs: "
                              f"mean {statistics.mean(deltas):+.0f}, "
                              f"range {min(deltas):+.0f}..{max(deltas):+.0f} | "
                              f"{beat}/{len(deltas)} random runs matched or beat "
                              f"the real filter")
    finally:
        mt5.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
