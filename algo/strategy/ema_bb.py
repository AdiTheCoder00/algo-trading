"""50 EMA trend filter with Bollinger bands - in both of its incompatible forms.

## Why one class with a `mode`, and not two strategies

"The 50 EMA and Bollinger band strategy" names two setups that trade in
**opposite directions on the same bar**, and which one people mean is almost
never stated:

- `pullback` buys the stretch. Price above the 50 EMA, a close back up through
  the lower band after being below it: a dip inside an uptrend, exiting at the
  middle band. This *fades* a band touch.
- `breakout` buys the escape. Price above the 50 EMA, a close outside the upper
  band: volatility expanding in the direction of trend, held while price walks
  the band and exited when it closes back inside. This *follows* a band touch.

Both are widely taught. They cannot both be right about the same bar, and
picking one silently would be choosing the answer before measuring it. They
share every indicator and differ only in the comparison, so they live in one
class where the difference is one branch and the comparison is the point.

## The 50 EMA is carried, not recomputed

An EMA is path-dependent: its value depends on every bar it has ever seen. A
Donchian channel can be rebuilt from a window (`trendline_breakout.py` explains
why it needs no state); this cannot. So the EMA is stepped once per bar and
persisted, exactly as `MacdCrossover` does.

The bands are the opposite case - an SMA and a standard deviation over the last
`bb_period` closes depend on only those closes, so they are recomputed from
`ctx.history(...)` every bar with nothing to seed and nothing to lose.

## `warmup_bars` is not `ema_period`

Seeded with the first close (`indicators.ema`'s convention), a 50-period EMA
still carries about 13% of that seed after 50 bars - `alpha = 2/51 = 0.0392`,
so the residue is `(1 - alpha)^50`. `MacdCrossover` uses `slow + signal + 2`,
which for its `alpha = 0.074` leaves about 5.8%. Matching that residue here
needs about 72 bars, and `ema_period + bb_period + 2` is 72. The shape of the
MACD's rule and the right magnitude for this one, rather than a number picked
to look tidy.

## The EMA is stepped BEFORE the protective-exit check

`mt5/README.md` records a known divergence in `MacdCrossover`: when a
protective exit fires, `on_bar` returns before `_prev_histogram` is assigned,
so the next bar compares against the histogram from two bars ago. It is
preserved in the MT5 port because matching the measured backtest matters more
than tidying it.

This strategy has no such history to match, so it does not reproduce the wart.
The EMA is stepped at the very top of `on_bar`, before the exit check and
before the warmup gate, so a bar on which a stop fires still advances it. An
indicator that skips bars is not the indicator it claims to be.

## `giveback_frac` was added, measured, and switched back off

Every exit this strategy had was a give-up: the flat stop, or the middle band.
The percentage trail existed but shipped at `trail_pct = 0`, so a winner ran to
the middle band and handed back whatever it had made on the way. `giveback_frac`
was added to close that gap - it exits once the position has surrendered that
fraction of the best unrealised profit it ever showed.

**It was measured and it failed (D-154).**
`scripts/measure_ema_bb_giveback_xauusd.py` ran both modes across three
timeframes, three windows and four activation gates, each cell against its own
`giveback_frac = 0` baseline. The trail beat that baseline in **3 of 72 cells**,
and all three are degenerate: two are +$556 and +$806 in cells where it fired
three or four times out of 245 trades, and the third merely loses less
(-$17,309 against -$21,912) in a cell where both readings are heavy losses.
Everywhere else it is worse, often catastrophically - H1 breakout over
2026.06-08 goes from +$37,725 at PF 1.54 to **-$70,636 at PF 0.10**.

The reason is arithmetic, not fit. With `frac = 0.5` a trail armed at `a` first
becomes able to fire at `a/2` of profit, while the flat stop still lets a loser
run to `stop_loss_pct`. At the 0.25% gate that is a 1:4 reward-to-risk floor on
every trade the trail touches, and no entry rule survives it. The results are
monotone in the gate for exactly that reason: the wider it is set, the closer to
baseline it lands, because it fires less. **Its best measured behaviour is not
firing at all**, which is the clearest possible statement that it should not.

**It is nevertheless on by default, at 0.5, and that was a deliberate call made
after the above was reported** - with `trail_activation_pct` raised from 0.25 to
2 in the same change. That pairing matters: 0.25 is the gate that measured as
destructive, 2 is the gate at which the trail is close to inert, firing three
times in fifty-three trades in the sharpest cell and finishing $556 from
baseline. The measurement above is not withdrawn; the default simply no longer
follows it, and this paragraph exists so the next reader sees both facts
together rather than inferring that D-154 came out the other way.

The parameter is kept regardless of its default, for the reason `long_only`
above it is kept: whoever next notices that this strategy hands its winners back
should find the falsification attached to the fix.

A give-back trail is only coherent when it arms well ABOVE the stop distance.
That is the shape any future attempt has to start from, and this data says even
then the middle band was already the better exit.

## Not registered in `strategy_for`, and now for a measured reason

Deliberately. D-151 is the entry recording an expert built from published rules
and measured afterwards, at which point it had no edge. Registering a strategy
makes it reachable by the live loop and the dashboard; that should follow a
measurement, not precede one.

The measurements exist now, and they say do not register it.
`scripts/measure_ema_bb_xauusd.py` (D-152) found `pullback` below break-even in
every window and `breakout` positive but losing to simply holding gold.
`scripts/walkforward_ema_bb_xauusd.py` (D-153) pre-registered the one promising
slice - long-only - and rejected it on seven years of unseen data.

Nothing here is tradable. It is kept because the code is the record of what was
tested, and because `run_cfd_walk_forward`'s `factory` hook, added for D-153,
lets the next candidate be tested the same way without being made live first.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from algo.core.enums import Side, SignalAction
from algo.core.errors import DomainError
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.pricing.indicators import bollinger
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext
from algo.strategy.protective_exits import ExitKind, ProtectiveExits

#: `long_only` exists because D-152 measured this strategy's shorts as negative
#: in eight of nine cells while its longs were positive in all nine, and
#: long-only would have beaten buy-and-hold on those windows.
#:
#: **It was tested and it failed.** D-153 pre-registered that hypothesis and ran
#: it on seven years of data D-152 never saw: profit factor 1.04 on H1 and 0.99
#: on M30, 48% and 43% of folds positive, against buy-and-hold's +$195,893 at a
#: third of the drawdown. The flag is kept, rather than deleted, so that the
#: next person to notice that the shorts lose finds the falsification attached
#: to it instead of rediscovering the same slice.

#: The two readings. Strings rather than an enum because they are also a CLI
#: and config value, and every other knob in this package is a string.
PULLBACK = "pullback"
BREAKOUT = "breakout"
MODES = (PULLBACK, BREAKOUT)


def _ema_step(close: float, previous: float | None, alpha: float) -> float:
    """One recursive EMA step. Seeded with the first close, as `ema()` is."""
    return close if previous is None else alpha * close + (1.0 - alpha) * previous


class EmaBollinger(Strategy):
    """50 EMA trend filter plus Bollinger bands, faded or followed by `mode`."""

    strategy_id = "xauusd_ema_bb_v1"

    def __init__(
        self,
        *,
        instrument: InstrumentId,
        mode: str = PULLBACK,
        long_only: bool = False,
        ema_period: int = 50,
        bb_period: int = 20,
        bb_stdev: float = 2.0,
        stop_loss_pct: Decimal = Decimal("0.5"),
        trail_activation_pct: Decimal = Decimal("2"),
        trail_pct: Decimal = Decimal("0"),
        giveback_frac: Decimal = Decimal("0.5"),
        config_hash: str = "",
    ) -> None:
        super().__init__()
        if mode not in MODES:
            raise DomainError(f"mode must be one of {MODES}, got {mode!r}")
        if ema_period < 2:
            raise DomainError(f"ema_period must be at least 2, got {ema_period}")
        if bb_period < 2:
            raise DomainError(f"bb_period must be at least 2, got {bb_period}")
        if bb_stdev <= 0:
            raise DomainError(f"bb_stdev must be positive, got {bb_stdev}")

        self._instrument = instrument
        self._mode = mode
        self._long_only = long_only
        self._ema_period = ema_period
        self._bb_period = bb_period
        self._bb_stdev = bb_stdev
        self._config_hash = config_hash
        self._exits = ProtectiveExits(
            stop_loss_pct=stop_loss_pct,
            trail_activation_pct=trail_activation_pct,
            trail_pct=trail_pct,
            giveback_frac=giveback_frac,
        )
        self._ema: float | None = None
        self._bars_seen = 0

    def warmup_bars(self) -> int:
        """See the module docstring - this is a residue argument, not `ema_period`."""
        return self._ema_period + self._bb_period + 2

    def params(self) -> dict[str, str]:
        return {
            "instrument": self._instrument.key,
            "mode": self._mode,
            "long_only": str(self._long_only),
            "ema_period": str(self._ema_period),
            "bb_period": str(self._bb_period),
            "bb_stdev": str(self._bb_stdev),
            **self._exits.params(),
        }

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, str]:
        """The EMA and the bar count, plus whatever the exits carry.

        `repr()` on the float, not `str()`: the EMA is resumed from this and a
        rounded value would restart the recursion from a different number than
        the one it stopped at.
        """
        carried = dict(self._exits.state())
        if self._ema is not None:
            carried["ema"] = repr(self._ema)
            carried["bars_seen"] = str(self._bars_seen)
        return carried

    def restore(self, state: Mapping[str, str]) -> None:
        self._exits.restore(state)
        raw = state.get("ema", "").strip()
        if raw:
            self._ema = float(raw)
            self._bars_seen = int(state.get("bars_seen", "0"))

    # ------------------------------------------------------------------ logic
    def on_bar(self, ctx: BarContext) -> list[Signal]:
        # Stepped first, unconditionally - see the module docstring. A bar on
        # which a stop fires is still a bar the EMA saw.
        close = float(ctx.bar.close)
        alpha = 2.0 / (self._ema_period + 1.0)
        self._ema = _ema_step(close, self._ema, alpha)
        self._bars_seen += 1

        held = ctx.positions().get(self._instrument)
        exit_decision = self._exits.check(ctx.bar, held)
        if exit_decision is not None:
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    exit_decision.side,
                    exit_decision.reason,
                    exit_kind=exit_decision.kind,
                )
            ]

        if self._bars_seen < self.warmup_bars() or not ctx.has_history(self._bb_period + 1):
            self.note(
                f"no entry: {self._bars_seen} bar(s) seen, need {self.warmup_bars()} "
                f"for a {self._ema_period}-period EMA to have shed its seed"
            )
            return []

        window = ctx.history(self._bb_period + 1)
        closes = [float(c) for c in window.closes()]
        bands = bollinger(closes, period=self._bb_period, num_stdev=self._bb_stdev)
        middle, upper, lower = bands.middle[-1], bands.upper[-1], bands.lower[-1]
        prev_close = closes[-2]
        prev_lower, prev_upper = bands.lower[-2], bands.upper[-2]
        if middle is None or upper is None or lower is None:
            return []

        ema = self._ema
        above_trend = close > ema
        below_trend = close < ema

        if held is not None and not held.is_flat:
            # Both modes exit on the middle band, from opposite sides: the
            # pullback has reached its target there, the breakout has lost the
            # expansion that justified it. One comparison, two meanings.
            long = held.qty > 0
            if self._mode == PULLBACK:
                reached = close >= middle if long else close <= middle
                if reached:
                    return [self._close(ctx, held, close, middle, "reached the middle band")]
                return []
            lost = close < middle if long else close > middle
            if lost:
                return [self._close(ctx, held, close, middle, "closed back inside the band")]
            return []

        if self._mode == PULLBACK:
            # A close back UP through the lower band, having been below it,
            # with the trend filter agreeing. The re-entry is what makes it a
            # rejection rather than a knife: price below the band is not a
            # signal, price leaving it is.
            if prev_lower is not None and above_trend and prev_close < prev_lower <= close:
                return [
                    self._signal(
                        ctx, SignalAction.OPEN, Side.BUY,
                        f"EMA/BB pullback: close {close:.2f} back above the lower band "
                        f"{lower:.2f} (previous {prev_close:.2f}), with price above the "
                        f"{self._ema_period} EMA {ema:.2f}",
                    )
                ]
            if (
                not self._long_only
                and prev_upper is not None
                and below_trend
                and prev_close > prev_upper >= close
            ):
                return [
                    self._signal(
                        ctx, SignalAction.OPEN, Side.SELL,
                        f"EMA/BB pullback: close {close:.2f} back below the upper band "
                        f"{upper:.2f} (previous {prev_close:.2f}), with price below the "
                        f"{self._ema_period} EMA {ema:.2f}",
                    )
                ]
            return []

        # BREAKOUT: a close OUTSIDE the band, trend agreeing.
        if above_trend and close > upper:
            return [
                self._signal(
                    ctx, SignalAction.OPEN, Side.BUY,
                    f"EMA/BB breakout: close {close:.2f} above the upper band {upper:.2f}, "
                    f"with price above the {self._ema_period} EMA {ema:.2f}",
                )
            ]
        if not self._long_only and below_trend and close < lower:
            return [
                self._signal(
                    ctx, SignalAction.OPEN, Side.SELL,
                    f"EMA/BB breakout: close {close:.2f} below the lower band {lower:.2f}, "
                    f"with price below the {self._ema_period} EMA {ema:.2f}",
                )
            ]
        return []

    # ----------------------------------------------------------------- helpers
    def _close(
        self, ctx: BarContext, held: object, close: float, middle: float, why: str
    ) -> Signal:
        closing_side = Side.SELL if held.qty > 0 else Side.BUY  # type: ignore[attr-defined]
        return self._signal(
            ctx,
            SignalAction.CLOSE,
            closing_side,
            f"EMA/BB {self._mode}: {why} - close {close:.2f} vs middle {middle:.2f}, "
            f"flattening a {held.side} position of {held.lots} lot(s)",  # type: ignore[attr-defined]
        )

    def _signal(
        self,
        ctx: BarContext,
        action: SignalAction,
        side: Side,
        reason: str,
        *,
        exit_kind: ExitKind | None = None,
    ) -> Signal:
        leg_key = f"{self._instrument.key}:{side}"
        context = {"close": str(ctx.bar.close), "mode": self._mode}
        if self._ema is not None:
            context["ema"] = f"{self._ema:.4f}"
        if exit_kind is not None:
            context["exit"] = exit_kind
        return Signal(
            signal_id=signal_id(
                strategy_id=self.strategy_id,
                params_hash=self.params_hash(),
                bar_close_iso=iso(ctx.now),
                action=action.value,
                leg_keys=(leg_key,),
                config_hash=self._config_hash,
            ),
            strategy_id=self.strategy_id,
            ts=ctx.now,
            action=action,
            legs=(
                SignalLeg(
                    instrument=self._instrument, direction=side, entry=PriceIntent.market()
                ),
            ),
            reason=reason,
            context=context,
        )
