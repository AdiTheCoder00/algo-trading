"""Shape test: MACD(12,26,9) crossover on real XAUUSD bars, across timeframes.

Not routed through `BacktestEngine`. That engine's per-bar session grouping is
built on `ist_date(bar.ts)` throughout (margin lookups, chain snapshots,
devolvement day-checks) - correct for MCX's one-session-a-day structure, and
the wrong day boundary for a 22:00-21:00 UTC continuous FX/CFD session (D-121).
Generalising the engine to ask a calendar "what session does this bar belong
to" rather than assuming IST is a real change to code that also backs live MCX
trading, and is deliberately not done here without being asked.

So this is a standalone measurement, the same spirit as
`algo/backtest/bhavcopy_runner.py`: real strategy logic
(`algo.strategy.macd_crossover.MacdCrossover`), real data (MT5, not a fixture),
real costs (`algo.costs.cfd`, D-121's measured spread and swap), reported
honestly as a shape test rather than dressed up as a production backtest.

Position sizing is fixed at one MT5 lot (100 engine lots / ounces) per trade -
not risk-scaled, matching the project's own stated position on fixed-lot sizing
(D-089): the implied risk is reported, not hidden, never auto-scaled.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import MetaTrader5 as mt5

from algo.core.bar import Bar, BarWindow, Timeframe
from algo.core.enums import Exchange, Side, SignalAction
from algo.core.instrument import CfdId
from algo.core.position import Position
from algo.costs.cfd import CfdChargeModel, SwapModel
from algo.data.mt5_feed import measure_server_offset
from algo.exchange.forex_calendar import ForexCalendar
from algo.exchange.specs import ContractSpecStore
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext, PositionView, SessionInfo
from algo.strategy.macd_crossover import MacdCrossover
from algo.strategy.price_stop import stop_fill_price
from algo.strategy.trendline_breakout import TrendlineBreakout

XAUUSD = CfdId(symbol="XAUUSD")
LOTS = 100  # one MT5 lot = 100 engine lots (ounces)

#: (label, engine Timeframe, MT5 constant name). The constant is looked up by
#: name at run time rather than imported directly, because `MetaTrader5` is
#: only importable where a terminal is actually installed.
TIMEFRAMES: dict[str, tuple[Timeframe, str]] = {
    "M5": (Timeframe(minutes=5), "TIMEFRAME_M5"),
    "M15": (Timeframe(minutes=15), "TIMEFRAME_M15"),
    "M30": (Timeframe(minutes=30), "TIMEFRAME_M30"),
    "H1": (Timeframe(minutes=60), "TIMEFRAME_H1"),
}

#: Measured 2026-08-28 (D-121). Half of the $0.29 round-trip spread; the
#: charge model is applied per fill, so both legs of a round trip pay it.
HALF_SPREAD = Decimal("0.145")

# Vantage swap terms, measured live (D-121).
SWAP = SwapModel(
    long_points=Decimal("-80.54"),
    short_points=Decimal("32.67"),
    point_value=Decimal("0.01"),  # $ per point per engine lot (one ounce)
)
COMMISSION = CfdChargeModel.vantage_standard()  # verified zero on this account
CALENDAR = ForexCalendar()

#: Single source of truth, passed explicitly into both strategy factories
#: below rather than relied on as their default - so a change here can never
#: drift out of sync with what `stop_fill_price()` is told to use. A
#: stop-triggered close is filled at the stop level itself (or the bar's open
#: on a gap), not `bar.close +/- spread` - see the note in `run()` below and
#: `price_stop.py`'s own docstring for why a close-based fill would be
#: dishonestly optimistic for exactly the signal that exists to bound a loss.
STOP_LOSS_PCT = Decimal("1.0")


@dataclass
class Trade:
    side: Side
    lots: int
    entry_ts: datetime
    entry_price: Decimal
    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    swap_paid: Decimal = Decimal("0")
    spread_paid: Decimal = Decimal("0")
    commission_paid: Decimal = Decimal("0")

    @property
    def gross_pnl(self) -> Decimal:
        if self.exit_price is None:
            return Decimal("0")
        move = self.exit_price - self.entry_price
        signed = move if self.side is Side.BUY else -move
        return signed * self.lots

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.spread_paid - self.swap_paid - self.commission_paid


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    bars_seen: int = 0


def fetch_bars(tf: Timeframe, mt5_constant: str, *, count: int = 50_000) -> list[Bar]:
    """Real closed bars for one timeframe. Connects and disconnects per call -
    simpler than sharing a session across timeframes, and this only runs a
    handful of times."""
    if not mt5.initialize():
        raise SystemExit(f"could not attach to MT5: {mt5.last_error()}")
    if not mt5.symbol_select("XAUUSD", True):
        raise SystemExit(f"could not select XAUUSD: {mt5.last_error()}")
    offset = measure_server_offset(mt5, "XAUUSD")
    # position 1, not 0: the same "exclude the forming bar" rule as
    # `Mt5BarFeed.closed_bars` (algo/data/mt5_feed.py) - position 0 is still
    # being built and its close can still change.
    raw = mt5.copy_rates_from_pos("XAUUSD", getattr(mt5, mt5_constant), 1, count)
    mt5.shutdown()
    if raw is None or len(raw) == 0:
        raise SystemExit(f"MT5 returned no {mt5_constant} bars")
    bars = []
    for row in raw:
        ts = datetime.fromtimestamp(int(row["time"]), UTC) - offset
        bars.append(
            Bar(
                ts=ts,
                timeframe=tf,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["tick_volume"]),
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars


def _session_date(ts: datetime) -> date:
    """Which `ForexCalendar` session (named by its close date) `ts` belongs to.

    Mirrors `ForexCalendar.is_open`'s own search: a session is named by the
    date it closes on, so the candidate is either today or tomorrow.
    """
    for candidate in (ts.date(), ts.date() + timedelta(days=1)):
        try:
            if CALENDAR.session_open(candidate) <= ts < CALENDAR.session_close(candidate):
                return candidate
        except Exception:  # noqa: BLE001 - candidate may not be a trading day
            continue
    return ts.date()


def run(
    bars: list[Bar], tf: Timeframe, strategy_factory: Callable[[], Strategy]
) -> Result:
    """Feed `bars` through one strategy instance, applying real costs to
    whatever it decides.

    `strategy_factory` builds a fresh strategy so this can be called for
    `MacdCrossover` (incremental, never reads `ctx.bars`) and
    `TrendlineBreakout` (stateless, reads `ctx.history(lookback + 1)`) alike -
    the harness does not need to know which.

    The window handed to each `BarContext` is a **bounded sliding window**, not
    the full history `BacktestEngine` builds (`BarWindow.of(tuple(self._bars[:
    index + 1]))`, D-115/D-116's engine). That is O(n) per bar there because a
    real backtest is a few hundred bars; here it is 50,000, and O(n^2) would
    make this script impractically slow for no benefit - no strategy measured
    by this script reads more than `warmup_bars()` bars back.
    """
    strategy = strategy_factory()
    specs = ContractSpecStore.default()
    result = Result()
    # A 23-hour session (D-121) divided into this timeframe's bars. Not read by
    # the strategy - only here for an honest `SessionInfo`, matching what a real
    # engine would compute from `ForexCalendar.bar_boundaries`.
    bars_in_session = max(int(23 * 60 / tf.minutes), 1)
    window_size = max(strategy.warmup_bars() + 5, 10)
    recent: deque[Bar] = deque(maxlen=window_size)

    open_trade: Trade | None = None
    last_session_date: date | None = None

    for index, bar in enumerate(bars):
        recent.append(bar)
        held_position = None
        if open_trade is not None:
            signed_qty = (
                Decimal(open_trade.lots)
                if open_trade.side is Side.BUY
                else -Decimal(open_trade.lots)
            )
            held_position = Position(
                instrument=XAUUSD,
                lots=int(signed_qty),
                qty=signed_qty,
                cost_basis=Decimal(open_trade.lots) * open_trade.entry_price,
            )

        current_session = _session_date(bar.ts)
        # Charge one night's swap each time the bar crosses into a new session
        # date while a position is held - the same instant `ForexCalendar`
        # names as the rollover. `carry_for` returns a signed P&L contribution
        # (negative for a long paying financing); `swap_paid` on `Trade` is a
        # positive cost subtracted in `net_pnl`, so the sign is flipped once,
        # here, and nowhere else.
        if (
            open_trade is not None
            and last_session_date is not None
            and current_session != last_session_date
        ):
            open_trade.swap_paid += -SWAP.carry_for(
                side=open_trade.side, lots=open_trade.lots, on=current_session
            )
        last_session_date = current_session

        ctx = BarContext(
            window=BarWindow.of(tuple(recent)),
            session=SessionInfo(
                session_date=current_session,
                is_us_dst=False,
                minutes_to_close=0,
                is_partial_bar=False,
                bar_index=index,
                bars_in_session=bars_in_session,
            ),
            specs=specs,
            positions=PositionView({} if held_position is None else {XAUUSD.key: held_position}),
            timeframe=tf,
            exchange=Exchange.OTC,
        )

        signals = strategy.on_bar(ctx)
        result.bars_seen += 1

        for signal in signals:
            leg = signal.legs[0]
            is_stop_exit = (
                signal.action is SignalAction.CLOSE
                and signal.reason.startswith("stop loss")
            )
            if is_stop_exit and held_position is not None:
                # A stop-triggered close fills at the stop level (or the bar's
                # open, on a gap) - not `bar.close +/- spread`. The whole point
                # of a stop is to bound the loss at a known level; filling it
                # at wherever the bar happened to close would be exactly the
                # kind of optimism `price_stop.py`'s own docstring argues
                # against, and this is the one place in this script where the
                # distinction has real weight. No additional spread is charged
                # on top - `stop_fill_price` already reflects the worst of the
                # level and the bar's open, which is the honest cost.
                fill_price = stop_fill_price(bar, held_position, STOP_LOSS_PCT)
                extra_spread = Decimal("0")
            else:
                fill_price = bar.close + (
                    HALF_SPREAD if leg.direction is Side.BUY else -HALF_SPREAD
                )
                extra_spread = HALF_SPREAD * LOTS
            commission = COMMISSION.charges_for(
                side=leg.direction, lots=LOTS, price=fill_price,
                multiplier=Decimal("1"), is_option=False, on=current_session,
            ).total

            if signal.action is SignalAction.OPEN:
                open_trade = Trade(
                    side=leg.direction, lots=LOTS, entry_ts=bar.ts, entry_price=fill_price,
                    spread_paid=extra_spread, commission_paid=commission,
                )
            elif signal.action is SignalAction.CLOSE and open_trade is not None:
                open_trade.exit_ts = bar.ts
                open_trade.exit_price = fill_price
                open_trade.spread_paid += extra_spread
                open_trade.commission_paid += commission
                result.trades.append(open_trade)
                open_trade = None

    return result


@dataclass
class Row:
    label: str
    span_years: float
    bars: int
    trades: int
    win_rate: Decimal | None
    gross: Decimal
    spread: Decimal
    swap: Decimal
    net: Decimal
    buyhold: Decimal


#: Strategies this script can measure, each a zero-argument factory so `run`
#: gets a fresh instance per call. `MacdCrossover` carries incremental state
#: and must never be reused across two different bar series.
STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "macd": lambda: MacdCrossover(instrument=XAUUSD, stop_loss_pct=STOP_LOSS_PCT),
    "breakout": lambda: TrendlineBreakout(
        instrument=XAUUSD, lookback=20, stop_loss_pct=STOP_LOSS_PCT
    ),
}


def measure(label: str, bars: list[Bar], strategy_name: str) -> Row:
    tf, _ = TIMEFRAMES[label]
    result = run(bars, tf, STRATEGIES[strategy_name])

    closed = result.trades
    net = sum((t.net_pnl for t in closed), Decimal("0"))
    gross = sum((t.gross_pnl for t in closed), Decimal("0"))
    spread = sum((t.spread_paid for t in closed), Decimal("0"))
    swap = sum((t.swap_paid for t in closed), Decimal("0"))
    wins = [t for t in closed if t.net_pnl > 0]
    span_years = (bars[-1].ts - bars[0].ts).days / 365.25

    return Row(
        label=label,
        span_years=span_years,
        bars=len(bars),
        trades=len(closed),
        win_rate=(
            Decimal(len(wins)) / Decimal(len(closed)) * 100 if closed else None
        ),
        gross=gross,
        spread=spread,
        swap=swap,
        net=net,
        buyhold=(bars[-1].close - bars[0].close) * LOTS,
    )


def _fmt_money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _print_table(rows: list[Row]) -> None:
    header = (
        f"{'tf':<4} {'span':>6} {'bars':>7} {'trades':>7} {'win%':>6} "
        f"{'gross':>14} {'spread':>14} {'swap':>12} {'net':>14} {'buy&hold':>14}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        win_str = f"{row.win_rate:.1f}" if row.win_rate is not None else "n/a"
        print(
            f"{row.label:<4} {row.span_years:>5.2f}y {row.bars:>7,} {row.trades:>7,} "
            f"{win_str:>6} {_fmt_money(row.gross):>14} {_fmt_money(row.spread):>14} "
            f"{_fmt_money(row.swap):>12} {_fmt_money(row.net):>14} "
            f"{_fmt_money(row.buyhold):>14}"
        )


def _common_window(all_bars: dict[str, list[Bar]]) -> tuple[datetime, datetime]:
    """The overlapping [start, end] shared by every fetched series.

    Each timeframe hits MT5's per-request bar cap independently, so a faster
    timeframe's 50,000 bars span far less calendar time than a slower one's -
    comparing them unfiltered would conflate "this timeframe" with "this
    historical period happened to trend". This is the fix: trim every series to
    the window they all actually cover.
    """
    start = max(bars[0].ts for bars in all_bars.values())
    end = min(bars[-1].ts for bars in all_bars.values())
    if start >= end:
        raise SystemExit(f"no overlapping window across {list(all_bars)}: {start} >= {end}")
    return start, end


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    strategy_names = [a for a in args if a in STRATEGIES] or list(STRATEGIES)
    labels = [a for a in args if a in TIMEFRAMES] or list(TIMEFRAMES)
    unknown = [a for a in args if a not in STRATEGIES and a not in TIMEFRAMES]
    if unknown:
        raise SystemExit(
            f"unknown argument(s) {unknown}; timeframes are {list(TIMEFRAMES)}, "
            f"strategies are {list(STRATEGIES)}"
        )

    fetched = {label: fetch_bars(*TIMEFRAMES[label]) for label in labels}

    for strategy_name in strategy_names:
        print(f"\n############ strategy: {strategy_name} ############")
        print(
            "=== full available history per timeframe "
            "(different windows - not a fair comparison) ==="
        )
        _print_table([measure(label, bars, strategy_name) for label, bars in fetched.items()])

        if len(labels) > 1:
            start, end = _common_window(fetched)
            trimmed = {
                label: [b for b in bars if start <= b.ts <= end]
                for label, bars in fetched.items()
            }
            print()
            print(
                f"=== same calendar window, {start:%Y-%m-%d} .. {end:%Y-%m-%d} "
                "(fair comparison) ==="
            )
            _print_table(
                [measure(label, bars, strategy_name) for label, bars in trimmed.items()]
            )
