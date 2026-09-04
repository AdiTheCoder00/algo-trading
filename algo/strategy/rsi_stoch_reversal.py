"""The XAUUSD 5-minute RSI/Stochastic-RSI scalper, as rules and nothing else.

The split here is the one `macd_crossover.py` and `cfd_runner.py` already draw:
this module decides *what the rules say* about a set of indicator values, and
`algo/backtest/xauusd_runner.py` decides what that costs and when it fills. Every
function below is pure - it reads numbers and returns a decision - so each rule
can be tested against a handful of floats instead of a hundred thousand bars.

It deliberately does **not** subclass `Strategy`. That contract hands a strategy
one `BarContext`, which carries exactly one timeframe (`context.py`: "everything
a strategy is allowed to see, and nothing else"), and this strategy needs the H1
trend alongside the M5 signal. The choices were to widen the look-ahead firewall
for one study, or to keep the rules as functions the runner feeds from two
series it has resolved itself. The second leaves the firewall alone, which is
the whole point of it - and `DeltaStrangle`, which needs an option chain the
context cannot supply either, is the precedent for a strategy whose inputs are
assembled outside it.

## The two rules that are easy to get subtly wrong

**The trend uses the last *completed* H1 candle.** The runner resolves that (an
H1 bar is usable by an M5 bar when its close timestamp is at or before the M5
bar's own), but the reason lives here: a trend read off the forming hourly candle
is a trend that knows how the hour ended, which is the look-ahead this whole
project is arranged against.

**The RSI reversal exit is a state, not a level.** "RSI above 75" is not the exit
- crossing back *down* through 75, having been above it, is. A position that
never reaches 75 never becomes eligible, and one that touched 76 an hour ago is
still eligible when it finally slips under. `ExitState.extreme_activated` carries
that per position and is reset with the position, never across trades.

## The one place this departs from the rules as specified

The specified stop is a flat $10. The baseline here is 6 x ATR(14), which is
what $10 was when the rule was written, expressed so that it stays that when
gold's range changes. `SPEC_BASELINE` is the literal specification and is run
and reported alongside every result. The reasoning is on `BASELINE` below.

## What the MACD histogram is not

The histogram is computed and recorded on every trade, and is **not** a
condition. `histogram > 0` is `macd > signal` rearranged; adding it as a third
test would look like confirmation and be arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum

from algo.core.enums import Side


class Trend(Enum):
    """What the last completed H1 candle permits."""

    BULL = "bull"
    BEAR = "bear"
    NONE = "none"


class ExitReason(Enum):
    STOP_LOSS = "stop loss"
    RSI_REVERSAL = "rsi reversal"
    MAX_HOLD = "max hold"
    #: An exit the strategy itself asked for through the runner's `exit_signal`
    #: hook - the 4-EMA strategy's "price closed back through the fast EMA", for
    #: instance. Named for the mechanism rather than for any one rule, because
    #: the runner does not know which rule asked.
    SIGNAL = "signal exit"
    END_OF_DATA = "end of data"


@dataclass(frozen=True, slots=True)
class ScalperParams:
    """Every number the rules use. The defaults are the baseline, exactly.

    Held in one frozen object so a robustness variant is a copy with one field
    changed - `replace(BASELINE, stop_loss=Decimal("7.5"))` - and there is no
    way to run a variant that differs in a second place by accident.
    """

    # --- 1H trend filter
    ema_fast: int = 20
    ema_slow: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # --- 5M signal
    rsi_period: int = 14
    stoch_rsi_period: int = 14
    stoch_smooth_k: int = 3
    stoch_smooth_d: int = 3
    buy_rsi_level: float = 60.0
    sell_rsi_level: float = 40.0
    buy_stoch_level: float = 60.0
    sell_stoch_level: float = 40.0

    # --- exits
    buy_rsi_extreme: float = 75.0
    sell_rsi_extreme: float = 25.0
    stop_loss: Decimal = Decimal("10")
    #: When set, the stop is this many ATRs from entry instead of `stop_loss`
    #: dollars. `None` - the baseline - keeps the fixed dollar distance the
    #: rules specify. This exists because a fixed distance is not a fixed risk:
    #: gold's median five-minute range trebled between 2024 and 2026, so "$10
    #: away" was six median bars in one regime and two in the next. Any run
    #: using it is a variant and is labelled as one.
    stop_atr_multiple: Decimal | None = None
    atr_period: int = 14
    max_hold: timedelta = timedelta(hours=4)
    rsi_reversal_exit: bool = True

    # --- risk and gating
    daily_loss_limit: Decimal = Decimal("-50")
    news_before: timedelta = timedelta(minutes=30)
    news_after: timedelta = timedelta(minutes=15)
    news_filter: bool = True

    # --- direction
    allow_buy: bool = True
    allow_sell: bool = True

    # --- optional entry filters, all off by default
    #
    # These are NOT part of the specified strategy and every one of them is
    # `None` in `BASELINE` and in `SPEC_BASELINE`. They exist so that
    # `study_xauusd_entry.py` can ask whether the entry can be made more
    # selective, and each is stated as a hypothesis before it is measured -
    # see that script's docstring for what each one claims and why.
    #
    #: Require ATR(14) at the signal to be at least this many times the full
    #: spread quoted in that bar. The strategy's own result says costs and edge
    #: are the same size, so the first hypothesis worth testing is "only trade
    #: when the move available is large relative to what it costs to take".
    min_atr_per_spread: Decimal | None = None
    #: Require |EMA20 - EMA50| on the H1 candle to be at least this many H1
    #: ATRs. A trend filter that only checks the SIGN of the gap fires on two
    #: EMAs a cent apart, which is not a trend; this asks for separation.
    min_trend_separation: Decimal | None = None
    #: Require the H1 MACD histogram to be growing in magnitude - momentum
    #: accelerating rather than fading. Adds no constant to fit.
    require_expanding_macd: bool = False
    #: Trade only in these UTC hours. The most dangerous of the filters, and it
    #: is here to be *measured* as dangerous: hour-of-day is exactly the sort of
    #: cut that fits beautifully in sample and means nothing out of it.
    allowed_hours: tuple[int, ...] | None = None
    #: Drop this fraction of otherwise-valid signals, deterministically and for
    #: no reason at all. The control: any filter that does not beat this is
    #: selecting, not filtering.
    drop_fraction: Decimal | None = None

    #: Engine lots. One engine lot is one ounce, so 0.01 MT5 lots (the brief's
    #: size) is 1 here and a $1 move in gold is $1 of P&L. Spelled out because
    #: "0.01 lot" means different things on different platforms, and the stop
    #: being "$10" only pins down a price distance once the size is fixed.
    lots: int = 1

    def label(self) -> str:
        directions = "buy+sell"
        if not self.allow_sell:
            directions = "buy only"
        elif not self.allow_buy:
            directions = "sell only"
        stop = (
            f"{self.stop_atr_multiple}xATR"
            if self.stop_atr_multiple is not None
            else f"${self.stop_loss}"
        )
        return (
            f"SL {stop}, hold {int(self.max_hold.total_seconds() // 3600)}h, "
            f"rsi-exit {'on' if self.rsi_reversal_exit else 'off'}, "
            f"news {'on' if self.news_filter else 'off'}, {directions}"
        )


#: The brief's rules exactly as written, fixed $10 stop and all. Kept as a named
#: object rather than as a comment because it is still run and still reported on
#: every study - promoting the ATR stop must not make the specified strategy
#: unmeasurable, only un-default.
SPEC_BASELINE = ScalperParams()

#: The baseline. The only difference from `SPEC_BASELINE` is that the stop is a
#: multiple of ATR rather than a fixed number of dollars.
#:
#: Six, and the number is not a choice from a sweep. $10 was 6.2 ATRs in the
#: 2024-25 window this was first measured on, so 6xATR is that same stop
#: translated into units that do not change meaning when the market does - which
#: is the entire reason for the change. Picking the multiple by which one earned
#: most would be fitting to 480 trades, and the sweep it would be fitted to
#: disagrees with itself between windows.
#:
#: What the change fixes, in one number: at a fixed $10 the share of trades
#: ending on the stop is 23.9% in the 2024-25 window and 65.1% in the 2026 one,
#: because gold's five-minute range trebled between them. At 6xATR the two are
#: within 6.6 points. The dollar stop was silently a different rule in each
#: regime; this one is not.
#:
#: It does not make the strategy profitable and was not adopted for that. Every
#: ATR row in both windows sits in the same band as every dollar row.
BASELINE = ScalperParams(stop_atr_multiple=Decimal("6"))


@dataclass(frozen=True, slots=True)
class EntryContext:
    """What the optional filters read, beyond what `entry_side` already sees.

    A separate object so `entry_side` keeps its narrow signature - the specified
    rule needs none of this - and so the runner has one obvious place to assemble
    it. Every field is what was true on the **confirmation candle**, never later.
    """

    m5_atr: float
    full_spread: Decimal
    h1_ema_gap: float
    h1_atr: float
    h1_hist: float
    h1_hist_previous: float
    hour: int
    signal_index: int


def passes_filters(context: EntryContext, params: ScalperParams = BASELINE) -> str | None:
    """`None` if the signal survives every filter, else the name of the one that
    rejected it.

    Returns the name rather than a bool so a run can report *which* filter did
    the work - with several of them on at once, "the filters rejected 400
    signals" says nothing about whether one of them is doing everything.
    """
    if params.allowed_hours is not None and context.hour not in params.allowed_hours:
        return "hour"

    if params.min_atr_per_spread is not None:
        if context.m5_atr != context.m5_atr or context.full_spread <= 0:
            return "atr/spread"
        ratio = Decimal(str(context.m5_atr)) / context.full_spread
        if ratio < params.min_atr_per_spread:
            return "atr/spread"

    if params.min_trend_separation is not None:
        if context.h1_atr != context.h1_atr or context.h1_atr <= 0:
            return "trend separation"
        if abs(context.h1_ema_gap) / context.h1_atr < float(params.min_trend_separation):
            return "trend separation"

    if params.require_expanding_macd:
        now, before = context.h1_hist, context.h1_hist_previous
        if now != now or before != before:
            return "macd expanding"
        if abs(now) <= abs(before):
            return "macd expanding"

    if params.drop_fraction is not None:
        # A fixed multiplier rather than `random`: the control has to give the
        # same answer on every run, or comparing against it is meaningless.
        bucket = (context.signal_index * 2654435761) % 1000
        if bucket < int(params.drop_fraction * 1000):
            return "control drop"

    return None


def trend_of(*, ema_fast: float, ema_slow: float, macd_line: float, macd_signal: float) -> Trend:
    """The H1 filter. Both conditions, or no trade - there is no partial trend."""
    if ema_fast > ema_slow and macd_line > macd_signal:
        return Trend.BULL
    if ema_fast < ema_slow and macd_line < macd_signal:
        return Trend.BEAR
    return Trend.NONE


def entry_side(
    *,
    trend: Trend,
    previous_rsi: float,
    rsi: float,
    stoch_k: float,
    stoch_d: float,
    params: ScalperParams = BASELINE,
) -> Side | None:
    """The direction this closed M5 candle calls for, if any.

    A NaN anywhere - the indicator warmup - is not a signal. `nan > 60` is False
    and `nan <= 60` is also False, so a NaN would quietly fail every test rather
    than raising; the explicit check means a warmup bar is skipped for a stated
    reason instead of by accident.
    """
    if _nan(previous_rsi) or _nan(rsi) or _nan(stoch_k) or _nan(stoch_d):
        return None

    if (
        trend is Trend.BULL
        and params.allow_buy
        and previous_rsi <= params.buy_rsi_level
        and rsi > params.buy_rsi_level
        and stoch_k > params.buy_stoch_level
        and stoch_d > params.buy_stoch_level
    ):
        return Side.BUY

    if (
        trend is Trend.BEAR
        and params.allow_sell
        and previous_rsi >= params.sell_rsi_level
        and rsi < params.sell_rsi_level
        and stoch_k < params.sell_stoch_level
        and stoch_d < params.sell_stoch_level
    ):
        return Side.SELL

    return None


@dataclass(slots=True)
class ExitState:
    """The per-position state the RSI reversal exit needs. Reset with the trade."""

    side: Side
    #: Has RSI reached the extreme at any point since this position opened?
    extreme_activated: bool = False

    def observe(self, rsi: float, params: ScalperParams = BASELINE) -> None:
        """Arm the reversal exit once, when the extreme is first reached.

        `>=` rather than `>`: "must first reach/cross above 75" reads as
        touching it, and on a five-minute chart RSI prints exactly 75.0 often
        enough for the distinction to change trades.
        """
        if _nan(rsi):
            return
        beyond = (
            rsi >= params.buy_rsi_extreme
            if self.side is Side.BUY
            else rsi <= params.sell_rsi_extreme
        )
        if beyond:
            self.extreme_activated = True


def rsi_reversal_exit(
    state: ExitState,
    *,
    previous_rsi: float,
    rsi: float,
    params: ScalperParams = BASELINE,
) -> bool:
    """Has RSI crossed back through the extreme, having been beyond it?

    Both halves are required and both are here rather than split across the
    runner: the activation flag, and the crossing on two consecutive closed
    candles. Dropping the flag would turn this into "RSI below 75", which is
    true on most bars of most trades and would close nearly every position on
    its first or second candle.
    """
    if not params.rsi_reversal_exit or not state.extreme_activated:
        return False
    if _nan(previous_rsi) or _nan(rsi):
        return False
    if state.side is Side.BUY:
        return previous_rsi >= params.buy_rsi_extreme and rsi < params.buy_rsi_extreme
    return previous_rsi <= params.sell_rsi_extreme and rsi > params.sell_rsi_extreme


def stop_distance(
    params: ScalperParams = BASELINE, *, atr_at_entry: float | None = None
) -> Decimal:
    """How far from entry the stop sits, in price.

    The fixed rule divides by `lots` because a dollar of loss and a dollar of
    gold are only the same thing at one ounce; the ATR rule does not, because a
    multiple of ATR is a distance already and scaling it by size would make the
    stop move when the position does.
    """
    if params.stop_atr_multiple is None:
        return params.stop_loss / Decimal(params.lots)
    if atr_at_entry is None or atr_at_entry != atr_at_entry or atr_at_entry <= 0:
        # No usable volatility estimate - fall back to the dollar stop rather
        # than to no stop at all. An unbounded position is never the safe
        # default when a measurement is missing.
        return params.stop_loss / Decimal(params.lots)
    return params.stop_atr_multiple * Decimal(str(atr_at_entry))


def stop_level(
    side: Side,
    entry_price: Decimal,
    params: ScalperParams = BASELINE,
    *,
    distance: Decimal | None = None,
) -> Decimal:
    """The executable exit price at which the stop is reached.

    Measured against the **executed** entry price and expressed as an executable
    exit price, which is what a broker's stop order actually is: a long stopped
    out is filled at bid, and the bid reaching this level is the loss being
    realised.
    """
    away = distance if distance is not None else stop_distance(params)
    return entry_price - away if side is Side.BUY else entry_price + away


def _nan(value: float) -> bool:
    return value != value
