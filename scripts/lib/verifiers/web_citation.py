"""Web-citation verifier: HEAD-200 + body contains local-part AND domain."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from scripts.lib.verifiers.base import VerificationResult


AGGREGATOR_HOSTS: frozenset[str] = frozenset({
    "contactout.com", "rocketreach.co", "rocketreach.com",
    "zoominfo.com", "apollo.io", "lusha.com", "hunter.io",
    "success.ai", "snov.io", "leadiq.com", "salesintel.com",
    "dropcontact.com", "getprospect.com", "kendo.tools",
    "signalhire.com", "swordfish.ai", "voilanorbert.com",
    "skrapp.io", "anymailfinder.com", "nymeria.io", "uplead.com",
})


def _is_aggregator(host: str) -> bool:
    h = host.lower()
    if h.startswith("www."):
        h = h[4:]
    if h in AGGREGATOR_HOSTS:
        return True
    return any(h.endswith("." + a) for a in AGGREGATOR_HOSTS)


class WebCitationVerifier:
    name = "web_citation"

    def __init__(self, *, fetch_timeout: float = 8.0, client: httpx.Client | None = None) -> None:
        self.fetch_timeout = fetch_timeout
        self._client = client

    def assert_available(self) -> None:
        return None

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self.fetch_timeout, follow_redirects=True)

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        if not citation_url:
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes="no citation URL provided",
            )
        try:
            parsed = urlparse(citation_url)
        except Exception:
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes="malformed citation URL",
            )
        if not parsed.scheme or not parsed.netloc:
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes="malformed citation URL",
            )
        if _is_aggregator(parsed.netloc):
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes="aggregator citation rejected",
            )

        try:
            local, domain = email.split("@", 1)
        except ValueError:
            return VerificationResult(
                status="unknown", confidence="", source_url="", notes="malformed email",
            )
        local = local.lower()
        domain = domain.lower()

        client = self._get_client()
        owns_client = self._client is None
        try:
            try:
                head = client.head(citation_url)
            except Exception as e:
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes=f"fetch exc: {type(e).__name__}",
                )
            if head.status_code != 200:
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes=f"citation URL not reachable (HTTP {head.status_code})",
                )
            final_host = (head.url.host or "").lower()
            if _is_aggregator(final_host):
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes="redirected to aggregator",
                )

            try:
                resp = client.get(citation_url)
            except Exception as e:
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes=f"fetch exc: {type(e).__name__}",
                )

            body = (resp.text or "").lower()
            has_local = local in body
            has_domain = domain in body
            if has_local and has_domain:
                return VerificationResult(
                    status="accepted", confidence="verified-web",
                    source_url=citation_url, notes="",
                )
            if has_domain and not has_local:
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes="local-part not on citation page",
                )
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes="neither local-part nor domain on citation page",
            )
        finally:
            if owns_client:
                client.close()
