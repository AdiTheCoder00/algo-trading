"""Synthetic option chains, for proving the strategy.

The same caveat as every other synthetic fixture, restated because it matters more
here than anywhere else: **this proves the strategy's logic, never its
profitability.** A generated chain always quotes, always fills, and always prices
off the model that generated it. A short-premium strategy tested against a
generator that never runs out of bids will look considerably better than one
tested against the book on your screen — where 157500 and 159000 showed no put
quote at all.

The generator therefore takes a `quote_gaps` argument, and the strategy tests use
it. A chain fixture with no gaps would be testing a market that does not exist.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Right
from algo.core.errors import DomainError
from algo.core.instrument import FutureId, OptionId
from algo.core.quote import Quote
from algo.pricing.black76 import greeks


def build_chain(
    *,
    ts: datetime,
    underlying_future: FutureId,
    option_expiry: date,
    futures_price: Decimal,
    expires_at: datetime,
    vol: float = 0.2175,
    r: float = 0.065,
    strike_interval: Decimal = Decimal("500"),
    strikes_each_side: int = 20,
    strike_centre: Decimal | None = None,
    half_spread: Decimal = Decimal("5.00"),
    skew_per_strike: float = 0.0,
    quote_gaps: frozenset[Decimal] = frozenset(),
    tick: Decimal = Decimal("0.50"),
    populate_greeks: bool = False,
) -> OptionChainSnapshot:
    """Generate a chain priced by Black-76.

    `skew_per_strike` tilts volatility by that much per strike step away from the
    money, so a fixture can reproduce the rising call volatility seen on the live
    chain (21.53% at 155000 to 22.51% at 159000) rather than assuming a flat
    surface that would misprice exactly the wings a strangle sells.

    `quote_gaps` names strikes that will be listed but not quoted. Strikes in this
    set produce a row with an empty book, which is what a thin option ladder
    actually looks like and what the strategy has to cope with.

    `populate_greeks` fills `iv` and `delta` straight from the parameters used to
    generate the row, skipping the solver. For a fixture this is exactly
    equivalent — the solver would recover the same volatility it was given — and
    it makes multi-session fixtures fast enough to be worth running. Real chains
    always go through `pricing.chain_greeks.enrich`.
    """
    if strike_interval <= 0:
        raise DomainError(f"strike interval must be positive, got {strike_interval}")
    seconds = (expires_at - ts).total_seconds()
    if seconds <= 0:
        raise DomainError("chain timestamp must be before expiry")
    t = seconds / (86400.0 * 365.0)

    # The ladder is anchored to a fixed centre, not to wherever price happens to
    # be. Real strikes do not move when the underlying does — the money moves
    # through a static ladder, and a strike listed last week is still listed.
    centre = strike_centre if strike_centre is not None else futures_price
    atm = (centre / strike_interval).quantize(Decimal("1")) * strike_interval

    rows: list[ChainRow] = []
    for step in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + strike_interval * Decimal(step)
        if strike <= 0:
            continue
        strike_vol = max(vol + skew_per_strike * step, 0.01)

        for right in (Right.CE, Right.PE):
            option = OptionId(
                underlying_future=underlying_future,
                option_expiry=option_expiry,
                strike=strike,
                right=right,
            )
            if strike in quote_gaps:
                rows.append(ChainRow(option=option, quote=Quote(exchange_ts=ts, received_ts=ts)))
                continue

            computed = greeks(float(futures_price), float(strike), t, strike_vol, r, right)
            # Floored at one tick, not dropped. A far out-of-the-money option a day
            # from expiry is worth almost nothing, but the exchange still quotes it
            # at the minimum tick — and a position in it still has to be marked.
            # Dropping the row instead produced a held leg the engine could not
            # price, which `require_mark` correctly refused to treat as zero.
            mid = max(_on_tick(Decimal(str(round(computed.price, 4))), tick), tick)

            rows.append(
                ChainRow(
                    option=option,
                    quote=Quote(
                        exchange_ts=ts,
                        received_ts=ts,
                        bid=max(mid - half_spread, tick),
                        ask=mid + half_spread,
                        bid_qty=10,
                        ask_qty=10,
                        ltp=mid,
                        volume=50,
                    ),
                    iv=strike_vol if populate_greeks else None,
                    delta=computed.delta if populate_greeks else None,
                    priced_from="MID" if populate_greeks else "",
                )
            )

    rows.sort(key=lambda row: (row.strike, row.right.value))
    return OptionChainSnapshot(
        ts=ts,
        underlying=underlying_future.underlying,
        option_expiry=option_expiry,
        futures_price=futures_price,
        rows=tuple(rows),
    )


def _on_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).quantize(Decimal("1")) * tick
