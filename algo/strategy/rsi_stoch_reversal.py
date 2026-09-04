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


BASELINE = ScalperParams()


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
