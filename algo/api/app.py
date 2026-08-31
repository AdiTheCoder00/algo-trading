"""FastAPI: read-only monitoring, plus the one button that halts trading.

Brief §9 Milestone 8 — "FastAPI read-only endpoints + Next.js: equity curve, open
positions, trade log, current signals with their `reason`, system health, kill
switch button."

Three constraints shape this file.

**Read-only means read-only.** Every endpoint but one is a SELECT against a SQLite
file the engine writes. The API never holds a `Portfolio`, an `OrderRouter` or a
broker connection, so no HTTP request can perturb trading state — not through a
bug, not through a badly-shaped query. A dashboard that can move a position is not
a dashboard.

**The kill switch is a request, not an action.** `POST /kill-switch` writes a row
saying a halt was asked for; the engine trips its own switch on the next bar. The
API never touches the switch. That way a dead API cannot leave the engine
half-tripped, and a dead engine cannot swallow the request — it is still waiting
when the engine comes back.

**There is no reset endpoint.** Tripping the switch is one click; clearing it is
not. Un-tripping a halt is a decision that deserves a terminal, a look at why it
tripped, and a person who has done both. `algo killswitch --reset` exists for
that. Q21 settled the same way for parameters: they change through config and a
restart, never through the UI, so every live parameter traces to a committed file.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from algo.core.clock import Clock, SystemClock
from algo.persistence.state import StateStore

TOKEN_ENV = "ALGO_API_TOKEN"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class HealthResponse(BaseModel):
    model_config = _FROZEN

    status: str
    mode: str
    kill_switch: str
    broker: str
    last_bar: str | None
    detail: dict[str, str]
    warnings: list[str] = Field(default_factory=list)


class KillSwitchBody(BaseModel):
    model_config = _FROZEN

    reason: str = Field(min_length=1, description="Why the halt was requested")
    flatten: bool = Field(
        default=False,
        description=(
            "Also close open positions. Off by default (D-012): market-closing a "
            "short strangle during the move that tripped the limit can cost more "
            "than the breach."
        ),
    )
    requested_by: str = Field(default="dashboard", min_length=1)


class KillSwitchResponse(BaseModel):
    model_config = _FROZEN

    request_id: int
    accepted_at: datetime
    note: str


def create_app(
    *,
    state_path: Path | str,
    clock: Clock | None = None,
    mode: str = "paper",
    require_token: bool = True,
) -> FastAPI:
    """Build the monitoring API over the state file at `state_path`."""
    the_clock = clock or SystemClock()
    expected_token = os.environ.get(TOKEN_ENV, "")

    if require_token and not expected_token:
        raise RuntimeError(
            f"{TOKEN_ENV} is not set. The kill-switch endpoint mutates trading "
            "behaviour, so the API refuses to start without a token rather than "
            "serving it open to anything that can reach the port."
        )

    app = FastAPI(
        title="GOLDM strangle engine",
        description="Read-only monitoring. The only write is a kill-switch request.",
        version="0.1.0",
    )

    def store() -> Iterator[StateStore]:
        opened = StateStore(state_path)
        try:
            yield opened
        finally:
            opened.close()

    def check_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if not require_token:
            return
        supplied = ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        # Constant-time comparison: a timing side channel on the token that guards
        # the kill switch is a small hole in an important door.
        if not secrets.compare_digest(supplied, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
            )

    Guarded = Depends(check_token)
    Store = Depends(store)

    # ------------------------------------------------------------------ reads
    @app.get("/health", response_model=HealthResponse)
    def health(state: StateStore = Store, _: None = Guarded) -> HealthResponse:
        detail = state.health()
        curve = state.equity_curve(limit=1)
        pending = state.pending_kill_switch_requests()

        warnings: list[str] = []
        if detail.get("costs_verified") == "false":
            warnings.append(
                "charge rates are sourced, not contract-note verified - "
                "net P&L is not calibrated to the paisa"
            )
        if detail.get("spread_measured") == "false":
            warnings.append("spread is modelled, not measured")
        if detail.get("margin_calibrated") == "false":
            warnings.append("margin is approximated, so the stop level is approximate too")
        if pending:
            warnings.append(f"{len(pending)} kill-switch request(s) not yet acted on")

        return HealthResponse(
            status="ok" if detail.get("engine") == "running" else "unknown",
            mode=detail.get("mode", mode),
            kill_switch=detail.get("kill_switch", "unknown"),
            broker=detail.get("broker", "unknown"),
            last_bar=curve[-1].ts.isoformat() if curve else None,
            detail=detail,
            warnings=warnings,
        )

    @app.get("/equity")
    def equity(
        limit: int = 1000, state: StateStore = Store, _: None = Guarded
    ) -> list[dict[str, Any]]:
        return [
            {
                "ts": row.ts.isoformat(),
                # Serialised as strings, not floats. A dashboard that renders
                # 1000000.0000000001 undermines every other number on the page.
                "equity": str(row.equity),
                "cash": str(row.cash),
                "realised": str(row.realised),
                "unrealised": str(row.unrealised),
                "charges": str(row.charges),
                "open_positions": row.open_positions,
            }
            for row in state.equity_curve(limit=limit)
        ]

    @app.get("/positions")
    def positions(state: StateStore = Store, _: None = Guarded) -> list[dict[str, Any]]:
        return [
            {
                "instrument": row.instrument_key,
                "lots": row.lots,
                "qty": str(row.qty),
                "average_price": str(row.average_price),
                "mark": str(row.mark) if row.mark is not None else None,
                "unrealised": str(row.unrealised) if row.unrealised is not None else None,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in state.positions()
        ]

    @app.get("/chain")
    def chain(state: StateStore = Store, _: None = Guarded) -> dict[str, Any] | None:
        """The option chain as of the most recent bar - every strike, its delta
        and IV, and which legs (if any) the strategy is actually holding.
        `None` when the run has no chain wired in (the M3 falsification and
        anything trading the underlying directly) or has not reached a bar yet."""
        return state.chain_snapshot()

    @app.get("/trades")
    def trades(
        limit: int = 100, state: StateStore = Store, _: None = Guarded
    ) -> list[dict[str, Any]]:
        return state.trades(limit=limit)

    @app.get("/trade-stats")
    def trade_stats_endpoint(state: StateStore = Store, _: None = Guarded) -> dict[str, Any]:
        """Win rate, profit factor, expectancy, the R-multiple distribution -
        brief §10's summary half, computed by the same tested `trade_stats()`
        the CLI's tearsheet already uses (algo/reporting/metrics.py), not a
        second implementation of the same arithmetic in this file.

        `trade_stats()` only ever reads `.net_pnl` and `.r_multiple` off each
        trade, both of which already survive the flattening `to_log_row()` does
        for storage - so a minimal stand-in carrying just those two fields is
        enough, without reconstructing the full `Trade` (legs, per-leg charges)
        that was never persisted in that shape to begin with.
        """
        from dataclasses import dataclass
        from decimal import Decimal

        from algo.reporting.metrics import trade_stats

        @dataclass
        class _Row:
            net_pnl: Decimal
            r_multiple: Decimal | None

        # A strangle trades on the order of ~12 cycles a year; there is no
        # realistic account where this needs a smaller window than "all of them".
        rows = state.trades(limit=100_000)
        parsed = [
            _Row(
                net_pnl=Decimal(row["net_pnl"]),
                r_multiple=Decimal(row["r_multiple"]) if row.get("r_multiple") else None,
            )
            for row in rows
        ]
        stats = trade_stats(parsed)  # type: ignore[arg-type]
        return {
            "trades": stats.trades,
            "wins": stats.wins,
            "losses": stats.losses,
            "win_rate": str(stats.win_rate) if stats.win_rate is not None else None,
            "profit_factor": str(stats.profit_factor) if stats.profit_factor is not None else None,
            "gross_profit": str(stats.gross_profit),
            "gross_loss": str(stats.gross_loss),
            "largest_win": str(stats.largest_win) if stats.largest_win is not None else None,
            "largest_loss": str(stats.largest_loss) if stats.largest_loss is not None else None,
            "longest_losing_streak": stats.longest_losing_streak,
            "trades_with_r": stats.trades_with_r,
            "expectancy_r": str(stats.expectancy_r) if stats.expectancy_r is not None else None,
            "average_win_r": str(stats.average_win_r) if stats.average_win_r is not None else None,
            "average_loss_r": (
                str(stats.average_loss_r) if stats.average_loss_r is not None else None
            ),
            "histogram": [{"label": label, "count": count} for label, count in stats.histogram()],
        }

    @app.get("/signals")
    def signals(
        limit: int = 50, state: StateStore = Store, _: None = Guarded
    ) -> list[dict[str, Any]]:
        """Signals with their reasons — brief §5's six-weeks-later question."""
        return [
            {
                "signal_id": row.signal_id,
                "ts": row.ts.isoformat(),
                "strategy": row.strategy,
                "action": row.action,
                "reason": row.reason,
                "context": row.context,
            }
            for row in state.signals(limit=limit)
        ]

    @app.get("/notes")
    def notes(
        limit: int = 50, state: StateStore = Store, _: None = Guarded
    ) -> list[dict[str, str]]:
        """Why the strategy did *not* trade. Just as informative as why it did."""
        return [
            {"ts": row.ts.isoformat(), "message": row.message}
            for row in state.notes(limit=limit)
        ]

    @app.get("/kill-switch")
    def kill_switch_history(
        state: StateStore = Store, _: None = Guarded
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": request.id,
                "requested_at": request.requested_at.isoformat(),
                "requested_by": request.requested_by,
                "reason": request.reason,
                "flatten": request.flatten,
                "acted_on_at": request.acted_on_at.isoformat()
                if request.acted_on_at
                else None,
            }
            for request in state.kill_switch_requests()
        ]

    # ------------------------------------------------------------- research
    # Reads history, returns numbers, touches no live state. The read-only rule
    # at the top of this module is about *trading* state; see
    # `algo/backtest/research.py` for the full argument.
    @app.get("/research/catalogue")
    def research_catalogue(_: None = Guarded) -> dict[str, Any]:
        """Strategies, timeframes and parameter ranges the console may offer.

        Served from the engine's own definitions so the UI cannot present a
        parameter the strategy does not have, or default one differently from
        its constructor.
        """
        from algo.backtest.research import catalogue

        return catalogue()

    @app.get("/research/backtest")
    def research_backtest(
        strategy: str,
        timeframe_minutes: int,
        symbol: str = "XAUUSD",
        lookback: str = "20",
        stop_loss_pct: str = "0",
        trail_activation_pct: str = "2",
        trail_pct: str = "0",
        lots: str = "100",
        bars: str = "5000",
        _: None = Guarded,
    ) -> dict[str, Any]:
        """Run one backtest over MT5 history and return its result.

        **A GET, deliberately.** `TestReadOnly.test_exactly_one_write_endpoint_
        exists` guards this module's central promise - "the guard that survives
        someone adding a 'close position' button later" - and a study has no
        business weakening it. The operation is nullipotent: it reads history,
        computes, and returns numbers, holding no portfolio, router or broker
        and writing to no state file. That is precisely what GET is for, so the
        invariant stays literally true rather than explained away.

        Errors come back as 400 with the engine's own message rather than a
        500: "Stop loss % must be between 0 and 25" is actionable in the
        console, and a stack trace is not.
        """
        import MetaTrader5 as mt5

        from algo.backtest.research import run_study
        from algo.core.errors import AlgoError

        if not mt5.initialize():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"cannot attach to the MT5 terminal: {mt5.last_error()}. "
                    "Is it running and logged in?"
                ),
            )
        try:
            return run_study(
                mt5,
                strategy=strategy,
                timeframe_minutes=timeframe_minutes,
                symbol=symbol,
                params={
                    "lookback": lookback,
                    "stop_loss_pct": stop_loss_pct,
                    "trail_activation_pct": trail_activation_pct,
                    "trail_pct": trail_pct,
                    "lots": lots,
                    "bars": bars,
                },
            )
        except AlgoError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        finally:
            mt5.shutdown()

    @app.get("/research/walk-forward")
    def research_walk_forward(
        strategy: str,
        timeframe_minutes: int,
        symbol: str = "XAUUSD",
        axis: str = "lookback",
        second_axis: str = "",
        in_sample_days: int = 90,
        out_of_sample_days: int = 30,
        lookback: str = "20",
        stop_loss_pct: str = "0",
        trail_activation_pct: str = "2",
        trail_pct: str = "0",
        lots: str = "100",
        bars: str = "5000",
        _: None = Guarded,
    ) -> dict[str, Any]:
        """Walk-forward: optimise on each window, report what was never fitted.

        A GET for the same reason `/research/backtest` is one - nullipotent, and
        the "exactly one write endpoint" guard is worth more than request-shape
        convenience.
        """
        import MetaTrader5 as mt5

        from algo.backtest.research import run_walk_forward_study
        from algo.core.errors import AlgoError

        if not mt5.initialize():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"cannot attach to the MT5 terminal: {mt5.last_error()}. "
                    "Is it running and logged in?"
                ),
            )
        try:
            return run_walk_forward_study(
                mt5,
                strategy=strategy,
                timeframe_minutes=timeframe_minutes,
                symbol=symbol,
                axis=axis,
                second_axis=second_axis,
                in_sample_days=in_sample_days,
                out_of_sample_days=out_of_sample_days,
                params={
                    "lookback": lookback,
                    "stop_loss_pct": stop_loss_pct,
                    "trail_activation_pct": trail_activation_pct,
                    "trail_pct": trail_pct,
                    "lots": lots,
                    "bars": bars,
                },
            )
        except AlgoError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        finally:
            mt5.shutdown()

    @app.get("/research/sweep")
    def research_sweep(
        strategy: str,
        timeframe_minutes: int = 60,
        symbol: str = "XAUUSD",
        row_axis: str = "timeframe",
        column_axis: str = "stop_loss_pct",
        lookback: str = "20",
        stop_loss_pct: str = "0",
        trail_activation_pct: str = "2",
        trail_pct: str = "0",
        lots: str = "100",
        bars: str = "5000",
        _: None = Guarded,
    ) -> dict[str, Any]:
        """A two-axis grid of backtests, scored for whether its peak is real.

        A GET, for the reason `/research/backtest` is one.
        """
        import MetaTrader5 as mt5

        from algo.backtest.research import run_sweep_study
        from algo.core.errors import AlgoError

        if not mt5.initialize():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"cannot attach to the MT5 terminal: {mt5.last_error()}. "
                    "Is it running and logged in?"
                ),
            )
        try:
            return run_sweep_study(
                mt5,
                strategy=strategy,
                timeframe_minutes=timeframe_minutes,
                symbol=symbol,
                row_axis=row_axis,
                column_axis=column_axis,
                params={
                    "lookback": lookback,
                    "stop_loss_pct": stop_loss_pct,
                    "trail_activation_pct": trail_activation_pct,
                    "trail_pct": trail_pct,
                    "lots": lots,
                    "bars": bars,
                },
            )
        except AlgoError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        finally:
            mt5.shutdown()

    # ------------------------------------------------- the only write endpoint
    @app.post("/kill-switch", response_model=KillSwitchResponse, status_code=202)
    def request_kill_switch(
        body: KillSwitchBody, state: StateStore = Store, _: None = Guarded
    ) -> KillSwitchResponse:
        """Ask the engine to halt. Returns 202: accepted, not yet done.

        The status code is not decoration. The halt has been *recorded*, and the
        engine acts on it at its next bar — so a dashboard that showed "halted"
        the instant this returned would be lying for as long as that takes.
        """
        now = the_clock.now()
        request_id = state.request_kill_switch(
            requested_by=body.requested_by,
            reason=body.reason,
            flatten=body.flatten,
            at=now,
        )
        return KillSwitchResponse(
            request_id=request_id,
            accepted_at=now,
            note=(
                "halt requested. The engine acts on this at its next bar; poll "
                "/health until kill_switch reports tripped."
            ),
        )

    return app
