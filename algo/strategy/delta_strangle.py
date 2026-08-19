"""The strategy: sell a ~0.25-delta call and a ~0.25-delta put, once per cycle.

What it does, in order, on every closed bar:

1. If a position is open, do nothing. Exits belong to the risk layer (brief §8),
   and a strategy that also managed its own exits would give two components an
   opinion about the same position.
2. If this is not one of the nominated entry bars (09:30 IST), do nothing.
3. Resolve the expiry cycle and check the DTE band.
4. Read the chain as of this bar's close, pick the tradeable call and put nearest
   0.25 delta, and emit **one** signal carrying both legs.

Three things it deliberately does not do:

**It does not size.** No lots, no rupees. The risk layer owns that.

**It does not invent a strike.** If no strike within tolerance has a two-sided
quote, it emits nothing and records why. That case is not hypothetical — on the
live 28 Aug chain the 0.25-delta strikes sat at roughly 160,500 and 153,000, past
every strike on screen, and two of the strikes that *were* on screen showed no put
quote at all. A strategy that quietly fell back to the nearest quoted strike would
be reporting fills at 0.34 delta while claiming to trade 0.25.

**It does not remember what it asked for.** Position state comes from the context
every time (D-041), and the "one strangle per cycle" cadence is recorded from
`on_fill` — from what actually traded — rather than from what was emitted. A
signal can be refused by the risk layer, and a strategy that counted its own
intentions would then skip the cycle it never actually traded.

That cadence set is genuine strategy state and must be persisted for a live
restart to behave correctly. Recorded as an open item for Milestone 6.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Atomicity, Right, Side, SignalAction
from algo.core.fill import Fill
from algo.core.ids import signal_id
from algo.core.instrument import OptionId
from algo.core.signal import ComboExit, PriceIntent, Signal, SignalLeg
from algo.core.timeutil import iso, to_ist
from algo.strategy.base import Strategy
from algo.strategy.context import BarContext


class DeltaStrangle(Strategy):
    """Short strangle at a target delta, one cycle at a time."""

    strategy_id = "goldm_delta_strangle_v1"

    def __init__(
        self,
        *,
        underlying: str,
        target_delta: Decimal = Decimal("0.25"),
        delta_tolerance: Decimal = Decimal("0.05"),
        entry_times_ist: tuple[time, ...] = (time(9, 30),),
        min_dte: int = 5,
        max_dte: int = 45,
        take_profit: ComboExit | None = None,
        stop_loss: ComboExit | None = None,
        config_hash: str = "",
    ) -> None:
        super().__init__()
        self._underlying = underlying
        self._target = target_delta
        self._tolerance = delta_tolerance
        self._entry_times = entry_times_ist
        self._min_dte = min_dte
        self._max_dte = max_dte
        self._take_profit = take_profit or ComboExit(
            kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("2")
        )
        self._stop_loss = stop_loss or ComboExit(
            kind="PCT_OF_MARGIN_AT_ENTRY", value=Decimal("1")
        )
        self._config_hash = config_hash
        self._traded_cycles: set[date] = set()

    def warmup_bars(self) -> int:
        return 0

    def on_fill(self, fill: Fill) -> None:
        """Record that this expiry cycle has been traded.

        Driven by fills rather than by emitted signals: the risk layer can refuse
        an entry, and a cadence counter that ticked on intent would skip a cycle
        the account never actually entered.
        """
        option = fill.instrument
        if isinstance(option, OptionId) and fill.side is Side.SELL:
            self._traded_cycles.add(option.option_expiry)

    def params(self) -> dict[str, str]:
        return {
            "underlying": self._underlying,
            "target_delta": str(self._target),
            "delta_tolerance": str(self._tolerance),
            "entry_times_ist": ",".join(t.isoformat() for t in self._entry_times),
            "min_dte": str(self._min_dte),
            "max_dte": str(self._max_dte),
            "take_profit": f"{self._take_profit.kind}:{self._take_profit.value}",
            "stop_loss": f"{self._stop_loss.kind}:{self._stop_loss.value}",
        }

    # ------------------------------------------------------------------ logic
    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if not ctx.positions().is_flat:
            return []

        if ctx.session.is_partial_bar:
            # The 23:30-23:55 stub outside US daylight saving. The risk layer may
            # act on it; the strategy may not (D-014).
            return []

        local = to_ist(ctx.now).time()
        if local not in self._entry_times:
            return []

        cycle = ctx.nearest_expiry(self._underlying)
        if cycle.option_expiry in self._traded_cycles:
            # Cadence: one strangle per monthly cycle. Without this the strategy
            # re-enters the moment an exit leaves it flat, turning ~12 trades a
            # year into as many as the exits allow.
            return []
        dte = ctx.days_to_expiry(self._underlying)
        if not self._min_dte <= dte <= self._max_dte:
            self.note(
                f"no entry: {dte} days to the {cycle.option_expiry} expiry is outside "
                f"the [{self._min_dte}, {self._max_dte}] band"
            )
            return []

        chain = ctx.chain(self._underlying, cycle.option_expiry)
        call = self._select(chain, Right.CE)
        put = self._select(chain, Right.PE)

        if call is None or put is None:
            self.note(self._explain_miss(chain, call, put, dte))
            return []

        credit = self._credit(call, put)
        legs = (
            SignalLeg(
                instrument=call.option, direction=Side.SELL, entry=PriceIntent.market()
            ),
            SignalLeg(
                instrument=put.option, direction=Side.SELL, entry=PriceIntent.market()
            ),
        )
        context = {
            "expiry": cycle.option_expiry.isoformat(),
            "dte": str(dte),
            "futures": str(chain.futures_price),
            "call_strike": str(call.strike),
            "call_delta": f"{call.delta:.4f}" if call.delta is not None else "",
            "call_iv": f"{call.iv:.4f}" if call.iv is not None else "",
            "put_strike": str(put.strike),
            "put_delta": f"{put.delta:.4f}" if put.delta is not None else "",
            "put_iv": f"{put.iv:.4f}" if put.iv is not None else "",
            "credit_per_unit": str(credit),
            "priced_from": f"{call.priced_from}/{put.priced_from}",
        }
        reason = (
            f"short strangle, {dte}d to {cycle.option_expiry}: "
            f"sell {call.strike} CE at delta {call.delta:.3f} and "
            f"{put.strike} PE at delta {put.delta:.3f}, "
            f"futures {chain.futures_price}, credit {credit} per unit"
            if call.delta is not None and put.delta is not None
            else "short strangle"
        )

        self.note(reason)
        return [
            Signal(
                signal_id=signal_id(
                    strategy_id=self.strategy_id,
                    params_hash=self.params_hash(),
                    bar_close_iso=iso(ctx.now),
                    action=SignalAction.OPEN.value,
                    leg_keys=(f"{call.option.key}:SELL", f"{put.option.key}:SELL"),
                    config_hash=self._config_hash,
                ),
                strategy_id=self.strategy_id,
                ts=ctx.now,
                action=SignalAction.OPEN,
                legs=legs,
                atomicity=Atomicity.ALL_OR_NONE,
                combo_take_profit=self._take_profit,
                combo_stop=self._stop_loss,
                reason=reason,
                context=context,
            )
        ]

    # ---------------------------------------------------------------- helpers
    def _select(self, chain: OptionChainSnapshot, right: Right) -> ChainRow | None:
        return chain.nearest_delta(
            float(self._target), right, tolerance=float(self._tolerance)
        )

    def _credit(self, call: ChainRow, put: ChainRow) -> Decimal:
        """Credit per unit, taken from the bid — the side we would actually sell at."""
        call_bid = call.quote.bid or Decimal("0")
        put_bid = put.quote.bid or Decimal("0")
        return call_bid + put_bid

    def _explain_miss(
        self,
        chain: OptionChainSnapshot,
        call: ChainRow | None,
        put: ChainRow | None,
        dte: int,
    ) -> str:
        """Say exactly which side could not be filled and what was on offer instead.

        The distinction that matters: "no strike is listed out there" and "the
        strike is listed but nobody is quoting it" are different problems with
        different answers, and a log line that says only "no entry" hides both.
        """
        missing = []
        if call is None:
            missing.append(f"call at {self._target}±{self._tolerance}")
        if put is None:
            missing.append(f"put at {self._target}±{self._tolerance}")

        tradeable = [r for r in chain.rows if r.is_tradeable and r.delta is not None]
        if not tradeable:
            nearest = "no strike in the chain has both a two-sided quote and a solved delta"
        else:
            best_call = max(
                (r for r in tradeable if r.right is Right.CE),
                key=lambda r: r.delta or 0.0,
                default=None,
            )
            closest = min(
                tradeable,
                key=lambda r: abs(abs(r.delta or 0.0) - float(self._target)),
            )
            nearest = (
                f"closest tradeable is {closest.strike} {closest.right} at delta "
                f"{closest.delta:.3f}"
                if closest.delta is not None
                else "no delta available"
            )
            if best_call is not None and best_call.delta is not None:
                nearest += f"; furthest quoted call is {best_call.strike} at {best_call.delta:.3f}"

        return (
            f"no entry at {dte}d: {' and '.join(missing)} not available — {nearest}. "
            f"{len(tradeable)} of {len(chain.rows)} rows were tradeable."
        )
