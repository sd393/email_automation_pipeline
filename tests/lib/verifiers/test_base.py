"""Tests for the verifier interface."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scripts.lib.verifiers.base import VerificationResult, Verifier, VerifierUnavailable


def test_verifier_protocol_satisfied():
    class Dummy:
        name = "dummy"

        def verify(self, email, *, citation_url):
            return VerificationResult(status="unknown", confidence="", source_url="", notes="")

        def assert_available(self):
            return None

    assert isinstance(Dummy(), Verifier)


def test_verification_result_status_enum():
    with pytest.raises(ValidationError):
        VerificationResult(status="bogus", confidence="", source_url="", notes="")


def test_verification_result_confidence_enum():
    with pytest.raises(ValidationError):
        VerificationResult(status="accepted", confidence="bogus", source_url="", notes="")


def test_verifier_unavailable_carries_message():
    try:
        raise VerifierUnavailable("Port 25 blocked. Connect to VPN.")
    except VerifierUnavailable as e:
        assert e.args[0] == "Port 25 blocked. Connect to VPN."
