"""Verifier interface shared by every concrete verifier under ``scripts/lib/verifiers/``."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["accepted", "catchall", "rejected", "unknown"]
    confidence: Literal["verified-smtp", "verified-web", "verified-api", ""]
    source_url: str
    notes: str


@runtime_checkable
class Verifier(Protocol):
    name: str

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult: ...

    def assert_available(self) -> None: ...


class VerifierUnavailable(Exception):
    """Raised by ``assert_available()`` when a verifier cannot run in this environment.

    The first arg is a plain remediation string the caller prints verbatim before
    exiting 2.
    """
