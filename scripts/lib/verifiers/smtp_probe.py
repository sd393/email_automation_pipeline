"""SMTP RFC-5321 verifier.

Single-thread-safe (internal rate limiter is thread-safe but the verifier holds
no per-call state). MX-tarpit hard-skip avoids opening sockets against providers
that always RCPT 250 regardless of recipient.
"""

from __future__ import annotations

import fnmatch
import socket
import string
import time
from typing import Callable

from scripts.lib import dns_check
from scripts.lib.rate_limit import HourlyLimiter, RateLimiter
from scripts.lib.verifiers.base import VerificationResult, VerifierUnavailable


TARPIT_MX_PATTERNS: tuple[str, ...] = (
    "*.mail.protection.outlook.com",
    "*.olc.protection.outlook.com",
    "*.pphosted.com",
    "*.ppe-hosted.com",
    "*.mimecast.com",
)


def _is_tarpit(host: str) -> str | None:
    h = host.lower().rstrip(".")
    for pattern in TARPIT_MX_PATTERNS:
        if fnmatch.fnmatchcase(h, pattern):
            return pattern
    return None


class SmtpProbeVerifier:
    name = "smtp_probe"

    def __init__(
        self,
        *,
        rate_per_sec: float,
        per_hour_cap: int,
        greylist_retry: bool,
        timeout: float = 10.0,
        burst: int = 10,
        smtp_factory: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.greylist_retry = greylist_retry
        self.timeout = timeout
        self._sleep = sleep
        self._rate = RateLimiter(rate_per_sec=rate_per_sec, burst=burst, clock=clock, sleep=sleep)
        self._hourly = HourlyLimiter(per_hour=per_hour_cap, burst=burst, clock=clock, sleep=sleep)
        if smtp_factory is None:
            import smtplib
            smtp_factory = smtplib.SMTP
        self._smtp_factory = smtp_factory

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def assert_available(self) -> None:
        try:
            with socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=5):
                return
        except OSError as e:
            raise VerifierUnavailable(
                'Port 25 blocked. Connect to Dartmouth VPN, or set verifier.chain to '
                '["web_citation"] in the brief, or enable api_provider.'
            ) from e

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        try:
            local, domain = email.split("@", 1)
        except ValueError:
            return VerificationResult(
                status="unknown", confidence="", source_url="", notes="malformed email",
            )

        self._rate.acquire()
        self._hourly.acquire()

        if dns_check.is_null_mx(domain):
            return VerificationResult(
                status="rejected", confidence="", source_url="", notes="null MX",
            )
        mx = dns_check.mx_records(domain)
        if not mx:
            return VerificationResult(
                status="rejected", confidence="", source_url="", notes="no MX",
            )

        tarpit = _is_tarpit(mx[0])
        if tarpit:
            return VerificationResult(
                status="catchall", confidence="", source_url="",
                notes=f"MX tarpit ({tarpit})",
            )

        return self._probe(local, domain, mx[0])

    def _probe(self, local: str, domain: str, mx_host: str) -> VerificationResult:
        candidate = f"{local}@{domain}"
        random_local = "x" + "".join(self._random_chars(19))
        random_addr = f"{random_local}@{domain}"

        try:
            cand_code, rand_code = self._probe_once(mx_host, candidate, random_addr)
        except Exception as exc:  # noqa: BLE001
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes=f"exc: {type(exc).__name__}",
            )

        if 400 <= cand_code < 500 and self.greylist_retry:
            self._sleep(90)
            try:
                cand_code, rand_code = self._probe_once(mx_host, candidate, random_addr)
            except Exception as exc:  # noqa: BLE001
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes=f"exc on retry: {type(exc).__name__}",
                )

        if cand_code == 250:
            if 500 <= rand_code < 600:
                return VerificationResult(
                    status="accepted", confidence="verified-smtp",
                    source_url="https://verified-smtp/", notes="",
                )
            if rand_code == 250:
                return VerificationResult(
                    status="catchall", confidence="", source_url="",
                    notes="domain accepts every recipient",
                )
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes=f"random probe code {rand_code}",
            )
        if 500 <= cand_code < 600:
            return VerificationResult(
                status="rejected", confidence="", source_url="",
                notes=f"smtp {cand_code}",
            )
        return VerificationResult(
            status="unknown", confidence="", source_url="",
            notes=f"smtp {cand_code}",
        )

    def _probe_once(self, mx_host: str, candidate: str, random_addr: str) -> tuple[int, int]:
        """Open SMTP, HELO, MAIL FROM, RCPT candidate + RCPT random. Returns
        ``(cand_code, rand_code)``. Closes the connection unconditionally.
        """
        local_hostname = socket.getfqdn() or "localhost"
        probe_from = f"postmaster@{local_hostname}"
        smtp = self._smtp_factory(host=mx_host, port=25, local_hostname=local_hostname, timeout=self.timeout)
        try:
            smtp.helo(local_hostname)
            smtp.mail(probe_from)
            cand_code, _ = smtp.rcpt(candidate)
            try:
                smtp.rset()
            except Exception:
                pass
            try:
                smtp.mail(probe_from)
                rand_code, _ = smtp.rcpt(random_addr)
            except Exception:
                rand_code = 0
            return (cand_code, rand_code)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    @staticmethod
    def _random_chars(n: int) -> list[str]:
        import random
        return random.choices(string.ascii_lowercase + string.digits, k=n)
