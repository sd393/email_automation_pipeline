"""DNS helpers used by source/discover/verify stages.

Wraps ``dnspython`` with a small public surface:

* :func:`mx_records` — MX hostnames sorted by preference (lowest first).
* :func:`is_null_mx` — RFC 7505 null MX detection.
* :func:`has_mail` — can this domain plausibly receive mail?

Results are cached process-wide via ``functools.lru_cache``.
"""

from __future__ import annotations

from functools import lru_cache

import dns.exception
import dns.resolver


@lru_cache(maxsize=1024)
def mx_records(domain: str, timeout: float = 5.0) -> tuple[str, ...]:
    """Return MX hostnames sorted by preference (lowest first).

    Empty tuple on NoAnswer / NXDOMAIN. Re-raises ``dns.exception.Timeout`` so
    the caller can decide what to do.
    """
    d = domain.lower().strip().rstrip(".")
    if not d:
        return ()
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        answer = resolver.resolve(d, "MX")
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return ()
    records = sorted(answer, key=lambda r: r.preference)
    return tuple(r.exchange.to_text().rstrip(".") for r in records)


@lru_cache(maxsize=1024)
def is_null_mx(domain: str) -> bool:
    """True iff the domain advertises RFC 7505 null MX (single MX, pref 0, '.')."""
    d = domain.lower().strip().rstrip(".")
    if not d:
        return False
    try:
        resolver = dns.resolver.Resolver()
        answer = list(resolver.resolve(d, "MX"))
    except (
        dns.resolver.NoAnswer,
        dns.resolver.NXDOMAIN,
        dns.resolver.NoNameservers,
        dns.exception.Timeout,
    ):
        return False
    if len(answer) != 1:
        return False
    only = answer[0]
    target = only.exchange.to_text().rstrip(".")
    return only.preference == 0 and target in ("", ".")


@lru_cache(maxsize=1024)
def has_mail(domain: str) -> bool:
    """True if ``domain`` can plausibly receive mail.

    Logic:
      * If MX records exist and are not null MX → True.
      * If no MX but an A record exists (RFC 5321 fallback) → True.
      * Otherwise (incl. timeouts) → False.
    """
    d = domain.lower().strip().rstrip(".")
    if not d:
        return False
    try:
        if is_null_mx(d):
            return False
        mx = mx_records(d)
        if mx:
            return True
        resolver = dns.resolver.Resolver()
        try:
            resolver.resolve(d, "A")
            return True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            return False
    except dns.exception.Timeout:
        return False


def clear_cache() -> None:
    """Clear all DNS caches. Useful in tests."""
    mx_records.cache_clear()
    is_null_mx.cache_clear()
    has_mail.cache_clear()
