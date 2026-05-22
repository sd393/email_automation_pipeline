"""Tests for ApiProviderVerifier (zerobounce wiring + neverbounce stub)."""

from __future__ import annotations

import httpx
import pytest

from scripts.lib.verifiers.api_provider import ApiProviderVerifier
from scripts.lib.verifiers.base import VerifierUnavailable


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("raw,expected_status,expected_conf", [
    ("valid", "accepted", "verified-api"),
    ("invalid", "rejected", ""),
    ("catch-all", "catchall", ""),
    ("unknown", "unknown", ""),
])
def test_status_mapping(raw, expected_status, expected_conf):
    def handler(request):
        return httpx.Response(200, json={"status": raw})

    v = ApiProviderVerifier(provider="zerobounce", api_key="k", client=_client(handler))
    r = v.verify("a@b.com", citation_url=None)
    assert r.status == expected_status
    assert r.confidence == expected_conf


def test_unmapped_status():
    def handler(request):
        return httpx.Response(200, json={"status": "do-not-mail"})
    v = ApiProviderVerifier(provider="zerobounce", api_key="k", client=_client(handler))
    r = v.verify("a@b.com", citation_url=None)
    assert r.status == "unknown"
    assert "unmapped" in r.notes


def test_assert_available_no_key():
    v = ApiProviderVerifier(provider="zerobounce", api_key="", client=httpx.Client())
    with pytest.raises(VerifierUnavailable) as exc:
        v.assert_available()
    assert "ZEROBOUNCE_API_KEY" in exc.value.args[0]


def test_assert_available_401():
    def handler(request):
        return httpx.Response(401)
    v = ApiProviderVerifier(provider="zerobounce", api_key="bad", client=_client(handler))
    with pytest.raises(VerifierUnavailable) as exc:
        v.assert_available()
    assert "Invalid" in exc.value.args[0]


def test_assert_available_ok():
    def handler(request):
        return httpx.Response(200, json={"credits": 100})
    v = ApiProviderVerifier(provider="zerobounce", api_key="ok", client=_client(handler))
    v.assert_available()


def test_connection_error_returns_unknown():
    def handler(request):
        raise httpx.ConnectError("nope")
    v = ApiProviderVerifier(provider="zerobounce", api_key="k", client=_client(handler))
    r = v.verify("a@b.com", citation_url=None)
    assert r.status == "unknown"
    assert "api exc" in r.notes


def test_neverbounce_verify_unimplemented():
    v = ApiProviderVerifier(provider="neverbounce", api_key="k", client=httpx.Client())
    with pytest.raises(NotImplementedError):
        v.verify("a@b.com", citation_url=None)
