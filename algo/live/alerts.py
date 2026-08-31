"""Proactive alerts: tell the operator when something happened, not when asked.

The system already has a kill switch and a dashboard. Both are *pull* - they
answer a question someone thought to ask. If the engine halts at 11pm, nobody
learns that until they look, and the whole point of a kill switch tripping is
that something needs a person.

## Nothing here may ever break trading

The single rule this module is written around. A notifier that can raise into
the loop, block it, or halt it is worse than no notifier at all: it converts a
Telegram outage into a trading outage. So `Alerter.send` **never raises** -
every failure is caught, logged and dropped, and the return value says whether
it got through for a caller that cares. The loop does not care.

That is why sending is not retried here either. `macd_alert.py` retries with
backoff because delivering the alert *is* its job; here the job is trading, and
a loop that sleeps through a backoff is a loop not watching the market.

## Credentials from the environment, never from config

`ALGO_TELEGRAM_BOT_TOKEN` and `ALGO_TELEGRAM_CHAT_ID`, read directly. Same rule
`credentials_from_env` follows for the broker SDKs (and the reason
`ALGO_TELEGRAM_*` is on the loader's non-config list): a secret that reaches
`AppConfig` reaches `config_hash`, which is stamped into every signal id and run
artefact.

## The token never reaches a log

Telegram puts the bot token in the URL path, so a `requests` exception's own
`str()` carries it - which is how D-128 found the live token being written to
`macd_alert.log` in plaintext on any network blip. Every log line here goes
through `redact`, and the tests assert a token cannot survive one.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)

#: Telegram embeds the bot token in the URL path; anything echoing a request
#: URL echoes the token with it.
_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")

#: How long to wait on the notification endpoint. Short on purpose: this runs
#: inside a trading loop's pass, and a slow alert must not delay a decision.
TIMEOUT_S = 5.0


def redact(text: str) -> str:
    """Strip a Telegram bot token out of anything bound for a log."""
    return _TOKEN_IN_URL.sub("/bot<redacted>", text)


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing worth telling a person about."""

    severity: Severity
    title: str
    body: str
    at: datetime | None = None

    def render(self) -> str:
        # ASCII only, for the reason `TestTheCliSourceStaysAscii` already gives:
        # Windows consoles default to a legacy code page, and the `LogNotifier`
        # writes to one. A pretty glyph that raises `UnicodeEncodeError` on the
        # operator's machine is worse than a plain one that does not - found by
        # printing a rendered alert on this very machine.
        mark = {
            Severity.INFO: "*",
            Severity.WARNING: "!",
            Severity.CRITICAL: "!!",
        }[self.severity]
        stamp = f"\n{self.at.isoformat()}" if self.at is not None else ""
        return f"{mark} {self.severity} - {self.title}\n{self.body}{stamp}"


@runtime_checkable
class Notifier(Protocol):
    """Somewhere an alert can be delivered. Implementations must not raise."""

    def deliver(self, alert: Alert) -> bool: ...


class NullNotifier:
    """Delivers nothing and says so. The default, so an unconfigured run is
    silent rather than broken."""

    def deliver(self, alert: Alert) -> bool:
        del alert
        return False


class LogNotifier:
    """Writes the alert to the log. Useful on its own, and the fallback when no
    channel is configured but the operator still wants a record."""

    def deliver(self, alert: Alert) -> bool:
        LOG.warning("ALERT %s: %s - %s", alert.severity, alert.title, alert.body)
        return True


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    token: str = ""
    chat_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def missing(self) -> tuple[str, ...]:
        required = {
            "ALGO_TELEGRAM_BOT_TOKEN": self.token,
            "ALGO_TELEGRAM_CHAT_ID": self.chat_id,
        }
        return tuple(name for name, value in required.items() if not value)


def telegram_credentials_from_env(
    env: dict[str, str] | None = None,
) -> TelegramCredentials:
    """Read `ALGO_TELEGRAM_*`. Never sourced from config - see the docstring."""
    source = os.environ if env is None else env
    return TelegramCredentials(
        token=source.get("ALGO_TELEGRAM_BOT_TOKEN", ""),
        chat_id=source.get("ALGO_TELEGRAM_CHAT_ID", ""),
    )


