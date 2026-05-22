"""Tests for WebCitationVerifier (mocked HTTP via httpx.MockTransport)."""

from __future__ import annotations

import gzip

import httpx
import pytest

from scripts.lib.verifiers.web_citation import WebCitationVerifier


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, follow_redirects=True)


def test_no_citation_url():
    v = WebCitationVerifier(client=httpx.Client())
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "unknown"
    assert "no citation" in r.notes.lower()


def test_empty_citation_url():
    v = WebCitationVerifier(client=httpx.Client())
    r = v.verify("jane@acme.com", citation_url="")
    assert r.status == "unknown"


def test_malformed_url():
    v = WebCitationVerifier(client=httpx.Client())
    r = v.verify("jane@acme.com", citation_url="not a url at all")
    assert r.status == "unknown"


@pytest.mark.parametrize("url", [
    "https://rocketreach.co/jane",
    "https://subdomain.contactout.com/x",
    "https://www.contactout.com/x",
])
def test_aggregator_rejected(url):
    """No HTTP call should be made."""
    called = []

    def handler(request):
        called.append(request.url)
        return httpx.Response(200, text="")

    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("jane@acme.com", citation_url=url)
    assert r.status == "unknown"
    assert called == []


@pytest.mark.parametrize("status_code", [404, 500])
def test_head_non_200(status_code):
    def handler(request):
        return httpx.Response(status_code, text="not found")
    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("jane@huckberry.com", citation_url="https://huckberry.com/team")
    assert r.status == "unknown"
    assert f"HTTP {status_code}" in r.notes


def test_redirect_to_aggregator():
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, text="", headers={"location": "https://apollo.io/x"})
        return httpx.Response(200, text="")

    # Simulate a final URL pointing to aggregator. Use MockTransport with redirects.
    def chain(request):
        if request.url.host == "redirect-source.com":
            return httpx.Response(302, headers={"location": "https://apollo.io/jane"})
        return httpx.Response(200, text="jane huckberry.com")

    v = WebCitationVerifier(client=_client(chain))
    r = v.verify("jane@huckberry.com", citation_url="https://redirect-source.com/")
    assert r.status == "unknown"
    assert "aggregator" in r.notes.lower()


def test_body_contains_both():
    def handler(request):
        return httpx.Response(200, text="Meet our CEO, Aforch from Huckberry.com")
    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("aforch@huckberry.com", citation_url="https://huckberry.com/about")
    assert r.status == "accepted"
    assert r.confidence == "verified-web"
    assert r.source_url == "https://huckberry.com/about"


def test_body_domain_only():
    def handler(request):
        return httpx.Response(200, text="Our company is huckberry.com")
    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("aforch@huckberry.com", citation_url="https://huckberry.com/about")
    assert r.status == "unknown"
    assert "local-part" in r.notes


def test_body_neither():
    def handler(request):
        return httpx.Response(200, text="some other stuff")
    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("aforch@huckberry.com", citation_url="https://huckberry.com/about")
    assert r.status == "unknown"


def test_timeout():
    def handler(request):
        raise httpx.TimeoutException("timeout")
    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("a@b.com", citation_url="https://b.com/team")
    assert r.status == "unknown"
    assert "fetch exc" in r.notes


def test_gzipped_body():
    body = gzip.compress(b"meet aforch at huckberry.com")

    def handler(request):
        return httpx.Response(200, content=body, headers={"content-encoding": "gzip"})

    v = WebCitationVerifier(client=_client(handler))
    r = v.verify("aforch@huckberry.com", citation_url="https://huckberry.com/about")
    assert r.status == "accepted"
