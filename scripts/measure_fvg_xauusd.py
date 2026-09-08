"""Shape test: the GoldFairValueGap rules on real XAUUSD bars.

Same spirit, and the same honesty caveats, as `measure_macd_xauusd.py`: real
data (MT5, not a fixture), real costs (D-121's measured spread and swap),
reported as a shape test rather than dressed up as a production backtest.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
It measures the RULES of `mt5/Experts/AlgoGold/GoldFairValueGap.mq5`,
reimplemented here against the same bars the expert would see. It does NOT run
the compiled `.ex5`. The two can disagree, and the places they are most likely
to are named in `DIVERGENCES` below.

The alternative - driving the MT5 Strategy Tester, as D-140 did for the scalper
- needs the terminal closed, and the terminal is currently forward-testing on
demo. This runs read-only against the running terminal instead.

WHY THE SAMPLE IS SMALL
-----------------------
An H4 breakout that also opens a fair value gap, then retraces into it, then
displaces back out on M15, is a rare conjunction. Expect tens of trades over a
window where the M15 scalper took thousands. Profit factor computed on 20 trades
is not a measurement, it is an anecdote with a decimal point. The trade counts
are printed first for that reason.

DIVERGENCES from the expert, stated rather than hidden
------------------------------------------------------
1. Intrabar ordering. When one M15 bar's range contains both the stop and the
   target, this charges the STOP. The expert's broker would fill whichever came
   first, which the bar does not record. Pessimistic by construction.
2. Fills are at the level, or at the bar's open on a gap - the convention
   `price_stop.py` uses and for the reason its docstring gives. No slippage is
   modelled; the expert's market orders would pay some.
3. Spread is charged as a flat per-fill amount (D-121's measured $0.29 round
   trip), not from the live book. The book was wider than that at some point in
   every window here.
4. `TimeCurrent()` in the expert is the moment of the bar close; here it is the
   next bar's open time. They differ by milliseconds live and by nothing here.
5. Requotes, rejects, margin and the broker's stops level do not exist here.

WHAT IT FOUND (D-151)
---------------------
As shipped: 192 setups over eighteen months became 10 entries and 7 closed
trades, all losers, and **not one reached its target**. Rule 6's liquidity
target sits far from entry, so the `MIN_REWARD_RISK` floor rejects the CLOSE
targets and keeps the distant ones - adverse selection written into the rules.

With the target replaced by a flat 2R (the `target_r` diagnostic below): 36
trades, +$20 net across all three windows, average R of +0.10 / -0.00 / -0.03.
The entries do not predict direction. The target rule was hiding a coin flip
behind a 0% hit rate, not causing a loss.

Do not tune this. D-131 established that sweeping thresholds on this data fits
noise, and the loosest setting here is already flat.

Usage:
    python scripts/measure_fvg_xauusd.py
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import MetaTrader5 as mt5

SYMBOL = "XAUUSD"

#: One MT5 lot is 100 ounces. The expert ships 0.05, so 5 ounces, and that is
#: what is measured - changing it here would not change profit factor but
#: would change every drawdown percentage below.
LOTS_MT5 = Decimal("0.05")
OUNCES = LOTS_MT5 * Decimal("100")
DEPOSIT = Decimal("10000")

#: Measured 2026-08-28 (D-121). Half of the $0.29 round-trip spread, per ounce,
#: charged per fill so both legs of a round trip pay it.
HALF_SPREAD = Decimal("0.145")

#: Vantage swap terms, measured live (D-121). Points per night per ounce, at
#: $0.01 per point per ounce.
SWAP_LONG_POINTS = Decimal("-80.54")
SWAP_SHORT_POINTS = Decimal("32.67")
SWAP_POINT_VALUE = Decimal("0.01")

#: The expert's shipped defaults. Kept as one block so a sweep has one place to
#: edit, and so a number here disagreeing with the .mq5 is visible.
LIQUIDITY_LOOKBACK = 12
SETUP_EXPIRY_BARS = 6
MIN_ZONE_ATR_MULT = Decimal("0.15")
ATR_PERIOD = 14
DISPLACE_ATR_MULT = Decimal("0.60")
CONFIRM_WINDOW_BARS = 8
STOP_BUFFER_ATR_MULT = Decimal("0.25")
MIN_REWARD_RISK = Decimal("1.5")
BREAK_EVEN_AT_1R = True
BREAK_EVEN_OFFSET_ATR = Decimal("0.05")
SESSION_START_HOUR = 7
SESSION_END_HOUR = 20
FRIDAY_END_HOUR = 20
MAX_TRADES_PER_DAY = 3

#: D-140's three windows, so the numbers sit beside the scalper's.
WINDOWS = [
    (
        "2026.06-08 (in-sample-ish)",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 8, 31, tzinfo=UTC),
    ),
    ("2026.01-05", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
    ("2025.06-12", datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
]


@dataclass
class Bar:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class Setup:
    side: str  # "BUY" | "SELL"
    zone_top: Decimal
    zone_bottom: Decimal
    target: Decimal
    armed_index: int  # index into the H4 series
    tapped_index: int | None = None


@dataclass
class Trade:
    side: str
    entry_ts: datetime
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk: Decimal
    exit_ts: datetime | None = None
    exit: Decimal | None = None
    reason: str = ""

    @property
    def gross(self) -> Decimal:
        assert self.exit is not None
        move = (self.exit - self.entry) if self.side == "BUY" else (self.entry - self.exit)
        return move * OUNCES

    @property
    def spread_cost(self) -> Decimal:
        return HALF_SPREAD * OUNCES * 2

    @property
    def swap_cost(self) -> Decimal:
        assert self.exit_ts is not None
        nights = 0
        day = self.entry_ts.date()
        while day < self.exit_ts.date():
            day += timedelta(days=1)
            # Wednesday carries the weekend's three nights on this book.
            nights += 3 if day.weekday() == 2 else 1
        pts = SWAP_LONG_POINTS if self.side == "BUY" else SWAP_SHORT_POINTS
        return pts * SWAP_POINT_VALUE * OUNCES * Decimal(nights)

    @property
    def net(self) -> Decimal:
        return self.gross - self.spread_cost + self.swap_cost

    @property
    def r_multiple(self) -> Decimal:
        if self.risk <= 0:
            return Decimal("0")
        return (self.net / OUNCES) / self.risk


@dataclass
class Counters:
    armed: int = 0
    invalidated: int = 0
    expired: int = 0
    tapped: int = 0
    confirm_timeout: int = 0
    confirmed: int = 0
    blocked_session: int = 0
    blocked_daycap: int = 0
    blocked_rr: int = 0
    entered: int = 0


def fetch(tf: int, start: datetime, end: datetime) -> list[Bar]:
    rates = mt5.copy_rates_range(SYMBOL, tf, start, end)
    if rates is None or not len(rates):
        raise SystemExit(f"no bars for {SYMBOL} tf={tf} {start}..{end}: {mt5.last_error()}")
    return [
        Bar(
            ts=datetime.fromtimestamp(int(r["time"]), UTC),
            open=Decimal(str(r["open"])),
            high=Decimal(str(r["high"])),
            low=Decimal(str(r["low"])),
            close=Decimal(str(r["close"])),
        )
        for r in rates
    ]


def wilder_atr(bars: Sequence[Bar], period: int) -> list[Decimal | None]:
    """Wilder's ATR, seeded with the SMA of the first `period` true ranges.

    This is what MT5's own iATR does, and the expert reads iATR - so seeding it
    the other way (first-value, as `algo/pricing/indicators.py` does for the
    MACD EMAs) would make this disagree with the expert on every early bar.
    """
    out: list[Decimal | None] = [None] * len(bars)
    trs: list[Decimal] = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
            continue
        prev = bars[i - 1].close
        trs.append(max(b.high - b.low, abs(b.high - prev), abs(b.low - prev)))
    if len(bars) <= period:
        return out
    seed = sum(trs[1 : period + 1], Decimal("0")) / Decimal(period)
    out[period] = seed
    atr = seed
    for i in range(period + 1, len(bars)):
        atr = (atr * Decimal(period - 1) + trs[i]) / Decimal(period)
        out[i] = atr
    return out


def try_arm(h4: Sequence[Bar], i: int, atr: Decimal | None) -> Setup | None:
    """Rules 2 and 3, tested together on the bar at index `i`."""
    if i < 2 or i - LIQUIDITY_LOOKBACK < 0:
        return None
    c0, h0, l0 = h4[i].close, h4[i].high, h4[i].low
    h1, l1 = h4[i - 1].high, h4[i - 1].low
    h2, l2 = h4[i - 2].high, h4[i - 2].low

    broke_up, broke_down = c0 > h1, c0 < l1
    if broke_up == broke_down:  # neither, or an outside bar beyond both
        return None

    if broke_up:
        if not (l0 > h2):
            return None
        bottom, top, side = h2, l0, "BUY"
    else:
        if not (h0 < l2):
            return None
        bottom, top, side = h0, l2, "SELL"

    width = top - bottom
    if width <= 0:
        return None
    if MIN_ZONE_ATR_MULT > 0 and atr is not None and width < MIN_ZONE_ATR_MULT * atr:
        return None

    window = h4[i - LIQUIDITY_LOOKBACK : i]
    target = max(b.high for b in window) if broke_up else min(b.low for b in window)
    return Setup(side=side, zone_top=top, zone_bottom=bottom, target=target, armed_index=i)


def session_allows(ts: datetime) -> bool:
    dow = ts.isoweekday() % 7  # Sunday 0, matching MQL5's day_of_week
    if dow in (0, 6):
        return False
    h = ts.hour
    if SESSION_START_HOUR != SESSION_END_HOUR:
        inside = (
            SESSION_START_HOUR <= h < SESSION_END_HOUR
            if SESSION_START_HOUR < SESSION_END_HOUR
            else (h >= SESSION_START_HOUR or h < SESSION_END_HOUR)
        )
        if not inside:
            return False
    return not (FRIDAY_END_HOUR >= 0 and dow == 5 and h >= FRIDAY_END_HOUR)


def run_window(
    label: str,
    start: datetime,
    end: datetime,
    target_r: Decimal | None = None,
) -> tuple[list[Trade], Counters]:
    """`target_r` replaces rule 6's liquidity target with a fixed R multiple.

    A DIAGNOSTIC, not a strategy option. The first run of this script took 10
    trades in eighteen months and none of them reached the target, while the
    reward:risk gate rejected 33 of 70 confirmed setups - the shape of a target
    that is systematically out of reach. Setting this isolates that: same
    entries, same stops, only the exit target changes. See the docstring note in
    the module docstring for what the comparison said.
    """
    warm = timedelta(days=30)
    h4 = fetch(mt5.TIMEFRAME_H4, start - warm, end)
    m15 = fetch(mt5.TIMEFRAME_M15, start - warm, end)
    m15_atr = wilder_atr(m15, ATR_PERIOD)

    # H4 close at time T is visible the instant the bar opening at T begins.
    h4_open_index = {b.ts: i for i, b in enumerate(h4)}

    trades: list[Trade] = []
    c = Counters()
    setup: Setup | None = None
    open_trade: Trade | None = None
    open_risk = Decimal("0")
    be_done = False
    trades_today: dict[object, int] = {}

    for k in range(1, len(m15)):
        bar = m15[k]          # the bar now forming; entries fill at its open
        closed = m15[k - 1]   # the bar that just closed - "shift 1"
        atr = m15_atr[k - 1]

        # --- a structure bar closed at this instant? -------------------------
        if bar.ts in h4_open_index:
            i = h4_open_index[bar.ts] - 1  # the H4 bar that just closed
            if i >= 0:
                if setup is not None:
                    breached = (
                        h4[i].close < setup.zone_bottom
                        if setup.side == "BUY"
                        else h4[i].close > setup.zone_top
                    )
                    if breached:
                        c.invalidated += 1
                        setup = None
                    elif SETUP_EXPIRY_BARS > 0 and (i - setup.armed_index) > SETUP_EXPIRY_BARS:
                        c.expired += 1
                        setup = None
                fresh = try_arm(h4, i, atr)
                if fresh is not None:
                    c.armed += 1
                    setup = fresh

        # --- manage an open position ----------------------------------------
        if open_trade is not None:
            t = open_trade
            long = t.side == "BUY"
            hit_stop = bar.low <= t.stop if long else bar.high >= t.stop
            hit_tp = bar.high >= t.target if long else bar.low <= t.target
            if hit_stop:
                # A gap through the level fills at the open, not the level.
                px = min(t.stop, bar.open) if long else max(t.stop, bar.open)
                t.exit, t.exit_ts, t.reason = px, bar.ts, "stop"
                trades.append(t)
                open_trade, be_done, open_risk = None, False, Decimal("0")
            elif hit_tp:
                px = max(t.target, bar.open) if long else min(t.target, bar.open)
                t.exit, t.exit_ts, t.reason = px, bar.ts, "target"
                trades.append(t)
                open_trade, be_done, open_risk = None, False, Decimal("0")
            else:
                if BREAK_EVEN_AT_1R and not be_done and open_risk > 0:
                    reached = (
                        bar.close >= t.entry + open_risk
                        if long
                        else bar.close <= t.entry - open_risk
                    )
                    if reached:
                        off = (BREAK_EVEN_OFFSET_ATR * atr) if atr else Decimal("0")
                        t.stop = t.entry + off if long else t.entry - off
                        be_done = True
            continue

        if setup is None:
            continue

        # --- rule 4: tap -----------------------------------------------------
        if setup.tapped_index is None:
            tapped = (
                closed.low <= setup.zone_top
                if setup.side == "BUY"
                else closed.high >= setup.zone_bottom
            )
            if not tapped:
                continue
            setup.tapped_index = k - 1
            c.tapped += 1

        if CONFIRM_WINDOW_BARS > 0 and (k - 1 - setup.tapped_index) > CONFIRM_WINDOW_BARS:
            c.confirm_timeout += 1
            setup = None
            continue

        # --- rule 5: displacement -------------------------------------------
        if atr is None or atr <= 0:
            continue
        body = abs(closed.close - closed.open)
        long = setup.side == "BUY"
        directional = closed.close > closed.open if long else closed.close < closed.open
        impulsive = body >= DISPLACE_ATR_MULT * atr
        escaped = (
            closed.close > setup.zone_top if long else closed.close < setup.zone_bottom
        )
        if not (directional and impulsive and escaped):
            continue
        c.confirmed += 1

        # --- gates -----------------------------------------------------------
        if not session_allows(bar.ts):
            c.blocked_session += 1
            continue
        day = bar.ts.date()
        if MAX_TRADES_PER_DAY > 0 and trades_today.get(day, 0) >= MAX_TRADES_PER_DAY:
            c.blocked_daycap += 1
            continue

        anchor = bar.open
        stop_level = (
            setup.zone_bottom - STOP_BUFFER_ATR_MULT * atr
            if long
            else setup.zone_top + STOP_BUFFER_ATR_MULT * atr
        )
        stop_distance = abs(anchor - stop_level)
        if stop_distance <= 0:
            c.blocked_rr += 1
            setup = None
            continue

        if target_r is not None:
            reach = target_r * stop_distance
            target = anchor + reach if long else anchor - reach
        else:
            target = setup.target
            take_distance = abs(target - anchor)
            ahead = target > anchor if long else target < anchor
            if not ahead or take_distance / stop_distance < MIN_REWARD_RISK:
                c.blocked_rr += 1
                setup = None
                continue

        open_trade = Trade(
            side=setup.side,
            entry_ts=bar.ts,
            entry=anchor,
            stop=stop_level,
            target=target,
            risk=stop_distance,
        )
        open_risk = stop_distance
        be_done = False
        trades_today[day] = trades_today.get(day, 0) + 1
        c.entered += 1
        setup = None

    # A position still open at the window's edge is dropped, not marked to
    # market: an unclosed trade has no realised result to report.
    return [t for t in trades if start <= t.entry_ts <= end], c


def report(label: str, trades: list[Trade], c: Counters) -> None:
    print(f"\n=== {label} ===")
    print(
        f"  setups armed {c.armed} | invalidated {c.invalidated} | expired {c.expired} | "
        f"tapped {c.tapped} | confirm timeout {c.confirm_timeout}"
    )
    print(
        f"  confirmed {c.confirmed} -> blocked: session {c.blocked_session}, "
        f"day cap {c.blocked_daycap}, reward:risk {c.blocked_rr} -> ENTERED {c.entered}"
    )
    if not trades:
        print("  no closed trades")
        return

    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    gross_win = sum((t.net for t in wins), Decimal("0"))
    gross_loss = -sum((t.net for t in losses), Decimal("0"))
    pf = (gross_win / gross_loss) if gross_loss > 0 else Decimal("999")
    net = sum((t.net for t in trades), Decimal("0"))

    equity, peak, max_dd = DEPOSIT, DEPOSIT, Decimal("0")
    for t in trades:
        equity += t.net
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    avg_r = sum((t.r_multiple for t in trades), Decimal("0")) / Decimal(len(trades))
    by_target = len([t for t in trades if t.reason == "target"])
    print(
        f"  trades {len(trades)} | win rate {len(wins) / len(trades) * 100:.1f}% | "
        f"PF {pf:.2f} | net ${net:.2f} | max DD {max_dd:.1f}% | avg {avg_r:+.2f}R"
    )
    print(
        f"  exits: {by_target} target, {len(trades) - by_target} stop/break-even | "
        f"spread paid ${sum((t.spread_cost for t in trades), Decimal('0')):.2f} | "
        f"swap ${sum((t.swap_cost for t in trades), Decimal('0')):.2f}"
    )


def main() -> int:
    if not mt5.initialize():
        print(f"MT5 initialize failed: {mt5.last_error()}", file=sys.stderr)
        return 1
    try:
        if not mt5.symbol_select(SYMBOL, True):
            print(f"cannot select {SYMBOL}", file=sys.stderr)
            return 1
        print(
            f"GoldFairValueGap rules on {SYMBOL}, {LOTS_MT5} MT5 lots ({OUNCES} oz), "
            f"${DEPOSIT} deposit"
        )
        print("Costs: D-121 measured spread $0.29 round trip + Vantage swap. Stop wins ties.")
        for label, start, end in WINDOWS:
            trades, counters = run_window(label, start, end)
            report(label, trades, counters)

        print("\n\n### DIAGNOSTIC: rule 6's liquidity target replaced by a fixed 2R ###")
        print("Same entries, same stops. Only the target moves. If the strategy is")
        print("sound and only the target is out of reach, this is where that shows.")
        for label, start, end in WINDOWS:
            trades, counters = run_window(label, start, end, target_r=Decimal("2"))
            report(f"{label}  [2R target]", trades, counters)
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
