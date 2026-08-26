"""Logging setup, so `LoggingConfig` decides something.

Until D-115 nothing configured structlog at all. It still *emitted* - the SDKs
call `get_logger` and structlog falls back to a default JSON renderer - so
`json_format: true` in config happened to describe what was happening without
causing it, and `level: INFO` filtered nothing. That is the same failure as a
setting that does nothing: a reader cannot tell a working knob from an inert one,
so both have to be assumed inert.

Deliberately small. This does not invent a logging policy; it makes the two
settings that already existed real, and adds a file sink because a live session
that only logs to a terminal has no record once the terminal closes.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog


def configure_logging(
    *, level: str = "INFO", json_format: bool = True, file: Path | None = None
) -> None:
    """Point structlog at one renderer and one level. Idempotent.

    `json_format=False` gives the console renderer, which is worth having when a
    person is reading: the JSON that a broker SDK emits on every request is
    unreadable at a terminal and is most of what comes out of a live session.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file, encoding="utf-8"))

    logging.basicConfig(
        format="%(message)s", level=numeric, handlers=handlers, force=True
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