class TelegramNotifier:
    """Sends to a Telegram chat. One attempt, no retry, never raises."""

    __slots__ = ("_chat_id", "_post", "_token")

    def __init__(
        self,
        credentials: TelegramCredentials,
        *,
        post: Any = None,
    ) -> None:
        if not credentials.configured:
            raise ValueError(
                "TelegramNotifier needs both a token and a chat id; missing "
                f"{', '.join(credentials.missing())}"
            )
        self._token = credentials.token
        self._chat_id = credentials.chat_id
        # Injected so tests never touch a socket, and so the `requests` import
        # stays lazy - a backtest should not pay for an HTTP library.
        self._post = post

    def _sender(self) -> Any:
        if self._post is not None:
            return self._post
        import requests

        from algo.core.tls import trust_the_os_certificate_store

        # D-113, and the exact accident its docstring warns about: the fix was
        # only ever injected by the SmartAPI and Kotak transports' constructors,
        # so a run that builds neither - `live-mt5` builds neither - reached
        # Telegram with Python's bundled roots and failed
        # `CERTIFICATE_VERIFY_FAILED` behind a TLS-scanning antivirus. Found by
        # running `telegram-check` against a real endpoint; the injected-`post`
        # tests could never have caught it. Idempotent by design.
        trust_the_os_certificate_store()
        return requests.post

    def deliver(self, alert: Alert) -> bool:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            response = self._sender()(
                url,
                data={
                    "chat_id": self._chat_id,
                    "text": alert.render(),
                    "disable_web_page_preview": True,
                },
                timeout=TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - an alert must never break trading
            LOG.warning("alert not delivered: %s", redact(str(exc)))
            return False
        ok = bool(getattr(response, "ok", False))
        if not ok:
            LOG.warning(
                "alert rejected (%s): %s",
                getattr(response, "status_code", "?"),
                redact(str(getattr(response, "text", ""))[:300]),
            )
        return ok


class Alerter:
    """Fans one alert out to every configured channel, swallowing failures.

    A list rather than a single notifier so a run can log *and* message without
    the caller branching. Delivery to one channel failing never stops the next
    from being tried.
    """

    __slots__ = ("_notifiers",)

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        self._notifiers = list(notifiers or [])

    @property
    def channels(self) -> int:
        return len(self._notifiers)

    def send(self, alert: Alert) -> int:
        """Deliver to every channel. Returns how many accepted it.

        Never raises. A notifier that throws despite the protocol saying it must
        not is caught here too - the loop calling this is holding a position.
        """
        delivered = 0
        for notifier in self._notifiers:
            try:
                if notifier.deliver(alert):
                    delivered += 1
            except Exception as exc:  # noqa: BLE001 - see the module docstring
                LOG.warning(
                    "notifier %s raised: %s",
                    type(notifier).__name__,
                    redact(str(exc)),
                )
        return delivered

    def info(self, title: str, body: str, *, at: datetime | None = None) -> int:
        return self.send(Alert(Severity.INFO, title, body, at))

    def warning(self, title: str, body: str, *, at: datetime | None = None) -> int:
        return self.send(Alert(Severity.WARNING, title, body, at))

    def critical(self, title: str, body: str, *, at: datetime | None = None) -> int:
        return self.send(Alert(Severity.CRITICAL, title, body, at))


def build_alerter(
    *,
    env: dict[str, str] | None = None,
    to_log: bool = True,
    post: Any = None,
) -> Alerter:
    """Assemble whatever the environment actually supports.

    An unconfigured run gets a log-only alerter rather than an error: alerting
    is an operational convenience, and refusing to start a trading loop because
    nobody set a Telegram token would be the tail wagging the dog.
    """
    notifiers: list[Notifier] = []
    if to_log:
        notifiers.append(LogNotifier())
    credentials = telegram_credentials_from_env(env)
    if credentials.configured:
        notifiers.append(TelegramNotifier(credentials, post=post))
    return Alerter(notifiers)
