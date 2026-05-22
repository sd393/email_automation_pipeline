"""Feature-flagged API verifier (zerobounce primary, neverbounce stub)."""

from __future__ import annotations

from typing import Literal

import httpx

from scripts.lib.verifiers.base import VerificationResult, VerifierUnavailable


ZEROBOUNCE_STATUS_MAP = {
    "valid": ("accepted", "verified-api", "https://zerobounce-api/"),
    "invalid": ("rejected", "", ""),
    "catch-all": ("catchall", "", ""),
    "unknown": ("unknown", "", ""),
    "spamtrap": ("unknown", "", ""),
    "abuse": ("unknown", "", ""),
    "do_not_mail": ("unknown", "", ""),
}


class ApiProviderVerifier:
    name = "api_provider"

    def __init__(
        self,
        *,
        provider: Literal["zerobounce", "neverbounce"],
        api_key: str,
        client: httpx.Client | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.timeout = timeout
        self._client = client

    def _get_client(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=self.timeout)

    def assert_available(self) -> None:
        if not self.api_key:
            env_var = "ZEROBOUNCE_API_KEY" if self.provider == "zerobounce" else "NEVERBOUNCE_API_KEY"
            raise VerifierUnavailable(f"{env_var} not set in config/secrets.env")
        client = self._get_client()
        owns = self._client is None
        try:
            if self.provider == "zerobounce":
                url = f"https://api.zerobounce.net/v2/getcredits?api_key={self.api_key}"
            else:
                url = f"https://api.neverbounce.com/v4/account/info?key={self.api_key}"
            try:
                resp = client.get(url)
            except Exception as e:
                raise VerifierUnavailable(f"{self.provider} unreachable: {type(e).__name__}") from e
            if resp.status_code in (401, 403):
                raise VerifierUnavailable(f"Invalid {self.provider} API key")
            if not (200 <= resp.status_code < 300):
                raise VerifierUnavailable(f"{self.provider} unreachable: HTTP {resp.status_code}")
        finally:
            if owns:
                client.close()

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        if self.provider == "neverbounce":
            raise NotImplementedError("neverbounce verify not implemented in v1")
        client = self._get_client()
        owns = self._client is None
        try:
            try:
                resp = client.get(
                    f"https://api.zerobounce.net/v2/validate?api_key={self.api_key}&email={email}"
                )
            except Exception as e:
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes=f"api exc: {type(e).__name__}",
                )
            if not (200 <= resp.status_code < 300):
                return VerificationResult(
                    status="unknown", confidence="", source_url="",
                    notes=f"api http {resp.status_code}",
                )
            try:
                data = resp.json()
            except Exception:
                return VerificationResult(
                    status="unknown", confidence="", source_url="", notes="api non-json",
                )
            raw = (data.get("status") or "").lower()
            if raw in ZEROBOUNCE_STATUS_MAP:
                status, conf, src = ZEROBOUNCE_STATUS_MAP[raw]
                return VerificationResult(
                    status=status, confidence=conf, source_url=src,
                    notes="" if status != "unknown" else f"zerobounce status={raw}",
                )
            return VerificationResult(
                status="unknown", confidence="", source_url="",
                notes=f"unmapped: {raw}",
            )
        finally:
            if owns:
                client.close()
