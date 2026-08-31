"""Alerting: the one rule is that it can never break trading.

An alerter that raises into the loop, blocks it, or halts it converts a Telegram
outage into a trading outage - strictly worse than having no alerter. So the
tests that matter here are the failure ones: a notifier that throws, a network
that times out, an endpoint that rejects. Each must be swallowed, and the loop's
caller must be able to carry on holding a position.

The second rule is D-128's: the bot token lives in the URL path, so anything
echoing a request URL echoes the token. Nothing here may log one.
"""

from __future__ import annotations

import logging

import pytest

from algo.live.alerts import (
    Alert,
    Alerter,
    LogNotifier,
    NullNotifier,
    Severity,
    TelegramCredentials,
    TelegramNotifier,
    build_alerter,
    redact,
    telegram_credentials_from_env,
)

TOKEN = "123456789:AAFakeTokenNotARealSecret_abcdef"


class _Response:
    def __init__(self, ok: bool, status_code: int = 200, text: str = "") -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text


class _Boom:
    """A notifier that violates the protocol by raising."""

    def deliver(self, alert: Alert) -> bool:
        raise RuntimeError("channel exploded")


class _Counting:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.seen: list[Alert] = []

    def deliver(self, alert: Alert) -> bool:
        self.seen.append(alert)
        return self.ok


class TestNothingCanBreakTrading:
    def test_a_notifier_that_raises_is_swallowed(self) -> None:
        """The protocol says notifiers must not raise. One that does anyway must
        not reach the loop, which is holding a position."""
        alerter = Alerter([_Boom()])

        assert alerter.send(Alert(Severity.CRITICAL, "halt", "kill switch")) == 0

    def test_one_channel_failing_does_not_stop_the_next(self) -> None:
        good = _Counting()
        alerter = Alerter([_Boom(), good])

        delivered = alerter.send(Alert(Severity.WARNING, "drift", "positions differ"))

        assert delivered == 1
        assert len(good.seen) == 1

    def test_a_network_failure_is_swallowed(self) -> None:
        def explode(*args: object, **kwargs: object) -> object:
            raise OSError("connection reset")

        notifier = TelegramNotifier(
            TelegramCredentials(token=TOKEN, chat_id="-100"), post=explode
        )

        assert notifier.deliver(Alert(Severity.INFO, "hello", "world")) is False

    def test_a_rejected_message_is_reported_not_raised(self) -> None:
        notifier = TelegramNotifier(
            TelegramCredentials(token=TOKEN, chat_id="-100"),
            post=lambda *a, **k: _Response(ok=False, status_code=400, text="bad chat"),
        )

        assert notifier.deliver(Alert(Severity.INFO, "hello", "world")) is False

    def test_no_channels_at_all_is_not_an_error(self) -> None:
        assert Alerter([]).send(Alert(Severity.INFO, "x", "y")) == 0


class TestTheTokenNeverReachesALog:
    """D-128: the token is in the URL, and a `requests` exception's own `str()`
    carries it. That is how a live token got written to disk in plaintext."""

    def test_redact_strips_a_token_from_a_url(self) -> None:
        raw = (
            "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
            f"exceeded with url: /bot{TOKEN}/sendMessage (Caused by ...)"
        )

        cleaned = redact(raw)

        assert TOKEN not in cleaned
        assert "/bot<redacted>/sendMessage" in cleaned

    def test_a_network_failure_does_not_log_the_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The end-to-end version: the exception really does carry the URL, and
        the log line really must not."""

        def explode(url: str, **kwargs: object) -> object:
            raise OSError(f"Max retries exceeded with url: {url}")

        notifier = TelegramNotifier(
            TelegramCredentials(token=TOKEN, chat_id="-100"), post=explode
        )

        with caplog.at_level(logging.WARNING):
            notifier.deliver(Alert(Severity.INFO, "hello", "world"))

        assert caplog.text, "expected a warning to be logged"
        assert TOKEN not in caplog.text
        assert "<redacted>" in caplog.text

    def test_a_rejection_body_is_redacted_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier = TelegramNotifier(
            TelegramCredentials(token=TOKEN, chat_id="-100"),
            post=lambda *a, **k: _Response(
                ok=False, status_code=401, text=f"unauthorised for /bot{TOKEN}/sendMessage"
            ),
        )

        with caplog.at_level(logging.WARNING):
            notifier.deliver(Alert(Severity.INFO, "hello", "world"))

        assert TOKEN not in caplog.text


class TestCredentials:
    def test_they_come_from_the_environment(self) -> None:
        creds = telegram_credentials_from_env(
            {"ALGO_TELEGRAM_BOT_TOKEN": TOKEN, "ALGO_TELEGRAM_CHAT_ID": "-100"}
        )

        assert creds.configured is True
        assert creds.missing() == ()

    def test_missing_ones_are_named(self) -> None:
        creds = telegram_credentials_from_env({"ALGO_TELEGRAM_BOT_TOKEN": TOKEN})

        assert creds.configured is False
        assert creds.missing() == ("ALGO_TELEGRAM_CHAT_ID",)

    def test_a_notifier_refuses_to_build_half_configured(self) -> None:
        """Better to refuse at construction than to fail silently on every send."""
        with pytest.raises(ValueError, match="ALGO_TELEGRAM_CHAT_ID"):
            TelegramNotifier(TelegramCredentials(token=TOKEN))


class TestBuildAlerter:
    def test_an_unconfigured_run_still_gets_a_log_channel(self) -> None:
        """Alerting is an operational convenience. Refusing to start a trading
        loop because nobody set a Telegram token would be the tail wagging the
        dog."""
        alerter = build_alerter(env={})

        assert alerter.channels == 1

    def test_telegram_is_added_when_configured(self) -> None:
        alerter = build_alerter(
            env={"ALGO_TELEGRAM_BOT_TOKEN": TOKEN, "ALGO_TELEGRAM_CHAT_ID": "-100"},
            post=lambda *a, **k: _Response(ok=True),
        )

        assert alerter.channels == 2
        assert alerter.send(Alert(Severity.INFO, "up", "loop started")) == 2


class TestRendering:
    def test_the_severity_and_title_are_both_present(self) -> None:
        text = Alert(Severity.CRITICAL, "kill switch tripped", "drawdown 12%").render()

        assert "CRITICAL" in text
        assert "kill switch tripped" in text
        assert "drawdown 12%" in text

    def test_a_rendered_alert_is_ascii(self) -> None:
        """Same reason `TestTheCliSourceStaysAscii` exists: Windows consoles
        default to a legacy code page and `LogNotifier` writes to one, so a
        decorative glyph raises `UnicodeEncodeError` on the operator's machine.
        """
        for severity in Severity:
            text = Alert(severity, "title", "body").render()

            text.encode("cp1252")  # raises if a glyph slipped back in
            assert text.isascii(), text

    def test_the_null_notifier_reports_that_it_delivered_nothing(self) -> None:
        assert NullNotifier().deliver(Alert(Severity.INFO, "x", "y")) is False

    def test_the_log_notifier_reports_success(self) -> None:
        assert LogNotifier().deliver(Alert(Severity.INFO, "x", "y")) is True
