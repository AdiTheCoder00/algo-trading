"""Exception taxonomy.

Brief §12: no silent `except: pass`. Every caught exception is logged with context,
and every raised exception carries enough detail to reconstruct what went wrong.

The split that matters operationally is Retryable vs Fatal on the broker boundary:
the router retries the first and escalates the second. Everything else is a
programming or data error and should stop the run.
"""

from __future__ import annotations


class AlgoError(Exception):
    """Base for every error this system raises deliberately."""


class ConfigError(AlgoError):
    """Configuration is missing, malformed, or internally inconsistent."""


class ModeError(ConfigError):
    """Trading-mode guard refused. See brief §2.1 — live is gated twice."""


class DomainError(AlgoError):
    """A domain invariant was violated (bad OHLC, negative quantity, ...)."""


class DataError(AlgoError):
    """Input data is unusable: gaps, duplicates, non-monotonic timestamps."""


class LookAheadError(AlgoError, IndexError):
    """Something tried to read data at or beyond the current bar's future.

    This is the canary from brief §7.3. If this is ever raised outside a test,
    a real look-ahead bug exists and the run must not be trusted.

    It subclasses `IndexError` as well as `AlgoError` so that a `BarWindow` still
    behaves like a well-mannered sequence — anything iterating it stops cleanly —
    while a test can still assert on the specific, louder type.
    """


class CalendarError(AlgoError):
    """A date could not be resolved against the exchange calendar."""


class SpecError(AlgoError):
    """No contract specification is in force for the requested instrument/date."""


class BrokerError(AlgoError):
    """Base for broker-adapter failures."""


class RetryableBrokerError(BrokerError):
    """Transient: timeout, rate limit, 5xx. The router may retry."""


class FatalBrokerError(BrokerError):
    """Terminal: auth failure, rejected order, malformed request. Do not retry."""
