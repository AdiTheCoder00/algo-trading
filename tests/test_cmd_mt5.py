"""The helpers inside `algo live-mt5`, exercised without a terminal.

The two commands in `cmd_mt5.py` need a running MetaTrader5 terminal, which no
CI machine has and which cannot be faked at the module boundary - the SDK is
imported inside them. So most of that file is untestable by construction, and
padding a number would be the wrong answer.

What *is* testable is the pair of helpers the loop calls on every pass, and both
carry a contract that is easy to break and invisible when broken.

**`_alert_on` decides what wakes a person.** An alert per poll is noise nobody
reads, and an alerter people mute is an alerter that is not there. Only routed
orders and router refusals qualify; the overwhelming majority of passes must say
nothing at all.

**`_record_account` must never raise.** `alerts.py` states the rule: a panel that
cannot be drawn is not a reason to stop trading. If this throws, a dashboard
hiccup takes down a live loop - the exact inversion of what it is for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from algo.cli.cmd_mt5 import _alert_on, _record_account
from algo.execution.router import Outcome, RoutingResult
from algo.live.alerts import Alert, Alerter, Severity
from algo.live.loop import PassResult

NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


class RecordingNotifier:
    """A real notifier that keeps what it was handed."""

    def __init__(self) -> None:
        self.delivered: list[Alert] = []

    def deliver(self, alert: Alert) -> bool:
        self.delivered.append(alert)
        return True


def _alerter() -> tuple[Alerter, RecordingNotifier]:
    notifier = RecordingNotifier()
    return Alerter([notifier]), notifier


def _routed(outcome: Outcome, order_id: str, detail: str = "") -> RoutingResult:
    return RoutingResult(outcome=outcome, client_order_id=order_id, detail=detail)


def _pass(*routed: RoutingResult) -> PassResult:
    return PassResult(ts=NOW, bar_ts=NOW, routed=tuple(routed))


class TestWhatWakesAPerson:
    def test_a_quiet_pass_says_nothing_at_all(self) -> None:
        """The overwhelming majority of passes. One message each and the alerter
        gets muted, at which point it may as well not exist."""
        alerter, notifier = _alerter()

        _alert_on(alerter, _pass())

        assert notifier.delivered == []

    def test_a_pass_that_only_settled_fills_is_still_quiet(self) -> None:
        """Settlement is the loop working normally. Only *routing* - an order
        sent, or refused - is worth an interruption."""
        alerter, notifier = _alerter()

        _alert_on(alerter, PassResult(ts=NOW, bar_ts=NOW))

        assert notifier.delivered == []

    def test_placed_orders_raise_an_informational_alert(self) -> None:
        alerter, notifier = _alerter()

        _alert_on(alerter, _pass(_routed(Outcome.PLACED, "XL-1")))

        assert len(notifier.delivered) == 1
        alert = notifier.delivered[0]
        assert alert.severity is Severity.INFO
        assert "1 order(s) placed" in alert.title
        assert "XL-1" in alert.body
        assert alert.at == NOW

    def test_a_refusal_is_a_warning_not_an_info(self) -> None:
        """A refusal is the reconcile-before-send rule working, but it also means
        intent and reality may now differ - which deserves a person, not a log
        line. Severity is how that difference is communicated."""
        alerter, notifier = _alerter()

        _alert_on(
            alerter,
            _pass(_routed(Outcome.BLOCKED_UNRECONCILED, "XL-2", "position already open")),
        )

        alert = notifier.delivered[0]
        assert alert.severity is Severity.WARNING
        assert "not placed" in alert.title
        assert "position already open" in alert.body, (
            "the reason is the whole value of the alert; without it the operator "
            "has to go and look"
        )

    def test_placed_and_refused_in_one_pass_produce_both_alerts(self) -> None:
        """They are separate messages on purpose: a placed order and a refusal
        are different facts at different severities, and merging them would hide
        the refusal inside good news."""
        alerter, notifier = _alerter()

        _alert_on(
            alerter,
            _pass(
                _routed(Outcome.PLACED, "XL-3"),
                _routed(Outcome.REJECTED, "XL-4", "kill switch tripped"),
            ),
        )

        severities = [a.severity for a in notifier.delivered]
        assert severities == [Severity.INFO, Severity.WARNING]

    def test_every_routed_order_appears_in_the_body(self) -> None:
        """Counting them in the title and listing one in the body would be worse
        than useless - it would look complete."""
        alerter, notifier = _alerter()

        _alert_on(
            alerter,
            _pass(
                _routed(Outcome.PLACED, "XL-5"),
                _routed(Outcome.PLACED, "XL-6"),
            ),
        )

        body = notifier.delivered[0].body
        assert "XL-5" in body and "XL-6" in body
        assert "2 order(s) placed" in notifier.delivered[0].title


class FakeSnapshot:
    login = "12345"
    server = "Vantage-Demo"
    currency = "USD"
    trade_mode = "DEMO"
    leverage = 500
    balance = Decimal("10000.00")
    equity = Decimal("10120.50")
    margin_used = Decimal("250.00")
    margin_free = Decimal("9870.50")
    margin_level = Decimal("4048.20")
    floating_pnl = Decimal("120.50")
    open_tickets = 2


class RecordingStore:
    def __init__(self, *, boom: bool = False) -> None:
        self.rows: list[Any] = []
        self.boom = boom

    def record_account(self, row: Any) -> None:
        if self.boom:
            raise RuntimeError("state db is locked")
        self.rows.append(row)


class FakeBroker:
    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom

    def account(self) -> Any:
        if self.boom:
            raise ConnectionError("terminal went away")
        return FakeSnapshot()


class TestTheAccountSnapshotNeverStopsTheLoop:
    def test_a_healthy_snapshot_is_recorded(self) -> None:
        store = RecordingStore()

        _record_account(store, FakeBroker(), at=NOW)

        assert len(store.rows) == 1
        row = store.rows[0]
        assert row.login == "12345"
        assert row.equity == Decimal("10120.50")

    def test_the_timestamp_is_carried_so_a_stale_panel_is_visible(self) -> None:
        """A number that has stopped moving is only visible as stale if the row
        says when it was taken."""
        store = RecordingStore()

        _record_account(store, FakeBroker(), at=NOW)

        assert store.rows[0].updated_at == NOW

    def test_a_broker_that_cannot_answer_does_not_raise(
        self, capsys: Any
    ) -> None:
        """`account_info` returning nothing on one poll is a blip, not a halt.
        Raising here would let a dashboard read stop a live trading loop."""
        store = RecordingStore()

        _record_account(store, FakeBroker(boom=True), at=NOW)

        assert store.rows == []
        assert "account snapshot skipped" in capsys.readouterr().out

    def test_a_store_that_cannot_write_does_not_raise(self, capsys: Any) -> None:
        """The other half: the read succeeded and the write failed. Same rule -
        the panel is not worth the session."""
        store = RecordingStore(boom=True)

        _record_account(store, FakeBroker(), at=NOW)

        assert "account snapshot skipped" in capsys.readouterr().out

    def test_the_failure_says_what_went_wrong(self, capsys: Any) -> None:
        """Swallowed is not the same as silent. A snapshot that stops updating
        with no explanation is a panel nobody can debug."""
        _record_account(RecordingStore(), FakeBroker(boom=True), at=NOW)

        assert "terminal went away" in capsys.readouterr().out
