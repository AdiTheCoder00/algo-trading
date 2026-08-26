"""The two process-wide side effects (D-115).

Both are small wrappers around a library call, and both were written because
something depended on them implicitly: logging config existed in the schema and
did nothing, and TLS trust worked only because `algo live` happened to build the
SmartAPI transport before the Kotak one.

Neither can be tested by its observable output without capturing global state, so
these check the shapes that matter - that the settings reach the library, and
that calling twice is safe, which is the property both rely on.
"""

from __future__ import annotations

import logging
from pathlib import Path

import structlog

from algo.core.logging import configure_logging
from algo.core.tls import trust_the_os_certificate_store


class TestConfigureLogging:
    def test_the_level_reaches_the_root_logger(self) -> None:
        configure_logging(level="WARNING")

        assert logging.getLogger().level == logging.WARNING

        configure_logging(level="INFO")  # restore for the rest of the suite

    def test_an_unknown_level_falls_back_rather_than_raising(self) -> None:
        """A typo in config must not take the process down at startup; INFO is
        the safe default and the value is visible in the file either way."""
        configure_logging(level="NOT_A_LEVEL")

        assert logging.getLogger().level == logging.INFO

    def test_json_and_console_pick_different_renderers(self) -> None:
        configure_logging(json_format=True)
        json_processors = structlog.get_config()["processors"]
        configure_logging(json_format=False)
        console_processors = structlog.get_config()["processors"]

        assert type(json_processors[-1]) is not type(console_processors[-1])
        assert isinstance(json_processors[-1], structlog.processors.JSONRenderer)

        configure_logging()  # restore

    def test_a_file_sink_is_created_and_written_to(self, tmp_path: Path) -> None:
        """A live session that only logs to a terminal has no record once the
        terminal closes."""
        target = tmp_path / "nested" / "live.log"

        configure_logging(level="INFO", file=target)
        logging.getLogger("test").info("hello")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert target.exists()
        assert "hello" in target.read_text(encoding="utf-8")

        configure_logging()  # detach the file handler for the rest of the suite

    def test_calling_it_twice_is_safe(self) -> None:
        """Every command configures at startup, and the test suite calls it
        repeatedly; duplicated handlers would multiply every line."""
        configure_logging()
        first = len(logging.getLogger().handlers)
        configure_logging()

        assert len(logging.getLogger().handlers) == first


class TestTrustTheOsCertificateStore:
    def test_it_is_idempotent(self) -> None:
        """Both transports call it in their own constructors now, so a second
        call must be a no-op rather than an error (D-113)."""
        trust_the_os_certificate_store()
        trust_the_os_certificate_store()

    def test_verification_is_still_on(self) -> None:
        """The whole point: this changes *which* roots are trusted, it does not
        disable checking. A default context that stopped verifying would accept
        anything, which is what the tempting fix would have done."""
        import ssl

        context = ssl.create_default_context()

        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True
