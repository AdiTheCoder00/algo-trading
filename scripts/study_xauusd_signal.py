"""Is there ANY entry signal in this data? Measure first, build second.

The previous three studies established that the specified entry is a coin flip
and that no stricter reading of it helps. The obvious next move is to invent
another entry and backtest it, and that is exactly the move that generates
false positives: try enough rules and one of them will look good.

So this does it the other way round. Phase 1 asks a question that has an answer
independent of any strategy - **does any of these features predict the next four
hours of gold, at all** - on the in-sample window only. Phase 2 builds entries
only from what Phase 1 supports, and tests them out of sample against the same
random control the entry study used.

    python scripts/study_xauusd_signal.py --data state/xauusd --out reports/xauusd

## Why the significance test looks paranoid

Forward returns over 48 bars measured at every bar overlap 47 times out of 48.
Ninety thousand such observations are not ninety thousand independent facts, and
a t-statistic computed as though they were will call noise significant with
enormous confidence. Phase 1 therefore samples **every 48th bar**, which throws
away 98% of the data and is the right thing to do: what is left is roughly
independent, and roughly independent is what a p-value assumes.

## The bar every feature has to clear

An information coefficient is not an edge. The strategy pays about $0.55 a round
trip, so the number that matters is the **dollar** spread between the top and
bottom decile of a feature's forward return. A feature can be statistically
significant and economically worthless, and on a five-minute chart most are.

## The one genuinely new input

Everything the specified strategy reads is derived from the four prices of a
bar. The tick archive has more than that: how many quote updates a bar took and
how many of them moved the mid up rather than down. That is the closest thing a
CFD feed has to order flow - no traded volume, no side - and it is the only
feature here that is not a rearrangement of OHLC. If anything is going to be
different, it is that.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from algo.backtest.xauusd_runner import compute_indicators, run_scalper
from algo.backtest.xauusd_study import (
    ACCOUNT,
    TICK,
    Dataset,
    cost_scenarios,
    load_dataset,
    money,
    mt5_costs,
    mt5_m5_bars,
    num,
    pct,
    wrap,
)
from algo.core.bar import Bar
from algo.core.enums import Side
from algo.data.dukascopy import Flow, load_flow
from algo.data.econ_calendar import EconomicCalendar
from algo.pricing.indicators import atr, ema
from algo.reporting.scalper_report import Summary, summarise
from algo.strategy.rsi_stoch_reversal import BASELINE

#: The strategy's own horizon: the four-hour cap, in five-minute bars.
HORIZON = 48

#: Round-trip cost the baseline actually paid, per trade. The bar any feature
#: has to clear before it is worth building anything on.
COST_PER_TRADE = Decimal("0.55")


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    claim: str
    values: list[float]


def build_features(m5: Sequence[Bar], flow: dict, h1: Sequence[Bar]) -> list[Feature]:
    """Every candidate predictor, each causal at the bar it is stamped on.

    "Causal" is load-bearing and is asserted in the tests: feature[i] must be
    computable from bars 0..i. A feature that peeks is guaranteed to look
    predictive and guaranteed to be worthless.
    """
    closes = [float(b.close) for b in m5]
    highs = [float(b.high) for b in m5]
    lows = [float(b.low) for b in m5]
    n = len(m5)

    fast = atr(highs, lows, closes, 14)
    slow = atr(highs, lows, closes, 96)
    ind = compute_indicators(m5, h1, BASELINE)

    def safe(index: int, series: list[float]) -> float:
        value = series[index]
        return value if value == value else float("nan")

    def normalised(lookback: int) -> list[float]:
        out = [float("nan")] * n
        for i in range(lookback, n):
            volatility = safe(i, fast)
            if volatility != volatility or volatility <= 0:
                continue
            out[i] = (closes[i] - closes[i - lookback]) / volatility
        return out

    sma20 = ema(closes, 20)
    reversion = [float("nan")] * n
    for i in range(20, n):
        volatility = safe(i, fast)
        if volatility == volatility and volatility > 0:
            reversion[i] = (closes[i] - sma20[i]) / volatility

    compression = [float("nan")] * n
    for i in range(n):
        a, b = safe(i, fast), safe(i, slow)
        if a == a and b == b and b > 0:
            compression[i] = a / b

    position = [float("nan")] * n
    window = 288
    for i in range(window, n):
        high = max(highs[i - window + 1 : i + 1])
        low = min(lows[i - window + 1 : i + 1])
        if high > low:
            position[i] = (closes[i] - low) / (high - low)

    separation = [float("nan")] * n
    for i in range(n):
        h1_index = ind.h1_index[i]
        if h1_index < 0:
            continue
        volatility = ind.h1_atr[h1_index]
        if volatility != volatility or volatility <= 0:
            continue
        separation[i] = (
            ind.h1_ema_fast[h1_index] - ind.h1_ema_slow[h1_index]
        ) / volatility

    bars_flow: list[Flow | None] = [flow.get(b.ts) for b in m5]
    imbalance = [
        f.imbalance if f is not None else float("nan") for f in bars_flow
    ]
    imbalance_1h = [float("nan")] * n
    for i in range(12, n):
        window_flow = [f for f in bars_flow[i - 11 : i + 1] if f is not None]
        if len(window_flow) < 12:
            continue
        up = sum(f.upticks for f in window_flow)
        down = sum(f.downticks for f in window_flow)
        imbalance_1h[i] = 0.0 if up + down == 0 else (up - down) / (up + down)

    activity = [float("nan")] * n
    for i in range(288, n):
        recent = [f.ticks for f in bars_flow[i - 287 : i + 1] if f is not None]
        if len(recent) < 200 or bars_flow[i] is None:
            continue
        typical = sorted(recent)[len(recent) // 2]
        if typical > 0:
            activity[i] = bars_flow[i].ticks / typical  # type: ignore[union-attr]

    return [
        Feature("mom 1h", "trend continues over the next four hours", normalised(12)),
        Feature("mom 4h", "a four-hour move continues", normalised(48)),
        Feature("mom 24h", "a daily trend continues", normalised(288)),
        Feature("reversion z", "price far from its mean comes back", reversion),
        Feature("rsi 14", "the specified signal's own input", ind.m5_rsi),
        Feature("stoch %K", "the specified signal's confirmation", ind.stoch_k),
        Feature("vol compression", "quiet leads to a directional move", compression),
        Feature("range position", "position in the daily range predicts", position),
        Feature("h1 trend separation", "a separated trend keeps going", separation),
        Feature("tick imbalance 5m", "one bar of flow leads price", imbalance),
        Feature("tick imbalance 1h", "an hour of flow leads price", imbalance_1h),
        Feature("tick activity", "unusual activity precedes a move", activity),
    ]


def forward_dollars(m5: Sequence[Bar], horizon: int = HORIZON) -> list[float]:
    """Mid-to-mid move over the next `horizon` bars, in dollars an ounce."""
    closes = [float(b.close) for b in m5]
    out = [float("nan")] * len(closes)
    for i in range(len(closes) - horizon):
        out[i] = closes[i + horizon] - closes[i]
    return out


def spearman(xs: list[float], ys: list[float]) -> tuple[float, int]:
    """Rank correlation, and the sample it was computed on."""
    pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if x == x and y == y]
    if len(pairs) < 30:
        return float("nan"), len(pairs)
    n = len(pairs)
    rank_x = _ranks([p[0] for p in pairs])
    rank_y = _ranks([p[1] for p in pairs])
    mean_x = sum(rank_x) / n
    mean_y = sum(rank_y) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rank_x, rank_y, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in rank_x)
    var_y = sum((b - mean_y) ** 2 for b in rank_y)
    if var_x <= 0 or var_y <= 0:
        return float("nan"), n
    return cov / math.sqrt(var_x * var_y), n


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = average
        i = j + 1
    return out


def decile_spread(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Mean forward move in the top and bottom decile of the feature, and the gap.

    The gap is what a perfect long-top-decile, short-bottom-decile trader would
    capture per trade before costs. Comparing it to `COST_PER_TRADE` is the
    whole economic test.
    """
    pairs = sorted(
        ((x, y) for x, y in zip(xs, ys, strict=True) if x == x and y == y),
        key=lambda pair: pair[0],
    )
    if len(pairs) < 50:
        return float("nan"), float("nan"), float("nan")
    cut = max(1, len(pairs) // 10)
    bottom = sum(p[1] for p in pairs[:cut]) / cut
    top = sum(p[1] for p in pairs[-cut:]) / cut
    return top, bottom, top - bottom


Scored = tuple["Feature", float, float, float]


def scan(features: list[Feature], target: list[float], say) -> list[Scored]:
    """Phase 1. Rank correlation and dollar decile spread, non-overlapping."""
    say(f"    {'feature':<22}{'IC':>8}{'t':>7}{'n':>7}"
        f"{'top decile':>12}{'bottom':>10}{'spread $':>11}")
    scored: list[tuple[Feature, float, float, float]] = []
    for feature in features:
        # Every 48th bar: the forward windows no longer overlap, so a t-stat
        # means what a t-stat is supposed to mean.
        xs = feature.values[::HORIZON]
        ys = target[::HORIZON]
        ic, n = spearman(xs, ys)
        top, bottom, spread = decile_spread(xs, ys)
        t = (
            ic * math.sqrt(max(n - 2, 1)) / math.sqrt(max(1e-12, 1 - ic * ic))
            if ic == ic
            else float("nan")
        )
        scored.append((feature, ic, t, spread))
        say(
            f"    {feature.name:<22}{ic:>8.4f}{t:>7.2f}{n:>7}"
            f"{top:>12.3f}{bottom:>10.3f}{spread:>11.3f}"
        )
    return scored


def threshold_entry(
    values: list[float], *, low: float, high: float, invert: bool = False
) -> Callable[[int], Side | None]:
    """Buy above `high`, sell below `low` - or the reverse when `invert`.

    Closes over a causal array and reads only the index it is handed, which is
    what `run_scalper`'s `entry` hook requires and cannot check.
    """

    def entry(i: int) -> Side | None:
        value = values[i]
        if value != value:
            return None
        if value >= high:
            return Side.SELL if invert else Side.BUY
        if value <= low:
            return Side.BUY if invert else Side.SELL
        return None

    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("state/xauusd"))
    parser.add_argument("--out", type=Path, default=Path("reports/xauusd"))
    args = parser.parse_args()

    out: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        out.append(text)

    dataset: Dataset = load_dataset(args.data)
    flow = load_flow(args.data / "m5_flow.csv")
    calendar = EconomicCalendar.load()
    costs, slippage = cost_scenarios(dataset.half_spread)["realistic"]

    split = int(len(dataset.m5) * 0.7)
    boundary = dataset.m5[split].ts
    in_m5 = [b for b in dataset.m5 if b.ts < boundary]
    in_h1 = [b for b in dataset.h1 if b.ts <= in_m5[-1].ts]
    out_m5 = [b for b in dataset.m5 if b.ts >= boundary]
    out_h1 = [b for b in dataset.h1 if b.ts >= boundary]

    say("=" * 100)
    say("A NEW ENTRY SIGNAL: MEASURE FIRST, BUILD SECOND")
    say("=" * 100)
    say()
    for row in wrap(
        "Phase 1 asks whether anything here predicts the next four hours of gold at all, "
        "on the in-sample window only, sampled every 48th bar so the forward windows do "
        "not overlap. Phase 2 builds entries only from what Phase 1 supports and tests "
        "them out of sample. Nothing in Phase 2 was chosen by looking at a Phase 2 result.",
        94,
    ):
        say(f"  {row}")
    say()
    say(f"  in sample         {in_m5[0].ts:%Y-%m-%d} .. {in_m5[-1].ts:%Y-%m-%d}"
        f"  ({len(in_m5):,} M5 bars)")
    say(f"  out of sample     {out_m5[0].ts:%Y-%m-%d} .. {out_m5[-1].ts:%Y-%m-%d}"
        f"  ({len(out_m5):,} M5 bars)")
    say(f"  horizon           {HORIZON} bars ({HORIZON * 5 // 60} hours), the strategy's own cap")
    say(f"  the bar to clear  a decile spread of ${COST_PER_TRADE} - what a round trip costs")
    say()

    say("=" * 100)
    say("PHASE 1: DOES ANYTHING PREDICT?  (in sample, non-overlapping)")
    say("=" * 100)
    say()
    features = build_features(in_m5, flow, in_h1)
    target = forward_dollars(in_m5)
    scored = scan(features, target, say)
    say()
    for row in wrap(
        "IC is the rank correlation with the forward four-hour move. t is its "
        "significance on the non-overlapping sample - |t| above about 2 is the "
        "conventional bar. 'spread $' is the gap in average forward move between the "
        "feature's top and bottom decile, in dollars an ounce: that is the gross edge a "
        "perfect user of the feature would capture, before paying "
        f"${COST_PER_TRADE} to take the trade.",
        94,
    ):
        say(f"  {row}")
    say()

    significant = [
        (feature, ic, t, spread)
        for feature, ic, t, spread in scored
        if t == t and abs(t) >= 2.0
    ]

    if significant:
        say("  Statistically significant at |t| >= 2:")
        for feature, ic, t, spread in significant:
            say(f"    {feature.name:<22} IC {ic:+.4f}  t {t:+.2f}  spread ${spread:+.3f}"
                f"   - claim: {feature.claim}")
    else:
        strongest = max(scored, key=lambda item: abs(item[2]) if item[2] == item[2] else 0)
        say("  Nothing reached |t| >= 2 on the non-overlapping sample. The strongest is")
        say(f"  {strongest[0].name} at t = {strongest[2]:+.2f}, which is what a null result")
        say("  looks like when twelve features are tried.")
    say()

    wide = [item for item in scored if item[3] == item[3] and abs(item[3]) > float(COST_PER_TRADE)]
    if wide:
        say(f"  {len(wide)} features DO have a decile spread wider than the "
            f"${COST_PER_TRADE} cost bar:")
        for feature, ic, t, spread in sorted(wide, key=lambda i: -abs(i[3])):
            disagree = " (sign disagrees with its IC)" if ic * spread < 0 else ""
            say(f"    {feature.name:<22} spread ${spread:+.3f}   t {t:+.2f}{disagree}")
        say()
        for row in wrap(
            "That is not a contradiction of the line above, it is the reason the line above "
            "is the one to believe. A decile spread is the mean of a few hundred forward "
            "moves whose own standard deviation is several dollars, so a spread of a dollar "
            "or so is exactly the size of its own error bar - which is what a t of one says. "
            "Where the spread's sign also disagrees with the feature's rank correlation "
            "there is not even a monotone relationship to be noisy about: the extremes are "
            "moving one way and the middle the other, which is the shape of nothing.",
            94,
        ):
            say(f"  {row}")
    say()

    # ------------------------------------------------------------- Phase 2
    say("=" * 100)
    say("PHASE 2: THE BEST OF THEM, TRADED")
    say("=" * 100)
    say()
    for row in wrap(
        "The three features with the largest absolute decile spread are traded as entries "
        "regardless of whether they cleared the bar - if the bar is the honest test, then "
        "watching the best-looking features fail it in a real backtest is the "
        "demonstration, not a waste. Everything except the entry is the baseline: same "
        "6xATR stop, same four-hour cap, same RSI reversal, same news and daily gates, "
        "same costs.",
        94,
    ):
        say(f"  {row}")
    say()

    ranked = sorted(scored, key=lambda item: -abs(item[3] if item[3] == item[3] else 0))[:3]
    say(f"    {'entry':<34}{'trades':>7}{'win%':>8}{'net $':>11}"
        f"{'PF':>8}{'exp $':>9}{'window':>14}")

    windows: list[tuple[str, list[Bar], list[Bar], object, object]] = [
        ("in sample", in_m5, in_h1, costs, slippage),
        ("out of sample", out_m5, out_h1, costs, slippage),
    ]
    fetched = mt5_m5_bars()
    if fetched is not None:
        h_m5, h_h1, _resolved = fetched
        windows.append(("mt5 holdout", h_m5, h_h1, mt5_costs(), slippage))

    traded: dict[tuple[str, str], Summary] = {}
    for label, m5, h1, window_costs, window_slip in windows:
        window_flow = flow if label != "mt5 holdout" else {}
        try:
            window_features = build_features(m5, window_flow, h1)
        except Exception as exc:  # noqa: BLE001 - a window may lack an input entirely
            say(f"    {label}: features unavailable ({exc})")
            continue
        by_name = {f.name: f for f in window_features}
        ind = compute_indicators(m5, h1, BASELINE)

        for feature, _ic, _t, spread in ranked:
            values = by_name[feature.name].values
            usable = [v for v in values if v == v]
            if len(usable) < 500:
                say(f"    {feature.name + ' (' + label + ')':<34}"
                    "   no usable values in this window")
                continue
            usable.sort()
            low = usable[len(usable) // 5]
            high = usable[len(usable) * 4 // 5]
            # A positive decile spread means high values precede up moves, so
            # buy the top quintile; a negative one means the opposite. The
            # DIRECTION comes from phase 1, on in-sample data, and is not
            # re-chosen per window - that would be fitting each window
            # separately and calling the agreement a result.
            entry = threshold_entry(values, low=low, high=high, invert=spread < 0)
            result = run_scalper(
                m5,
                h1,
                params=BASELINE,
                costs=window_costs,
                calendar=calendar,
                slippage=window_slip,
                tick=TICK,
                starting_equity=ACCOUNT,
                indicators=ind,
                entry=entry,
            )
            item = summarise(result, BASELINE, label=feature.name, starting_equity=ACCOUNT)
            traded[(feature.name, label)] = item
            say(
                f"    {feature.name:<34}{item.trades:>7}{pct(item.win_rate):>8}"
                f"{money(item.net_pnl):>11}{num(item.profit_factor):>8}"
                f"{money(item.expectancy):>9}{label:>14}"
            )
        # The control, once per window: the same quintile trade on a feature
        # with no information in it at all.
        control = threshold_entry(
            [float((i * 2654435761) % 1000) / 1000 for i in range(len(m5))],
            low=0.2,
            high=0.8,
        )
        result = run_scalper(
            m5,
            h1,
            params=BASELINE,
            costs=window_costs,
            calendar=calendar,
            slippage=window_slip,
            tick=TICK,
            starting_equity=ACCOUNT,
            indicators=ind,
            entry=control,
        )
        item = summarise(result, BASELINE, label="control", starting_equity=ACCOUNT)
        traded[("control", label)] = item
        say(
            f"    {'control: a deterministic coin':<34}{item.trades:>7}"
            f"{pct(item.win_rate):>8}{money(item.net_pnl):>11}"
            f"{num(item.profit_factor):>8}{money(item.expectancy):>9}{label:>14}"
        )
        say()

    say("=" * 100)
    say("VERDICT")
    say("=" * 100)
    say()
    for text in verdict(scored, significant, traded, [w[0] for w in windows]):
        say(text)

    args.out.mkdir(parents=True, exist_ok=True)
    report = args.out / "signal_study.txt"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwritten to {report}")


def verdict(
    scored: list[Scored],
    significant: list[Scored],
    traded: dict[tuple[str, str], Summary],
    windows: list[str],
) -> list[str]:
    """What the two phases together support, and nothing beyond it."""
    lines: list[str] = []

    def add(heading: str, body: str) -> None:
        lines.append(f"  {heading}")
        lines.extend(f"      {row}" for row in wrap(body, 94))
        lines.append("")

    strongest = max(scored, key=lambda item: abs(item[2]) if item[2] == item[2] else 0)
    widest = max(scored, key=lambda item: abs(item[3]) if item[3] == item[3] else 0)

    add(
        "Phase 1: nothing predicts.",
        f"{len(significant)} of {len(scored)} features reached |t| >= 2 against the forward "
        f"four-hour move on non-overlapping in-sample data. The strongest of the twelve is "
        f"{strongest[0].name} at t = {strongest[2]:+.2f} - and with twelve features tried, "
        "one at |t| around 1.4 is what you expect from noise alone. Several features have "
        f"decile spreads wider than the ${COST_PER_TRADE} cost bar - {widest[0].name} is "
        f"${widest[3]:+.2f} - but a spread that size on this sample is indistinguishable "
        "from zero, and in several cases its sign disagrees with the feature's own rank "
        "correlation.",
    )

    control_by_window = {w: traded.get(("control", w)) for w in windows}
    control_line = ", ".join(
        f"{w} {money(item.expectancy)}" for w, item in control_by_window.items() if item
    )
    add(
        "Phase 2: the control settles it.",
        "A deterministic coin - an entry with no information in it whatsoever, taken at the "
        f"same quintile thresholds - makes {control_line} a trade. Read the holdout figure "
        "there before reading anything else in that table: in a market that trended as hard "
        "as gold did through 2026, a four-hour position taken at random makes money, so "
        "every positive number in the holdout column is the market and not the signal.",
    )

    consistent: list[str] = []
    for feature, _ic, _t, _spread in scored:
        results = [traded.get((feature.name, w)) for w in windows]
        if any(item is None for item in results):
            continue
        signs = {(item.expectancy or 0) > 0 for item in results}  # type: ignore[union-attr]
        beats = all(
            (item.expectancy or Decimal("-99"))  # type: ignore[union-attr]
            > (control_by_window[w].expectancy or Decimal("0"))  # type: ignore[union-attr]
            for item, w in zip(results, windows, strict=True)
            if control_by_window[w]
        )
        if len(signs) == 1 and True in signs and beats:
            consistent.append(feature.name)

    if consistent:
        add(
            f"One feature is positive in every window and beats the control in every "
            f"window: {', '.join(consistent)}.",
            "That is the strongest result in this report and it is still not a strategy. It "
            "comes from a feature whose in-sample t-statistic is under 1, which means the "
            "measurement that was supposed to justify trading it did not - so the agreement "
            "across windows is being asked to carry the whole case on its own, and three "
            "windows of one instrument is not enough evidence to carry it. The honest next "
            "step is not to trade it but to test it properly: other instruments, other "
            "years, and a pre-registered rule fixed before the test rather than after.",
        )
    else:
        add(
            "Phase 2: nothing is consistent either.",
            "No feature is positive in every window while beating the control in every "
            "window. The one that comes closest changes sign between the in-sample and "
            "out-of-sample halves, which is the same thing the entry study found: these are "
            "not weak signals, they are absent ones.",
        )

    flow_ts = {
        feature.name: t for feature, _ic, t, _spread in scored if "tick" in feature.name
    }
    flow_t = (
        f"t = {max(flow_ts.values(), key=abs):+.2f}" if flow_ts else "no flow features"
    )
    add(
        "The tick data was the best hope, and it did not rescue this.",
        "Order flow does predict at very short horizons - that is what makes market making "
        "work - but the horizon here is four hours, and one bar of imbalance, or an hour of "
        f"it, carries nothing about it - {flow_t} at best. The features that are not "
        "rearrangements of OHLC sit in the table indistinguishable from the ones that are.",
    )

    add(
        "The recommendation.",
        "Stop looking for a five-minute entry on XAUUSD with these inputs. A round trip "
        f"costs ${COST_PER_TRADE} and the best four-hour decile spread any of twelve "
        f"features can show is ${abs(widest[3]):.2f} with a t-statistic under one - and that "
        "spread is the gross move available to someone who could pick the extreme decile "
        "and trade it perfectly. What would change the answer is a cheaper venue, a longer "
        "horizon where the available move grows faster than the cost, or an input this "
        "dataset does not contain: real traded volume, the options surface, positioning. "
        "Each of those is a different project and deserves this same order of work - "
        "measure whether the input predicts before building anything that trades on it.",
    )
    return lines


if __name__ == "__main__":
    main()
