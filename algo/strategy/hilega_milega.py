"""Hilega-Milega — RSI(9) with a 3-EMA and a 21-WMA plotted on the RSI itself.

A widely-taught Indian retail setup, and one this project can implement without
inventing anything, because the source states every component precisely: take
RSI, shorten it from 14 to 9, move **both** its bands to 50, then plot a
3-period EMA and a 21-period WMA *on the RSI line* rather than on price. Three
lines, one panel. `algo/pricing/indicators.py` computes them to Pine's own
definitions for the reason that module's docstring gives — a rule here and a
chart on TradingView must not disagree about what the lines are.

## What the three lines are claimed to mean

The pitch is that four things move a market — price, strength, momentum and
volume — and that reading four separate indicators for them produces a chart
where one says buy while another says sell. So all four are folded into one
panel:

- **RSI(9), the black line** — strength. Above 50 is the half of the panel
  where longs are allowed; below 50, they are not.
- **EMA(3) of the RSI, the green line** — the RSI's own short-term direction.
  It hugs the RSI, so it is the first line crossed when a move stalls.
- **WMA(21) of the RSI, the red line** — the slow line, and the one the source
  calls the most important. "Weighted" is where the volume claim comes from.

**That volume claim is false, and this module does not rest on it.** A weighted
moving average weights by *recency*, not by traded volume; `wma`'s own
docstring states the weights. Nothing about a WMA of an RSI reads the volume
column, so a rule justified as "volume confirmation" is justified by a
misreading of the arithmetic. What survives the correction is still a perfectly
well-defined object — a slow, recency-weighted average of RSI — and the rules
below are written against *that*, which is what the indicator computes either
way. The claim is recorded rather than quietly dropped, because it is the
stated reason the line is in the panel at all.

## The rules, as implemented

Long alignment is all three of: RSI above 50, RSI above its EMA, RSI above its
WMA — the source's "red line inside the strength", which is the WMA sitting
below the RSI on the long half of the panel. Short alignment is the mirror.
Entry is on the bar alignment first holds while flat.

The exit is deliberately looser than the entry, which is what the source
describes when it calls the WMA crossing back through the RSI a trailing stop
rather than a reversal: a long closes when the RSI falls back to or below its
WMA, without waiting for full bearish alignment. Requiring the entry
condition's mirror in order to close would hold a position through the whole of
the move the setup's own author treats as the exit.

At most one signal per bar, `MacdCrossover`'s convention: a close and a
re-entry on the opposite side never share a bar. `ProtectiveExits` relies on
that (see its `check`), and a same-bar reversal would also mean acting twice on
one close.

## `min_separation`, the momentum claim made checkable

"As long as there is distance between the three lines, momentum continues" is
the source's other main claim, and the one part of the method that is otherwise
pure eyeballing. `min_separation` (default 0, meaning off) turns it into a
number: the RSI must clear the nearer of the two averages by at least this many
RSI points before an entry is taken. Zero keeps the plain rule, so turning it
on is an explicit choice by whoever is measuring rather than a default nobody
selected.

## Incremental state, persisted, for `MacdCrossover`'s reasons

Wilder's average is recursive and the EMA over the RSI is recursive, so both
depend on every bar this strategy has ever seen; the WMA needs the last 21 RSI
readings. All of it is carried as running state, updated by exactly one close
per `on_bar`, and persisted across a restart — reseeding from zero would spend
`warmup_bars()` bars blind, and a restart mid-position is when that blind spot
is least acceptable (D-110, D-123). The update reproduces
`indicators.hilega_milega` over the same bars exactly, and
`tests/test_hilega_milega.py` asserts that against the vectorised form rather
than trusting two implementations to stay in step on their own.

## The protective exits are the shared ones

`stop_loss_pct` (default 0.5) and the optional trail come from
`algo/strategy/protective_exits.py`, checked before the warmup gate and before
any of the rules above, for the reason stated there and in `MacdCrossover`: a
held position must never go unprotected because an indicator has not converged
yet. That matters more here than for MACD — nothing in the published method is
a stop at all, only the WMA cross, and an RSI can sit on the wrong side of its
own average for a long time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal

from algo.core.enums import Side, SignalAction
from algo.core.errors import DomainError
from algo.core.ids import signal_id
from algo.core.instrument import InstrumentId
from algo.core.position import Position
from algo.core.signal import PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso
from algo.pricing.indicators import hilega_milega_warmup_bars, rsi_from_averages
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext
from algo.strategy.protective_exits import ExitKind, ProtectiveExits

MIDLINE = 50.0
"""The line both RSI bands are moved to.

