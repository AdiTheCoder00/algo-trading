"""The monitoring API. Brief §9 Milestone 8.

Two properties are worth more than the endpoint coverage.

**Read-only means read-only.** The API is asserted to expose exactly one write
endpoint, and that one only *records a request*. A test enumerates the routes and
fails if a second mutating path ever appears — which is the only version of that
rule that survives someone adding a convenient "close position" button later.

**The kill switch is a request, not an action.** Posting it does not halt anything;
it writes a row the engine reads. The tests assert the 202, assert the engine's own
state is untouched, and assert the request survives for the engine to find.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from algo.api.app import TOKEN_ENV, create_app
from algo.core.clock import BacktestClock
from algo.core.timeutil import utc
from algo.persistence.state import (
    AccountRow,
    EquityRow,
    PositionRow,
    SignalRow,
    StateStore,
)

NOW = utc(2026, 8, 19, 4, 0)
TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(state_path: Path) -> Iterator[StateStore]:
    with StateStore(state_path) as opened:
        yield opened


@pytest.fixture
def client(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    app = create_app(state_path=state_path, clock=BacktestClock(NOW), mode="paper")
    with TestClient(app) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestItRefusesToRunOpen:
    def test_no_token_no_api(self, state_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The kill-switch endpoint mutates behaviour; serving it unauthenticated
        to anything that can reach the port is not a default worth having."""
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        with pytest.raises(RuntimeError, match="refuses to start without a token"):
            create_app(state_path=state_path)

    def test_requests_without_a_token_are_rejected(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 401
        assert client.get("/positions").status_code == 401
        assert client.post("/kill-switch", json={"reason": "x"}).status_code == 401

    def test_a_wrong_token_is_rejected(self, client: TestClient) -> None:
        response = client.get("/health", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401


class TestReadOnly:
    def test_exactly_one_write_endpoint_exists(self, client: TestClient) -> None:
        """The guard that survives someone adding a 'close position' button later."""
        writes = [
            (route.path, method)
            for route in client.app.routes  # type: ignore[attr-defined]
            for method in getattr(route, "methods", set())
            if method in {"POST", "PUT", "PATCH", "DELETE"}
        ]
        assert writes == [("/kill-switch", "POST")], writes

    def test_health_reports_what_the_engine_wrote(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.set_health("engine", "running", at=NOW)
        store.set_health("mode", "paper", at=NOW)
        store.set_health("kill_switch", "armed", at=NOW)
        store.set_health("broker", "connected", at=NOW)

        body = client.get("/health", headers=_auth()).json()
        assert body["status"] == "ok"
        assert body["kill_switch"] == "armed"
        assert body["broker"] == "connected"

    def test_health_surfaces_the_uncalibrated_warnings(
        self, client: TestClient, store: StateStore
    ) -> None:
        """The caveats that make a P&L figure readable rather than misleading."""
        store.set_health("costs_verified", "false", at=NOW)
        store.set_health("spread_measured", "false", at=NOW)
        store.set_health("margin_calibrated", "false", at=NOW)

        warnings = client.get("/health", headers=_auth()).json()["warnings"]
        assert any("not contract-note verified" in w for w in warnings)
        assert any("modelled" in w for w in warnings)
        assert any("stop level is approximate" in w for w in warnings)

    def test_the_equity_curve_comes_back_oldest_first(
        self, client: TestClient, store: StateStore
    ) -> None:
        for index in range(5):
            store.record_equity(
                EquityRow(
                    ts=NOW + timedelta(minutes=30 * index),
                    equity=Decimal("1000000") + Decimal(index),
                    cash=Decimal("1000000"),
                    realised=Decimal("0"),
                    unrealised=Decimal(index),
                    charges=Decimal("0"),
                    open_positions=1,
                )
            )
        rows = client.get("/equity", headers=_auth()).json()
        assert len(rows) == 5
        assert [r["equity"] for r in rows] == [
            "1000000",
            "1000001",
            "1000002",
            "1000003",
            "1000004",
        ]

    def test_money_is_serialised_as_strings(
        self, client: TestClient, store: StateStore
    ) -> None:
        """A dashboard rendering 1000000.0000000001 undermines every other number
        on the page."""
        store.record_equity(
            EquityRow(
                ts=NOW,
                equity=Decimal("1000000.05"),
                cash=Decimal("999999.95"),
                realised=Decimal("0.10"),
                unrealised=Decimal("0"),
                charges=Decimal("0"),
                open_positions=0,
            )
        )
        row = client.get("/equity", headers=_auth()).json()[0]
        assert row["equity"] == "1000000.05"
        assert isinstance(row["equity"], str)

    def test_positions_are_a_snapshot_not_a_log(
        self, client: TestClient, store: StateStore
    ) -> None:
        """An emptied book must show as empty, not as the last thing held."""
        store.replace_positions(
            [
                PositionRow(
                    instrument_key="MCX:GOLDM:20260828:160500:CE",
                    lots=-1,
                    qty=Decimal("-1"),
                    average_price=Decimal("756.00"),
                    mark=Decimal("700.00"),
                    unrealised=Decimal("560.00"),
                    updated_at=NOW,
                )
            ]
        )
        assert len(client.get("/positions", headers=_auth()).json()) == 1

        store.replace_positions([])
        assert client.get("/positions", headers=_auth()).json() == []

    def test_signals_carry_their_reason(
        self, client: TestClient, store: StateStore
    ) -> None:
        """Brief §5: I need to know why a trade fired six weeks later."""
        store.record_signal(
            SignalRow(
                signal_id="abc123",
                ts=NOW,
                strategy="goldm_delta_strangle_v1",
                action="OPEN",
                reason=(
                    "short strangle, 9d to 2026-08-28: sell 160500 CE at delta 0.250 "
                    "and 153000 PE at delta -0.246"
                ),
                context={"dte": "9", "call_strike": "160500", "call_iv": "0.2175"},
            )
        )
        row = client.get("/signals", headers=_auth()).json()[0]
        assert "delta 0.250" in row["reason"]
        assert row["context"]["call_iv"] == "0.2175"

    def test_notes_explain_why_nothing_traded(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.record_note(NOW, "no entry at 9d: call at 0.25±0.05 not available")
        rows = client.get("/notes", headers=_auth()).json()
        assert "not available" in rows[0]["message"]


class TestChain:
    def test_no_chain_is_null_not_an_error(self, client: TestClient) -> None:
        response = client.get("/chain", headers=_auth())
        assert response.status_code == 200
        assert response.json() is None

    def test_a_recorded_chain_comes_back_whole(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.record_chain_snapshot(
            {
                "ts": NOW.isoformat(),
                "underlying": "GOLDM",
                "option_expiry": "2026-08-28",
                "futures_price": "156640",
                "rows": [
                    {
                        "strike": "160500",
                        "right": "CE",
                        "bid": "700.00",
                        "ask": "710.00",
                        "ltp": "705.00",
                        "volume": 40,
                        "iv": 0.216,
                        "delta": 0.25,
                        "tradeable": True,
                        "held": True,
                    }
                ],
            }
        )
        body = client.get("/chain", headers=_auth()).json()
        assert body["underlying"] == "GOLDM"
        assert body["rows"][0]["strike"] == "160500"
        assert body["rows"][0]["held"] is True


class TestTradeStats:
    """Brief §10's summary half. The arithmetic is not re-tested here -
    `tests/test_metrics.py` (or wherever `trade_stats()` itself is tested)
    already owns that - this only proves the endpoint hands it real rows and
    serialises what comes back without losing precision."""

    def _record(
        self,
        store: StateStore,
        *,
        trade_id: str,
        net_pnl: str,
        r_multiple: str | None,
    ) -> None:
        store.record_trade(
            trade_id,
            NOW,
            NOW,
            {
                "trade_id": trade_id,
                "strategy_id": "goldm_delta_strangle_v1",
                "signal_id": trade_id,
                "opened_at": NOW.isoformat(),
                "closed_at": NOW.isoformat(),
                "legs": "MCX:GOLDM:20260828:160500:CE:SELL:1",
                "gross_pnl": net_pnl,
                "charges_total": "0",
                "net_pnl": net_pnl,
                "r_multiple": r_multiple or "",
                "exit_reason": "STOP_LOSS" if net_pnl.startswith("-") else "TAKE_PROFIT",
                "reason": "short strangle",
            },
        )

    def test_win_rate_and_profit_factor_from_real_rows(
        self, client: TestClient, store: StateStore
    ) -> None:
        self._record(store, trade_id="t1", net_pnl="500.00", r_multiple="0.50")
        self._record(store, trade_id="t2", net_pnl="500.00", r_multiple="0.50")
        self._record(store, trade_id="t3", net_pnl="-1000.00", r_multiple="-1.00")

        body = client.get("/trade-stats", headers=_auth()).json()
        assert body["trades"] == 3
        assert body["wins"] == 2
        assert body["losses"] == 1
        assert Decimal(body["win_rate"]) == pytest.approx(Decimal("66.666666"), abs=Decimal("0.01"))
        assert Decimal(body["profit_factor"]) == Decimal("1")  # 1000 gross profit / 1000 gross loss
        assert body["longest_losing_streak"] == 1
        assert body["histogram"], "R-multiples were recorded; the histogram must not be empty"

    def test_money_and_ratios_are_strings_not_floats(
        self, client: TestClient, store: StateStore
    ) -> None:
        self._record(store, trade_id="t1", net_pnl="123.45", r_multiple="1.10")
        body = client.get("/trade-stats", headers=_auth()).json()
        assert isinstance(body["gross_profit"], str)
        assert isinstance(body["expectancy_r"], str)

    def test_no_trades_reports_none_not_zero(
        self, client: TestClient, store: StateStore
    ) -> None:
        """A profit factor of 0.0 reads as 'loses everything'; None reads as
        'nothing to compute this from' - the distinction `trade_stats()` itself
        makes, and this endpoint must not flatten on the way to JSON."""
        del store
        body = client.get("/trade-stats", headers=_auth()).json()
        assert body["trades"] == 0
        assert body["win_rate"] is None
        assert body["profit_factor"] is None
        assert body["expectancy_r"] is None
        assert body["histogram"] == []


class TestStateStoreCrossesRealThreads:
    """The regression `TestClient` cannot see: it dispatches sync endpoints
    through its own portal, not FastAPI's threadpool executor, so a request run
    through it never actually lands its open and its close on different OS
    threads - confirmed by running 30 requests through it against the
    unpatched code with `check_same_thread` still defaulted True, and every one
    of them passed. A real uvicorn server does not have that luxury: it
    genuinely dispatches a sync dependency's open, yield and teardown to
    whichever threadpool worker is free at each step, and that produced
    `sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread` on `/positions` the first time a real browser
    hit it. `ThreadPoolExecutor` here reproduces the actual mechanism - open on
    one real thread, use and close on another - rather than trusting a test
    harness that turned out not to exercise it."""

    def test_a_store_opened_on_one_thread_is_usable_from_another(
        self, state_path: Path
    ) -> None:
        with ThreadPoolExecutor(max_workers=1) as opener:
            store = opener.submit(StateStore, state_path).result()
        with ThreadPoolExecutor(max_workers=1) as user:
            # Both a read and the close itself must survive landing on a
            # thread that did not construct the connection.
            user.submit(store.positions).result()
            user.submit(store.close).result()


class TestKillSwitchIsARequest:
    def test_posting_it_returns_accepted_not_done(self, client: TestClient) -> None:
        """202, because the engine acts at its next bar. A dashboard showing
        'halted' the instant this returned would be lying until then."""
        response = client.post(
            "/kill-switch",
            headers=_auth(),
            json={"reason": "stepping away from the desk"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["request_id"] > 0
        assert "acts on this at its next bar" in body["note"]

    def test_the_request_waits_for_the_engine(
        self, client: TestClient, store: StateStore
    ) -> None:
        """A dead engine cannot swallow a halt — it is still here when it returns."""
        client.post("/kill-switch", headers=_auth(), json={"reason": "halt"})
        pending = store.pending_kill_switch_requests()
        assert len(pending) == 1
        assert pending[0].reason == "halt"
        assert pending[0].acted_on_at is None

    def test_the_engine_marks_it_acted_on(
        self, client: TestClient, store: StateStore
    ) -> None:
        client.post("/kill-switch", headers=_auth(), json={"reason": "halt"})
        request = store.pending_kill_switch_requests()[0]
        store.mark_kill_switch_acted(request.id, at=NOW + timedelta(minutes=1))
        assert store.pending_kill_switch_requests() == []

    def test_flatten_defaults_to_off(self, client: TestClient, store: StateStore) -> None:
        """D-012: market-closing a strangle during the move that tripped the limit
        can cost more than the breach."""
        client.post("/kill-switch", headers=_auth(), json={"reason": "halt"})
        assert store.pending_kill_switch_requests()[0].flatten is False

    def test_flatten_can_be_asked_for_explicitly(
        self, client: TestClient, store: StateStore
    ) -> None:
        client.post(
            "/kill-switch", headers=_auth(), json={"reason": "halt", "flatten": True}
        )
        assert store.pending_kill_switch_requests()[0].flatten is True

    def test_a_reason_is_mandatory(self, client: TestClient) -> None:
        assert client.post("/kill-switch", headers=_auth(), json={}).status_code == 422
        assert (
            client.post("/kill-switch", headers=_auth(), json={"reason": ""}).status_code
            == 422
        )

    def test_there_is_no_reset_endpoint(self, client: TestClient) -> None:
        """Tripping is one click; clearing it is a terminal, a look at why, and a
        person who has done both."""
        paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
        assert not any("reset" in path for path in paths)
        assert client.delete("/kill-switch", headers=_auth()).status_code == 405

    def test_the_history_is_visible(self, client: TestClient) -> None:
        client.post("/kill-switch", headers=_auth(), json={"reason": "first"})
        client.post("/kill-switch", headers=_auth(), json={"reason": "second"})
        history = client.get("/kill-switch", headers=_auth()).json()
        assert [h["reason"] for h in history] == ["second", "first"]


class TestResearchDoesNotWeakenTheReadOnlyRule:
    """A backtest console reads history and returns numbers. It must not become
    the crack in the one property this module is built around.

    `TestReadOnly.test_exactly_one_write_endpoint_exists` above is the guard;
    these assert the research surface stays on the correct side of it rather
    than the guard being widened to accommodate it.
    """

    def test_the_backtest_endpoint_is_a_get(self, client: TestClient) -> None:
        """Nullipotent operations are GETs. Making this a POST to carry
        parameters would have cost the 'exactly one write endpoint' guarantee
        for nothing but request-shape convenience."""
        routes = {
            (route.path, method)
            for route in client.app.routes  # type: ignore[attr-defined]
            for method in getattr(route, "methods", set())
        }

        assert ("/research/backtest", "GET") in routes
        assert ("/research/backtest", "POST") not in routes

    def test_the_catalogue_needs_a_token_like_everything_else(
        self, client: TestClient
    ) -> None:
        assert client.get("/research/catalogue").status_code == 401

    def test_the_backtest_needs_a_token_like_everything_else(
        self, client: TestClient
    ) -> None:
        response = client.get(
            "/research/backtest", params={"strategy": "breakout", "timeframe_minutes": 30}
        )

        assert response.status_code == 401

    def test_the_catalogue_describes_what_the_engine_actually_offers(
        self, client: TestClient
    ) -> None:
        """Served from the engine's own definitions so the console cannot offer
        a parameter no strategy has, or default one differently."""
        response = client.get(
            "/research/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}
        )

        assert response.status_code == 200
        body = response.json()
        assert {s["id"] for s in body["strategies"]} == {"breakout", "macd"}
        names = {p["name"] for p in body["parameters"]}
        assert {"stop_loss_pct", "trail_pct", "trail_activation_pct", "lots"} <= names
        # `lookback` is a Donchian channel length; MACD has no such knob, and the
        # console must not render one for it.
        lookback = next(p for p in body["parameters"] if p["name"] == "lookback")
        assert lookback["applies_to"] == ["breakout"]


def _account(**overrides: object) -> AccountRow:
    fields: dict[str, object] = {
        "login": "25804244",
        "server": "VantageMarkets-Demo",
        "currency": "USD",
        "trade_mode": "demo",
        "leverage": 100,
        "balance": Decimal("108805.15"),
        "equity": Decimal("108647.25"),
        "margin_used": Decimal("3412.00"),
        "margin_free": Decimal("105235.25"),
        "margin_level": Decimal("3184.7"),
        "floating_pnl": Decimal("-157.90"),
        "open_tickets": 1,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return AccountRow(**fields)  # type: ignore[arg-type]


class TestTheBrokerAccount:
    """The account panel's data - the broker's claim, not the engine's book.

    The two are separate on purpose and the endpoint keeps them separate: this
    reports what the venue says is there, and nothing here is derived from the
    equity curve the engine writes.
    """

    def test_a_run_with_no_broker_account_reports_none_not_zeros(
        self, client: TestClient
    ) -> None:
        """A backtest, a replay and a paper run have no account behind them.
        Zeros would render as a real account that happens to be empty, which
        is a different and false statement."""
        response = client.get("/account", headers=_auth())
        assert response.status_code == 200
        assert response.json() is None

    def test_it_reports_what_the_broker_said(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.record_account(_account())
        body = client.get("/account", headers=_auth()).json()
        assert body["login"] == "25804244"
        assert body["server"] == "VantageMarkets-Demo"
        assert body["trade_mode"] == "demo"
        assert body["open_tickets"] == 1

    def test_every_money_field_is_a_string(
        self, client: TestClient, store: StateStore
    ) -> None:
        """Same rule as every other endpoint: a Decimal that passes through a
        JSON number has passed through a float."""
        store.record_account(_account())
        body = client.get("/account", headers=_auth()).json()
        for field in (
            "balance",
            "equity",
            "margin_used",
            "margin_free",
            "margin_level",
            "floating_pnl",
        ):
            assert isinstance(body[field], str), field

    def test_a_flat_account_reports_a_null_margin_level(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.record_account(_account(margin_level=None, open_tickets=0))
        assert client.get("/account", headers=_auth()).json()["margin_level"] is None

    def test_it_is_a_snapshot_so_the_newest_write_wins(
        self, client: TestClient, store: StateStore
    ) -> None:
        """A snapshot, not a log - the question is what the account looks like
        now, and the equity table already keeps the historical half."""
        store.record_account(_account())
        store.record_account(_account(balance=Decimal("99000.00")))
        assert client.get("/account", headers=_auth()).json()["balance"] == "99000.00"

    def test_it_needs_a_token_like_everything_else(self, client: TestClient) -> None:
        assert client.get("/account").status_code == 401


class TestTheEngineStatusCannotLieAboutBeingAlive:
    """`engine: running` is written at startup and `stopped` only by the exit
    handler - which a crashed process never reaches. So the word alone cannot
    distinguish a working loop from a dead one, and a heartbeat is what makes
    the difference reportable.
    """

    def test_a_fresh_heartbeat_reads_as_ok(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.set_health("engine", "running", at=NOW)
        store.set_health("heartbeat", NOW.isoformat(), at=NOW)

        body = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert body["status"] == "ok"
        assert not [w for w in body["warnings"] if "still says" in w]

    def test_a_stale_heartbeat_is_reported_rather_than_believed(
        self, client: TestClient, store: StateStore
    ) -> None:
        """The case this exists for: the process died without ever recording a
        stop, so the file still says running."""
        long_ago = NOW - timedelta(hours=2)
        store.set_health("engine", "running", at=long_ago)
        store.set_health("heartbeat", long_ago.isoformat(), at=long_ago)

        body = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert body["status"] == "stale"
        assert any("still says" in w for w in body["warnings"])

    def test_a_stopped_engine_is_never_called_stale(
        self, client: TestClient, store: StateStore
    ) -> None:
        """A cleanly stopped loop has an old heartbeat by definition. Reporting
        that as a suspected crash would cry wolf on the normal case."""
        long_ago = NOW - timedelta(hours=2)
        store.set_health("engine", "stopped", at=long_ago)
        store.set_health("heartbeat", long_ago.isoformat(), at=long_ago)

        body = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert body["status"] == "stopped"
        assert not [w for w in body["warnings"] if "still says" in w]

    def test_no_heartbeat_at_all_is_not_treated_as_stale(
        self, client: TestClient, store: StateStore
    ) -> None:
        """A state file written by an older build has no heartbeat field. That
        must not make a running loop look dead."""
        store.set_health("engine", "running", at=NOW)

        body = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert body["status"] == "ok"

    def test_an_unparseable_heartbeat_is_treated_as_absent(
        self, client: TestClient, store: StateStore
    ) -> None:
        store.set_health("engine", "running", at=NOW)
        store.set_health("heartbeat", "not-a-timestamp", at=NOW)

        body = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert body["status"] == "ok"
