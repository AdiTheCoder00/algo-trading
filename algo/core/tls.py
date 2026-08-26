"""Outbound TLS trust.

Lives in `core` because it is not one venue's problem. It was found through
Angel One and fixed there, and Kotak then worked only by accident - `algo live`
happened to construct the SmartAPI transport first, which injected the fix
process-wide before the Kotak SDK ever opened a socket. Reordering those two
lines would have broken the broker connection for a reason nobody would look for
in this file (D-113).
"""

from __future__ import annotations


def trust_the_os_certificate_store() -> None:
    """Point Python's SSL contexts at the OS certificate store.

    Some environments intercept outbound HTTPS locally - antivirus web shields
    doing TLS scanning are the common case, confirmed here: the real certificate
    the broker's server presents gets swapped in transit for one issued by
    "Norton Antivirus for SSL/TLS scanning". The OS trusts that root; Python's
    bundled `certifi` list does not, so a plain HTTPS call fails with
    `CERTIFICATE_VERIFY_FAILED` even though the connection is fine.

    This **does not weaken verification** - it changes which already-vetted set
    of roots is consulted, the same set curl and every other OS-native tool
    already uses, so a genuinely bad certificate still fails. Disabling
    verification instead would have accepted this one blindly along with
    everything else.

    Idempotent: safe to call from every transport's constructor, which is the
    point - each one should be able to connect without depending on some other
    transport having been built first.
    """
    import truststore

    truststore.inject_into_ssl()
