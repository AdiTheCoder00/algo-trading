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
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from algo.api.app import TOKEN_ENV, create_app
from algo.core.clock import BacktestClock
from algo.core.timeutil import utc
from algo.persistence.state import EquityRow, PositionRow, SignalRow, StateStore

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
        assert any("placeholder" in w for w in warnings)
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
