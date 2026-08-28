"""MACD(12,26,9) crossover on a single directional instrument — XAUUSD.

Mirrors `tools/macd_telegram_alert` exactly: same EMA form (`adjust=False`), same
default periods, same crossover rule (`<=` then `>`, `>=` then `<`). A signal
here and an alert there are answering the same question the same way.

## The indicator state is incremental, not recomputed per bar

The alert tool has two regimes: `--backtest` walks the *entire* paged history
once and computes MACD from genesis; the live poller refetches a rolling
`candle_limit` window (default 300) and reseeds the EMA from the start of that
window on every single poll, accepting the resulting float-precision drift as
the cost of a bounded, rate-limited API call.

Neither regime fits this engine directly. Recomputing from the full window on
every bar (the live-poller approach) would reseed 50,000 times over one M5
backtest — needless cost, and it also means two live polls of the same instant
could show slightly different histogram values depending on exactly which 300
bars were in the window, which is not a property this engine's live/backtest
parity discipline can accept (the live loop is required to reach the identical
decision path the backtest does).

So this strategy keeps the three EMAs as **running state**, updated by exactly
one new close per `on_bar` call. That is mathematically the alert tool's
`--backtest` mode — an unbroken recursive chain from the first bar this
strategy ever saw — computed incrementally instead of by replaying the whole
window every time. Bar-by-bar, live and backtest walk the identical bars in the
identical order, so the two cannot diverge.

Persisted across a restart (`state`/`restore`), for the same reason
`DeltaStrangle`'s cadence is (D-110): reseeding from zero on every restart would
silently spend `warmup_bars()` bars re-converging with no signal, and a restart
during a real position is exactly when that blind spot is least acceptable.

## Position management: this strategy owns its own exits

`DeltaStrangle` deliberately does *not* manage exits — "exits belong to the risk
layer", because a strangle's exit is a devolvement deadline or a P&L level
unrelated to the entry signal. A crossover strategy is the opposite case: the
entry signal *is* the exit signal for the opposite side. Waiting for a separate
risk-layer stop to close a long after MACD has already turned bearish would mean
holding a position the strategy's own logic no longer believes in. So this
follows `CoinFlip`'s pattern instead: read the held position from the context
(never from what the strategy itself remembers asking for — the same rule,
D-041), close on an opposing cross, open on cost signal when flat.

## A percentage stop-loss, added after the fact and checked first

`tools/macd_telegram_alert` has none, so the first version of this strategy
didn't either — a real, stated gap (D-123): on the measured spread and swap
economics (D-121), a bad run between crossovers was carried to its full extent
with no circuit breaker. `stop_loss_pct` (default 0.5) closes that. It is
checked **before** the warmup gate and before the crossover logic, every bar a
position is held — a held position must never go unprotected because the
indicator that would eventually close it opposingly has not converged yet.

The check uses the bar's actual low/high, not its close (`algo/strategy/
price_stop.py`), for the reason stated there: checking only the close would
understate how often a real broker-side stop fires, and understating a safety
feature is the wrong direction to be optimistic in. No take-profit is added —
that remains this strategy's stated position, unchanged.
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
from algo.pricing.indicators import warmup_bars
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext
from algo.strategy.price_stop import stop_touched


def _ema_step(close: float, previous: float | None, alpha: float) -> float:
    """One recursive EMA step. Seeded with the raw price on the first call."""
    return close if previous is None else alpha * close + (1.0 - alpha) * previous


class MacdCrossover(Strategy):
    """Long on a bullish MACD cross, short on a bearish one, always in or flat."""

    strategy_id = "xauusd_macd_crossover_v1"

    def __init__(
        self,
        *,
        instrument: InstrumentId,
        fast: int = 12,
        slow: int = 26,
        signal_period: int = 9,
        stop_loss_pct: Decimal = Decimal("0.5"),
        config_hash: str = "",
    ) -> None:
        super().__init__()
        if fast >= slow:
            raise DomainError(
                f"the fast period must be shorter than the slow one, got {fast} and {slow}"
            )
        if stop_loss_pct < 0:
            raise DomainError(f"stop_loss_pct cannot be negative, got {stop_loss_pct}")
        self._instrument = instrument
        self._fast = fast
        self._slow = slow
        self._signal_period = signal_period
        self._stop_loss_pct = stop_loss_pct
        self._config_hash = config_hash
        self._fast_ema: float | None = None
        self._slow_ema: float | None = None
        self._signal_ema: float | None = None
        self._prev_histogram: float | None = None
        self._bars_seen = 0

    def warmup_bars(self) -> int:
        return warmup_bars(slow=self._slow, signal=self._signal_period)

    def params(self) -> dict[str, str]:
        return {
            "instrument": self._instrument.key,
            "fast": str(self._fast),
            "slow": str(self._slow),
            "signal": str(self._signal_period),
            "stop_loss_pct": str(self._stop_loss_pct),
        }

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, str]:
        """The running EMAs, so a restart does not silently reseed.

        `repr(float)` round-trips exactly in Python (`float(repr(x)) == x`),
        which is what a persisted indicator value needs — a decimal-string
        truncation here would introduce a discrepancy no contract note could
        ever explain.
        """
        if self._fast_ema is None:
            return {}
        return {
            "fast_ema": repr(self._fast_ema),
            "slow_ema": repr(self._slow_ema),
            "signal_ema": repr(self._signal_ema),
            "prev_histogram": (
                repr(self._prev_histogram) if self._prev_histogram is not None else ""
            ),
            "bars_seen": str(self._bars_seen),
        }

    def restore(self, state: Mapping[str, str]) -> None:
        raw_fast = state.get("fast_ema", "").strip()
        if not raw_fast:
            return
        try:
            self._fast_ema = float(raw_fast)
            self._slow_ema = float(state["slow_ema"])
            self._signal_ema = float(state["signal_ema"])
            raw_hist = state.get("prev_histogram", "").strip()
            self._prev_histogram = float(raw_hist) if raw_hist else None
            self._bars_seen = int(state.get("bars_seen", "0"))
        except (KeyError, ValueError) as exc:
            # A half-restored indicator is worse than a cold one: it would
            # compute a histogram value that looks legitimate but is not
            # continuous with anything the strategy actually saw.
            raise DomainError(
                f"cannot restore MACD state from {dict(state)!r}: {exc}. Refusing "
                "to run with a partially-restored indicator."
            ) from exc

    # ------------------------------------------------------------------ logic
    def _update(self, close: float) -> float:
        """Feed one closed bar's price into the running EMAs. Returns the new
        histogram value. `alpha = 2 / (period + 1)`, the standard EMA weight."""
        fast_alpha = 2.0 / (self._fast + 1.0)
        slow_alpha = 2.0 / (self._slow + 1.0)
        signal_alpha = 2.0 / (self._signal_period + 1.0)

        self._fast_ema = _ema_step(close, self._fast_ema, fast_alpha)
        self._slow_ema = _ema_step(close, self._slow_ema, slow_alpha)
        self._bars_seen += 1

        macd_value = self._fast_ema - self._slow_ema
        self._signal_ema = _ema_step(macd_value, self._signal_ema, signal_alpha)
        return macd_value - self._signal_ema

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        # The EMAs must see every bar regardless of what follows - skipping the
        # update during warmup or while a stop is being checked would leave
        # them permanently behind by however many bars were skipped.
        histogram = self._update(float(ctx.bar.close))

        # The stop is checked before anything else, including warmup - a held
        # position must never go unprotected because the indicator that would
        # have closed it opposingly has not converged yet.
        held = ctx.positions().get(self._instrument)
        if held is not None and not held.is_flat and stop_touched(
            ctx.bar, held, self._stop_loss_pct
        ):
            closing_side = Side.SELL if held.qty > 0 else Side.BUY
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    closing_side,
                    f"stop loss: {self._stop_loss_pct}% move against a "
                    f"{held.side} position of {held.lots} lot(s), entry "
                    f"{held.average_price:.2f}",
                )
            ]

        if self._bars_seen < self.warmup_bars():
            # Still tracked and persisted so the very first post-warmup bar has
            # a real predecessor to compare against, exactly the alert tool's
            # own `warmup_needed` gate.
            self._prev_histogram = histogram
            self.note(
                f"no entry: {self._bars_seen} bar(s) seen, need "
                f"{self.warmup_bars()} before MACD is trusted"
            )
            return []

        previous = self._prev_histogram
        self._prev_histogram = histogram
        if previous is None:
            return []

        crossed_up = previous <= 0.0 < histogram
        crossed_down = previous >= 0.0 > histogram

        if held is not None and not held.is_flat:
            wants_close = (held.qty > 0 and crossed_down) or (held.qty < 0 and crossed_up)
            if not wants_close:
                return []
            closing_side = Side.SELL if held.qty > 0 else Side.BUY
            return [
                self._signal(
                    ctx,
                    SignalAction.CLOSE,
                    closing_side,
                    f"macd crossover: {'bearish' if closing_side is Side.SELL else 'bullish'} "
                    f"cross, flattening a {held.side} position of {held.lots} lot(s) "
                    f"(hist {previous:.4f} -> {histogram:.4f})",
                )
            ]

        if crossed_up:
            return [
                self._signal(
                    ctx, SignalAction.OPEN, Side.BUY,
                    f"macd crossover: bullish (hist {previous:.4f} -> {histogram:.4f})",
                )
            ]
        if crossed_down:
            return [
                self._signal(
                    ctx, SignalAction.OPEN, Side.SELL,
                    f"macd crossover: bearish (hist {previous:.4f} -> {histogram:.4f})",
                )
            ]
        return []

    def _signal(
        self, ctx: BarContext, action: SignalAction, side: Side, reason: str
    ) -> Signal:
        leg_key = f"{self._instrument.key}:{side}"
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
            context={"close": str(ctx.bar.close), "bars_seen": str(self._bars_seen)},
        )