Not a parameter. Moving it would stop this being the published setup, and "RSI
above its own midpoint" is the one threshold in the method that carries an
argument rather than a fitted value.
"""


def _ema_step(value: float, previous: float | None, alpha: float) -> float:
    """One recursive EMA step, seeded with the first value. `MacdCrossover`'s."""
    return value if previous is None else alpha * value + (1.0 - alpha) * previous


def _floats(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part]


class HilegaMilega(Strategy):
    """Long when the RSI leads both its averages above 50, short when below."""

    strategy_id = "xauusd_hilega_milega_v1"

    def __init__(
        self,
        *,
        instrument: InstrumentId,
        rsi_period: int = 9,
        trend_period: int = 3,
        weighted_period: int = 21,
        min_separation: Decimal = Decimal("0"),
        stop_loss_pct: Decimal = Decimal("0.5"),
        trail_activation_pct: Decimal = Decimal("2"),
        trail_pct: Decimal = Decimal("0"),
        config_hash: str = "",
    ) -> None:
        super().__init__()
        if rsi_period < 2:
            raise DomainError(f"the RSI period must be at least 2, got {rsi_period}")
        if trend_period < 1:
            raise DomainError(f"the trend EMA period must be at least 1, got {trend_period}")
        if weighted_period < 2:
            raise DomainError(f"the weighted MA period must be at least 2, got {weighted_period}")
        if trend_period >= weighted_period:
            # The whole read is "fast line hugs the RSI, slow line lags it". Two
            # averages of the same length, or a fast one that is longer, would
            # still compute — and would silently be a different indicator.
            raise DomainError(
                "the trend EMA must be shorter than the weighted MA, got "
                f"{trend_period} and {weighted_period}"
            )
        if min_separation < 0:
            raise DomainError(f"min_separation cannot be negative, got {min_separation}")
        self._instrument = instrument
        self._rsi_period = rsi_period
        self._trend_period = trend_period
        self._weighted_period = weighted_period
        self._min_separation = min_separation
        self._config_hash = config_hash
        self._exits = ProtectiveExits(
            stop_loss_pct=stop_loss_pct,
            trail_activation_pct=trail_activation_pct,
            trail_pct=trail_pct,
        )
        self._prev_close: float | None = None
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []
        self._average_gain: float | None = None
        self._average_loss: float | None = None
        self._trend_ema: float | None = None
        self._rsi_window: list[float] = []
        self._bars_seen = 0

    def warmup_bars(self) -> int:
        return hilega_milega_warmup_bars(
            rsi_period=self._rsi_period,
            trend_period=self._trend_period,
            weighted_period=self._weighted_period,
        )

    def params(self) -> dict[str, str]:
        return {
            "instrument": self._instrument.key,
            "rsi_period": str(self._rsi_period),
            "trend_period": str(self._trend_period),
            "weighted_period": str(self._weighted_period),
            "min_separation": str(self._min_separation),
            **self._exits.params(),
        }

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, str]:
        """Every recursive term, plus the WMA's window. `repr` for the floats.

        `repr(float)` round-trips exactly in Python, which is what a persisted
        indicator value needs — a decimal-string truncation here would put the
        restarted process on a slightly different RSI from the one that opened
        the position it is now managing.

        The seed buffers are persisted too. They are non-empty only during the
        first `rsi_period` bars, but dropping them would silently restart
        Wilder's seed from whatever bars arrived after the restart, producing an
        RSI that is continuous with nothing this strategy ever saw.
        """
        if self._prev_close is None:
            return {}
        return {
            "prev_close": repr(self._prev_close),
            "seed_gains": ",".join(repr(v) for v in self._seed_gains),
            "seed_losses": ",".join(repr(v) for v in self._seed_losses),
            "average_gain": (repr(self._average_gain) if self._average_gain is not None else ""),
            "average_loss": (repr(self._average_loss) if self._average_loss is not None else ""),
            "trend_ema": repr(self._trend_ema) if self._trend_ema is not None else "",
            "rsi_window": ",".join(repr(v) for v in self._rsi_window),
            "bars_seen": str(self._bars_seen),
            **self._exits.state(),
        }

    def restore(self, state: Mapping[str, str]) -> None:
        raw_close = state.get("prev_close", "").strip()
        if not raw_close:
            return
        try:
            self._prev_close = float(raw_close)
            self._seed_gains = _floats(state.get("seed_gains", ""))
            self._seed_losses = _floats(state.get("seed_losses", ""))
            raw_gain = state.get("average_gain", "").strip()
            raw_loss = state.get("average_loss", "").strip()
            self._average_gain = float(raw_gain) if raw_gain else None
            self._average_loss = float(raw_loss) if raw_loss else None
            raw_trend = state.get("trend_ema", "").strip()
            self._trend_ema = float(raw_trend) if raw_trend else None
            self._rsi_window = _floats(state.get("rsi_window", ""))
            self._bars_seen = int(state["bars_seen"])
        except (KeyError, ValueError) as exc:
            # `MacdCrossover`'s refusal, for its reason: a half-restored
            # indicator is worse than a cold one, because it produces readings
            # that look legitimate and are continuous with nothing.
            raise DomainError(
                f"cannot restore Hilega-Milega state from {dict(state)!r}: {exc}. "
                "Refusing to run with a partially-restored indicator."
            ) from exc
        self._check_restored(state)
        # After the indicator, and only once it restored cleanly — a trail
        # without its lines would arm against a position the strategy could not
        # yet reason about. Raises on its own terms, naming the trail.
        self._exits.restore(state)

    def _check_restored(self, state: Mapping[str, str]) -> None:
        """Reject a state that parsed but cannot describe a real indicator.

        Both of these are shapes `float()` accepts happily and that would then
        run: one Wilder average seeded without the other divides a settled
        numerator by an unsettled denominator, and an over-long window would
        make the WMA average more bars than its own period.
        """
        if (self._average_gain is None) != (self._average_loss is None):
            raise DomainError(
                f"cannot restore Hilega-Milega state from {dict(state)!r}: Wilder's "
                "gain and loss averages must both be seeded or both unseeded. "
                "Refusing to run with a partially-restored indicator."
            )
        if len(self._rsi_window) > self._weighted_period:
            raise DomainError(
                f"cannot restore Hilega-Milega state from {dict(state)!r}: the saved "
                f"window holds {len(self._rsi_window)} readings but the weighted MA "
                f"is {self._weighted_period} bars. Refusing to run with a "
                "partially-restored indicator."
            )

    # ------------------------------------------------------------------ logic
    def _update(self, close: float) -> tuple[float, float, float] | None:
        """Feed one closed bar in. Returns the three lines, or `None` while any
        of them is still undefined.

        `None` rather than `nan` at this boundary: the vectorised form must
        return one value per bar and so has nowhere else to put "undefined",
        but a caller holding a tuple has somewhere better, and `None` cannot be
        compared against 50 by accident.
        """
        self._bars_seen += 1
        previous_close = self._prev_close
        self._prev_close = close
        if previous_close is None:
            return None

        gain = max(close - previous_close, 0.0)
        loss = max(previous_close - close, 0.0)

        if self._average_gain is None or self._average_loss is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < self._rsi_period:
                return None
            # Wilder's SMA seed, exactly `rsi`'s: the mean of the first
            # `rsi_period` changes, which lands on bar `rsi_period`.
            self._average_gain = sum(self._seed_gains) / self._rsi_period
            self._average_loss = sum(self._seed_losses) / self._rsi_period
            self._seed_gains.clear()
            self._seed_losses.clear()
        else:
            alpha = 1.0 / self._rsi_period
            self._average_gain = alpha * gain + (1.0 - alpha) * self._average_gain
            self._average_loss = alpha * loss + (1.0 - alpha) * self._average_loss

        strength = rsi_from_averages(self._average_gain, self._average_loss)
        self._trend_ema = _ema_step(strength, self._trend_ema, 2.0 / (self._trend_period + 1.0))

        self._rsi_window.append(strength)
        if len(self._rsi_window) > self._weighted_period:
            self._rsi_window.pop(0)
        if len(self._rsi_window) < self._weighted_period:
            return None

        denominator = self._weighted_period * (self._weighted_period + 1) / 2.0
        weighted = sum(value * (i + 1) for i, value in enumerate(self._rsi_window)) / denominator
        return strength, self._trend_ema, weighted

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        # The indicator sees every bar regardless of what follows — skipping the
        # update during warmup, or while a stop is being checked, would leave
        # all three lines permanently behind by however many bars were skipped.
        lines = self._update(float(ctx.bar.close))

        # Before the warmup gate and before the rules, for the reason
        # `ProtectiveExits.check` states.
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

        if lines is None or self._bars_seen < self.warmup_bars():
            self.note(
                f"no entry: {self._bars_seen} bar(s) seen, need "
                f"{self.warmup_bars()} before the three lines are trusted"
            )
            return []

        strength, trend, weighted = lines
        if math.isnan(strength) or math.isnan(trend) or math.isnan(weighted):
            # Belt and braces. `_update` returns `None` rather than a `nan`
            # tuple, so this is unreachable today; it is here because every
            # comparison below reads `False` on a `nan`, which would look like a
            # deliberate "no signal" rather than a broken indicator.
            self.note("no entry: the indicator produced an undefined reading")
            return []

        if held is not None and not held.is_flat:
            return self._manage(ctx, held, strength, weighted)

        return self._enter(ctx, strength, trend, weighted)

    def _manage(
        self, ctx: BarContext, held: Position, strength: float, weighted: float
    ) -> list[Signal]:
        """The WMA crossing back through the RSI — the setup's published exit."""
        is_long = held.qty > 0
        crossed_back = strength <= weighted if is_long else strength >= weighted
        if not crossed_back:
            return []
        return [
            self._signal(
                ctx,
                SignalAction.CLOSE,
                Side.SELL if is_long else Side.BUY,
                f"hilega-milega: rsi {strength:.2f} back "
                f"{'below' if is_long else 'above'} its {self._weighted_period}-bar "
                f"weighted average {weighted:.2f}, flattening a {held.side} position "
                f"of {held.lots} lot(s)",
            )
        ]

    def _enter(
        self, ctx: BarContext, strength: float, trend: float, weighted: float
    ) -> list[Signal]:
        separation = float(self._min_separation)
        if (
            strength > MIDLINE
            and strength > trend
            and strength > weighted
            and strength - max(trend, weighted) >= separation
        ):
            return [
                self._signal(
                    ctx,
                    SignalAction.OPEN,
                    Side.BUY,
                    f"hilega-milega: rsi {strength:.2f} above {MIDLINE:.0f} and "
                    f"leading both averages (ema {trend:.2f}, wma {weighted:.2f})",
                )
            ]
        if (
            strength < MIDLINE
            and strength < trend
            and strength < weighted
            and min(trend, weighted) - strength >= separation
        ):
            return [
                self._signal(
                    ctx,
                    SignalAction.OPEN,
                    Side.SELL,
                    f"hilega-milega: rsi {strength:.2f} below {MIDLINE:.0f} and "
                    f"trailing both averages (ema {trend:.2f}, wma {weighted:.2f})",
                )
            ]
        return []

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
        context = {"close": str(ctx.bar.close), "bars_seen": str(self._bars_seen)}
        if exit_kind is not None:
            # Structured, so a consumer never has to parse `reason` to learn
            # which exit fired — see `ExitKind`'s own docstring.
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
                    instrument=self._instrument,
                    direction=side,
                    entry=PriceIntent.market(),
                ),
            ),
            reason=reason,
            context=context,
        )
