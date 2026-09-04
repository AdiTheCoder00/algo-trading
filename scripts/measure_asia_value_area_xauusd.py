"""Shape test: the Asia-open value-area fade on XAUUSD, over real M1 bars.

The setup, as it was described: mark 05:30 IST, draw a volume profile over
05:30-05:45, and fade the first return inside it in the direction the session
already had. Everything below is that rule made arithmetic, so it can be
measured rather than argued about.

Not routed through `BacktestEngine`, and not through `run_cfd_backtest` either.
That runner decides on every bar and manages the exit with a percentage stop
and trail; this strategy takes at most one trade a day, off a level computed
from a fixed fifteen-minute window, with a *structural* stop at a price the
market actually printed rather than a percentage. Bending the percentage runner
into that shape would have meant lying to it about what the stop was. The
precedent is `algo/backtest/bhavcopy_runner.py` and
`scripts/measure_macd_xauusd.py`: a bespoke walk over a different data shape,
with the shared parts - `Bar`, the measured spread profile, the server-clock
resolution - imported rather than reimplemented.

## The two assumptions, because the source rule is silent on both

The video gives an entry and a target and nothing else. Two things had to be
decided before this could run at all, and they are decisions, not readings:

1. **The stop.** There is none in the original. Without one the strategy has no
   defined loss and the result would be uninterpretable in exactly the way
   D-145's martingale was. This uses the *liquidity wick*: for a short, the
   highest price printed during the excursion above the value-area high - the
   level whose breach means the read was wrong. Nothing is added to it, so this
   is the tightest honest reading of "beyond the wick"; a wider stop trades win
   rate against size of loss and does not change the sign of the edge.

2. **The bias.** "Morning se direction kya hai" is not a formula. This uses the
   broker's own session: the change from the session open (00:00 server time,
   21:00 UTC on this broker's +3 clock, ~02:30 IST) to 00:00 UTC. A down
   session means fade the move above the value area; an up session means fade
   the move below it. A session flat to the cent is no trade.

Both are stated here rather than buried because they are the only places a
reader could reasonably have chosen differently, and either choice moves the
number.

## What the data can and cannot support

This broker serves M1 back to 2026-05-26 and no earlier - checked, not assumed.
A fifteen-minute volume profile needs M1: three M5 bars is not a distribution.
So the window is about fourteen weeks, roughly seventy samples of a once-a-day
setup. **That is a shape test and not a verdict**, and it is the same
sample-size trap D-145 was written about: seventy sessions cannot contain a
regime change. The honest use of this number is to see whether the effect is
anywhere near large enough to justify an EA, not to decide that it works.

Two further limits, both structural rather than fixable:

- **The profile is built from tick volume, not traded volume.** A CFD feed has
  no traded volume; MT5's `tick_volume` counts quote updates. Every value area
  here is therefore a distribution of quotes, and will not match a value area
  drawn on a futures feed. This is not an approximation that improves with more
  data - it is a different measurement wearing the same name.
- **Bars, not ticks.** A stop and a target inside the same M1 bar cannot be
  ordered from OHLC, so the stop is assumed to have come first. That is the
  pessimistic choice and the same one `run_cfd_backtest` makes.

A control run is reported alongside the strategy: the identical rules with the
session bias **inverted**. If fading and chasing pay the same, the direction
filter - the part the source rule leans on hardest - carries no information,
and the sign of the result is spread and structure rather than a read.

Position size is a flat 100 engine lots - one MT5 lot, 100 ounces, $100 a
dollar of gold - matching the project's fixed-lot position (D-089). The implied
risk is reported, never auto-scaled.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

# The editable install of `algo` points at a path this checkout no longer lives
# at, so running this file directly cannot import the package without help.
# Same bootstrap as `scripts/mcx_live_to_excel.py` and its neighbours.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from algo.core.bar import M1, Bar
from algo.core.enums import Side
from algo.data.mt5_history import (
    fetch_history_range,
    resolve_server_offset,
)
from algo.data.mt5_spread import SpreadProfile, load_profile

SYMBOL = "XAUUSD"

#: One MT5 lot. An engine lot is one ounce, so a $1 move is $100.
LOTS = 100

#: Price bucket for the volume profile, in dollars. Ten points: fine enough
#: that a $3 opening range is thirty buckets rather than three, coarse enough
#: that fifteen bars of tick volume are not spread into noise.
BUCKET = Decimal("0.10")

#: Share of the window's tick volume the value area encloses. 70% is the
#: convention the technique is always quoted with; it is not tuned here.
VALUE_AREA_SHARE = Decimal("0.70")

#: The profile window, in UTC. 00:00-00:15 UTC is 05:30-05:45 IST.
PROFILE_START = time(0, 0)
PROFILE_END = time(0, 15)

#: Entries are accepted until this UTC time, and any open position is closed at
#: `FLAT_BY`. Both are assumptions - the source rule bounds neither. 06:00 UTC
#: keeps the trade inside the Asia session it claims to be about; 12:00 UTC is
#: comfortably before the 21:00 rollover, so no position is ever financed
#: overnight and swap never enters the arithmetic.
ENTRY_UNTIL = time(6, 0)
FLAT_BY = time(12, 0)

#: A session needs at least this many M1 bars before 00:00 UTC for its
#: direction to mean anything. Three hours of a normally six-hour run-up.
MIN_BIAS_BARS = 60


@dataclass(frozen=True, slots=True)
class ValueArea:
    """The 00:00-00:15 UTC profile, reduced to the three levels used."""

    poc: Decimal
    high: Decimal
    low: Decimal
    total_volume: int

    @property
    def width(self) -> Decimal:
        return self.high - self.low


@dataclass
class Trade:
    """One round trip. Spread is the only cost that can apply intraday."""

    session: date
    side: Side
    entry_ts: datetime
    entry_price: Decimal
    stop: Decimal
    target: Decimal
    value_area: ValueArea
    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str = ""
    spread_paid: Decimal = Decimal("0")

    @property
    def risk(self) -> Decimal:
        """Distance to the stop, in dollars of gold."""
        return abs(self.entry_price - self.stop)

    @property
    def reward(self) -> Decimal:
        return abs(self.target - self.entry_price)

    @property
    def gross_pnl(self) -> Decimal:
        if self.exit_price is None:
            return Decimal("0")
        move = self.exit_price - self.entry_price
        signed = move if self.side is Side.BUY else -move
        return signed * Decimal(LOTS)

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.spread_paid


@dataclass
class Skips:
    """Why sessions produced no trade. Counted, so the denominator is visible."""

    reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def note(self, reason: str) -> None:
        self.reasons[reason] += 1

    @property
    def total(self) -> int:
        return sum(self.reasons.values())


def fetch_m1(*, months_back: int) -> tuple[list[Bar], timedelta, str]:
    """Every M1 bar the terminal will serve, oldest first, stamped in UTC.

    Walked in month-long chunks because `copy_rates_range` returns nothing at
    all - not an error - when asked for a span this long in one call, and
    because the terminal's "max bars in chart" cap makes the count-backwards
    fetcher stop about seven weeks short of what the server actually holds.
    """
    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select(SYMBOL, True):
        mt5.shutdown()
        raise SystemExit(f"could not select {SYMBOL}: {mt5.last_error()}")

    resolved = resolve_server_offset(mt5, SYMBOL)
    now = datetime.now(UTC)
    seen: dict[datetime, Bar] = {}
    cursor = (now - timedelta(days=31 * months_back)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    while cursor < now:
        nxt = (cursor + timedelta(days=32)).replace(day=1)
        for bar in fetch_history_range(
            mt5,
            symbol=SYMBOL,
            timeframe=M1,
            start=cursor,
            end=min(nxt, now),
            offset=resolved.offset,
        ):
            seen[bar.ts] = bar
        cursor = nxt
    mt5.shutdown()

    bars = sorted(seen.values(), key=lambda b: b.ts)
    if not bars:
        raise SystemExit("MT5 served no M1 history at all")
    return bars, resolved.offset, resolved.describe()


def value_area(bars: list[Bar]) -> ValueArea | None:
    """Volume-by-price over `bars`, reduced to POC and the 70% value area.

    Each bar's tick volume is spread evenly across the buckets its range
    touches - the standard reconstruction when only OHLC is available, and the
    reason this is a profile of where quotes happened rather than of where size
    traded. The area then grows one bucket at a time toward whichever
    neighbour holds more volume, which is the textbook expansion.
    """
    if not bars:
        return None
    volume_at: dict[int, Decimal] = defaultdict(Decimal)
    for bar in bars:
        lo = int(bar.low / BUCKET)
        hi = int(bar.high / BUCKET)
        share = Decimal(bar.volume) / Decimal(hi - lo + 1)
        for index in range(lo, hi + 1):
            volume_at[index] += share
    total = sum(volume_at.values(), Decimal("0"))
    if total <= 0:
        return None

    # Lowest index wins a tie, so the same bars always give the same POC.
    poc_index = max(volume_at, key=lambda i: (volume_at[i], -i))
    lower = upper = poc_index
    enclosed = volume_at[poc_index]
    goal = total * VALUE_AREA_SHARE
    lowest, highest = min(volume_at), max(volume_at)
    while enclosed < goal and (lower > lowest or upper < highest):
        below = volume_at.get(lower - 1, Decimal("0")) if lower > lowest else Decimal("-1")
        above = volume_at.get(upper + 1, Decimal("0")) if upper < highest else Decimal("-1")
        if above >= below:
            upper += 1
            enclosed += volume_at.get(upper, Decimal("0"))
        else:
            lower -= 1
            enclosed += volume_at.get(lower, Decimal("0"))

    return ValueArea(
        poc=Decimal(poc_index) * BUCKET,
        high=Decimal(upper + 1) * BUCKET,
        low=Decimal(lower) * BUCKET,
        total_volume=int(total),
    )


def session_bars(bars: list[Bar], offset: timedelta) -> dict[date, list[Bar]]:
    """Bars grouped by the broker's own trading day.

    The session rolls at 00:00 server time, which this broker's +3 clock puts
    at 21:00 UTC - the boundary D-121 measured. A bar at 22:00 UTC on Monday
    therefore belongs to Tuesday's session, and the 00:00-00:15 UTC window sits
    a few hours into it rather than at its edge, which is the whole reason
    "direction since the open" has an answer at all.
    """
    rollover = (datetime(2000, 1, 1, tzinfo=UTC) - offset).time()
    grouped: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        day = bar.ts.date()
        if bar.ts.time() >= rollover:
            day = day + timedelta(days=1)
        grouped[day].append(bar)
    return grouped


def session_bias(day: date, day_bars: list[Bar]) -> int | None:
    """Direction from the session open to 00:00 UTC: -1 down, +1 up.

    None when the session is too short to read or closed the window exactly
    where it opened - both cases where the source rule's first question has no
    answer, and inventing one would put a coin flip in front of every trade.
    """
    boundary = datetime.combine(day, PROFILE_START, tzinfo=UTC)
    pre = [b for b in day_bars if b.ts < boundary]
    if len(pre) < MIN_BIAS_BARS:
        return None
    opened, closed = pre[0].open, pre[-1].close
    if closed == opened:
        return None
    return 1 if closed > opened else -1


def simulate_session(
    day: date,
    day_bars: list[Bar],
    *,
    invert_bias: bool,
    spread: SpreadProfile,
    skips: Skips,
) -> Trade | None:
    """One session, at most one trade. Returns None with a counted reason."""
    bias = session_bias(day, day_bars)
    if bias is None:
        skips.note("no readable session direction before 00:00 UTC")
        return None
    if invert_bias:
        bias = -bias

    window = [b for b in day_bars if PROFILE_START <= b.ts.time() < PROFILE_END]
    if len(window) < 15:
        skips.note("incomplete 00:00-00:15 UTC window")
        return None
    area = value_area(window)
    if area is None:
        skips.note("no volume in the profile window")
        return None

    after = [b for b in day_bars if PROFILE_END <= b.ts.time() < FLAT_BY]
    if not after:
        skips.note("no bars after the profile window")
        return None

    # A short fades the excursion *above* the value area on a down session; a
    # long fades the excursion below it on an up session.
    short = bias < 0
    level = area.high if short else area.low
    excursion: Decimal | None = None
    for index, bar in enumerate(after):
        if bar.ts.time() >= ENTRY_UNTIL:
            skips.note("no signal inside the entry window")
            return None

        beyond = bar.high > level if short else bar.low < level
        if beyond:
            reach = bar.high if short else bar.low
            if excursion is None:
                excursion = reach
            else:
                excursion = max(excursion, reach) if short else min(excursion, reach)
        if excursion is None:
            continue

        returned = bar.close < level if short else bar.close > level
        if not returned:
            continue

        # Decided on this closed bar, filled at the next one's open. There is
        # no next bar at the end of the window, and inventing one would be the
        # look-ahead this codebase builds `BarWindow` to prevent.
        if index + 1 >= len(after):
            skips.note("signal on the last bar of the window")
            return None
        entry_bar = after[index + 1]
        entry = entry_bar.open
        target = area.low if short else area.high
        if (short and entry <= target) or (not short and entry >= target):
            skips.note("target already passed at the fill")
            return None
        if (short and excursion <= entry) or (not short and excursion >= entry):
            skips.note("stop on the wrong side of the fill")
            return None
        return _walk_out(
            Trade(
                session=day,
                side=Side.SELL if short else Side.BUY,
                entry_ts=entry_bar.ts,
                entry_price=entry,
                stop=excursion,
                target=target,
                value_area=area,
            ),
            after[index + 1 :],
            spread=spread,
        )

    skips.note("price never left the value area")
    return None


def _walk_out(trade: Trade, rest: list[Bar], *, spread: SpreadProfile) -> Trade:
    """Carry the trade forward bar by bar until stop, target or the clock.

    The entry bar is included: filling at its open and then ignoring the rest
    of its range would hand the trade a free minute. When a bar covers both the
    stop and the target, the stop is taken - OHLC cannot order two prices
    inside one bar, and assuming the good one came first is precisely the
    flattery D-125 and D-127 exist to refuse.
    """
    short = trade.side is Side.SELL
    for bar in rest:
        hit_stop = bar.high >= trade.stop if short else bar.low <= trade.stop
        hit_target = bar.low <= trade.target if short else bar.high >= trade.target
        if hit_stop:
            gapped = bar.open > trade.stop if short else bar.open < trade.stop
            trade.exit_price = bar.open if gapped else trade.stop
            trade.exit_ts, trade.exit_reason = bar.ts, "stop"
            break
        if hit_target:
            gapped = bar.open < trade.target if short else bar.open > trade.target
            trade.exit_price = bar.open if gapped else trade.target
            trade.exit_ts, trade.exit_reason = bar.ts, "target"
            break
    else:
        last = rest[-1]
        trade.exit_price, trade.exit_ts, trade.exit_reason = last.close, last.ts, "time"

    assert trade.exit_ts is not None
    trade.spread_paid = (
        spread.half_spread_at(trade.entry_ts) + spread.half_spread_at(trade.exit_ts)
    ) * Decimal(LOTS)
    return trade


@dataclass(frozen=True, slots=True)
class Summary:
    label: str
    trades: list[Trade]
    skips: Skips
    sessions: int

    @property
    def net(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal("0"))

    @property
    def gross(self) -> Decimal:
        return sum((t.gross_pnl for t in self.trades), Decimal("0"))

    @property
    def spread(self) -> Decimal:
        return sum((t.spread_paid for t in self.trades), Decimal("0"))

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.trades if t.net_pnl > 0]

    @property
    def profit_factor(self) -> Decimal | None:
        won = sum((t.net_pnl for t in self.wins), Decimal("0"))
        lost = -sum((t.net_pnl for t in self.trades if t.net_pnl <= 0), Decimal("0"))
        return None if lost == 0 else won / lost

    @property
    def per_trade_sd(self) -> Decimal | None:
        """Sample standard deviation of net P&L per trade."""
        n = len(self.trades)
        if n < 2:
            return None
        mean = self.net / Decimal(n)
        variance = sum(
            ((t.net_pnl - mean) ** 2 for t in self.trades), Decimal("0")
        ) / Decimal(n - 1)
        return variance.sqrt()

    @property
    def t_stat(self) -> Decimal | None:
        """How many standard errors the mean trade sits from zero.

        Fifty-odd samples of a once-a-day setup is a small sample, and a result
        this size deserves to be read against its own noise rather than taken
        at face value. |t| under about 2 means the run cannot tell this apart
        from zero either way - which is a finding about the *measurement*, not
        a licence to assume the edge is positive.
        """
        sd = self.per_trade_sd
        n = len(self.trades)
        if sd is None or sd == 0:
            return None
        return (self.net / Decimal(n)) / (sd / Decimal(n).sqrt())

    @property
    def max_drawdown(self) -> Decimal:
        """Worst peak-to-trough of the closed-trade equity curve, in dollars."""
        equity = peak = worst = Decimal("0")
        for trade in self.trades:
            equity += trade.net_pnl
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst


def _money(value: Decimal) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def report(summary: Summary, *, started: date, ended: date, spread_note: str, clock: str) -> None:
    trades = summary.trades
    print(f"\n=== {summary.label} ===")
    print(f"window          {started} .. {ended}  ({summary.sessions} broker sessions)")
    print(f"server clock    {clock}")
    print(f"spread          {spread_note}")
    print("commission      $0.00 a lot, verified against 54 real deals (D-121)")
    print(f"swap            not charged - every position is flat by {FLAT_BY} UTC")
    print(f"size            {LOTS} engine lots (1 MT5 lot, 100oz): $1 of gold = $100")
    print()
    if not trades:
        print("no trades")
    else:
        by_reason: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            by_reason[t.exit_reason].append(t)
        reward = sum((t.reward for t in trades), Decimal("0")) / len(trades)
        risk = sum((t.risk for t in trades), Decimal("0")) / len(trades)
        width = sum((t.value_area.width for t in trades), Decimal("0")) / len(trades)
        print(f"trades          {len(trades)}  ({len(trades) / summary.sessions:.0%} of sessions)")
        print(f"win rate        {len(summary.wins) / len(trades) * 100:.2f}%")
        print(f"gross           {_money(summary.gross)}")
        print(f"spread paid     {_money(-summary.spread)}")
        print(f"net             {_money(summary.net)}")
        print(f"expectancy      {_money(summary.net / len(trades))} a trade")
        pf = summary.profit_factor
        print(f"profit factor   {'n/a' if pf is None else f'{pf:.2f}'}")
        print(f"max drawdown    {_money(summary.max_drawdown)}")
        sd, t_stat = summary.per_trade_sd, summary.t_stat
        if sd is not None and t_stat is not None:
            print(f"per-trade sd    {_money(sd)}")
            verdict = "distinguishable" if abs(t_stat) >= 2 else "NOT distinguishable"
            print(f"t of the mean   {t_stat:.2f}  ({verdict} from zero at n={len(trades)})")
        print()
        print(f"avg value area  ${width:.2f} wide  ({width * 100:.0f} pips at $0.01 a pip)")
        print(f"avg target dist ${reward:.2f}  ({reward * 100:.0f} pips)")
        print(f"avg stop dist   ${risk:.2f}  ({risk * 100:.0f} pips)")
        if risk:
            print(f"avg R:R         {reward / risk:.2f} : 1")
        print()
        for reason in ("target", "stop", "time"):
            hits = by_reason.get(reason, [])
            if hits:
                pnl = sum((t.net_pnl for t in hits), Decimal("0"))
                print(f"  {reason:<7} {len(hits):>3}  {_money(pnl)}")
    print()
    print(f"no trade on {summary.skips.total} sessions:")
    for reason, count in sorted(summary.skips.reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3}  {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Asia value-area fade, measured")
    parser.add_argument("--months", type=int, default=8, help="how far back to ask MT5")
    args = parser.parse_args()

    bars, offset, clock = fetch_m1(months_back=args.months)
    profile = load_profile(SYMBOL, Path("state/mt5_spread_profile.json"))
    if profile is None:
        raise SystemExit(
            "no measured spread profile in state/mt5_spread_profile.json - refusing "
            "to charge a guessed spread to a strategy whose whole target is $2-5"
        )

    grouped = session_bars(bars, offset)
    days = sorted(grouped)
    print(f"M1 bars: {len(bars):,} from {bars[0].ts} to {bars[-1].ts}")

    runs = (("value-area fade, as described", False), ("control: same rules, bias inverted", True))
    for label, invert in runs:
        skips = Skips()
        trades = [
            trade
            for day in days
            if (
                trade := simulate_session(
                    day, grouped[day], invert_bias=invert, spread=profile, skips=skips
                )
            )
        ]
        report(
            Summary(label=label, trades=trades, skips=skips, sessions=len(days)),
            started=days[0],
            ended=days[-1],
            spread_note=profile.describe(),
            clock=clock,
        )


if __name__ == "__main__":
    main()
