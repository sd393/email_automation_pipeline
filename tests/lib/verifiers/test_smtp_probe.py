"""Tests for SmtpProbeVerifier (mocked SMTP + DNS)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.lib import dns_check
from scripts.lib.verifiers.base import VerifierUnavailable
from scripts.lib.verifiers.smtp_probe import SmtpProbeVerifier


class FakeSmtp:
    """Replaces smtplib.SMTP. Configured via a script of (rcpt_code,) tuples per rcpt."""

    instances: list = []

    def __init__(self, host, port, local_hostname, timeout):
        self.host = host
        self.port = port
        self.local_hostname = local_hostname
        self.timeout = timeout
        self.rcpt_responses: list = list(self._script)
        self.calls: list = []
        FakeSmtp.instances.append(self)

    def helo(self, hostname): self.calls.append(("helo", hostname))
    def mail(self, addr): self.calls.append(("mail", addr))
    def rset(self): self.calls.append(("rset",))
    def quit(self): self.calls.append(("quit",))

    def rcpt(self, addr):
        self.calls.append(("rcpt", addr))
        code = self.rcpt_responses.pop(0)
        return (code, b"ok")


def _factory(*responses_per_instance):
    """Returns a fresh FakeSmtp class with the given list of rcpt response sequences."""
    queues = list(responses_per_instance)

    class _F(FakeSmtp):
        _script = []
        _all_queues = queues

        def __init__(self, *a, **kw):
            self._script = _F._all_queues.pop(0) if _F._all_queues else []
            super().__init__(*a, **kw)

    _F.instances = []
    return _F


def _verifier(factory=None, greylist_retry=True, mx_records=("mail.example.com",), null_mx=False, sleep_calls=None):
    sleep_calls = sleep_calls if sleep_calls is not None else []

    def sleep(s):
        sleep_calls.append(s)

    if factory is None:
        factory = _factory([])
    v = SmtpProbeVerifier(
        rate_per_sec=100.0,
        per_hour_cap=1000,
        greylist_retry=greylist_retry,
        timeout=2.0,
        burst=20,
        smtp_factory=factory,
        sleep=sleep,
    )
    return v, factory, sleep_calls


@pytest.fixture(autouse=True)
def _patch_dns(monkeypatch):
    dns_check.clear_cache()
    monkeypatch.setattr(dns_check, "mx_records", lambda d: ("mail.example.com",))
    monkeypatch.setattr(dns_check, "is_null_mx", lambda d: False)


def test_accepted_path(monkeypatch):
    # One SMTP connection per probe; rcpt response list is [cand, rand].
    factory = _factory([250, 550])
    v, f, _ = _verifier(factory=factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "accepted"
    assert r.confidence == "verified-smtp"
    assert r.source_url == "https://verified-smtp/"


def test_catchall(monkeypatch):
    factory = _factory([250, 250])
    v, f, _ = _verifier(factory=factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "catchall"


def test_rejected(monkeypatch):
    factory = _factory([550, 550])
    v, f, _ = _verifier(factory=factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "rejected"


def test_socket_error(monkeypatch):
    def bad_factory(host, port, local_hostname, timeout):
        raise OSError("connection refused")
    v, f, _ = _verifier(factory=bad_factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "unknown"
    assert "exc" in r.notes


def test_greylist_retry_succeeds(monkeypatch):
    # First connection: cand=450, rand=ignored. After sleep: new connection cand=250, rand=550.
    factory = _factory([450, 0], [250, 550])
    sleep_calls = []
    v, f, _ = _verifier(factory=factory, sleep_calls=sleep_calls)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "accepted"
    assert 90 in sleep_calls


def test_greylist_retry_disabled(monkeypatch):
    factory = _factory([450, 0])
    sleep_calls = []
    v, f, _ = _verifier(factory=factory, greylist_retry=False, sleep_calls=sleep_calls)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "unknown"
    assert 90 not in sleep_calls


@pytest.mark.parametrize("mx", [
    "foo.mail.protection.outlook.com",
    "mx.olc.protection.outlook.com",
    "mail.pphosted.com",
    "mx0.ppe-hosted.com",
    "eu-smtp.mimecast.com",
])
def test_tarpit_short_circuits(monkeypatch, mx):
    monkeypatch.setattr(dns_check, "mx_records", lambda d: (mx,))
    factory = _factory()  # no SMTP responses queued — must not be invoked
    v, f, _ = _verifier(factory=factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "catchall"
    assert "tarpit" in r.notes.lower()
    assert f.instances == []


def test_no_mx(monkeypatch):
    monkeypatch.setattr(dns_check, "mx_records", lambda d: ())
    factory = _factory()
    v, f, _ = _verifier(factory=factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "rejected"
    assert "no MX" in r.notes
    assert f.instances == []


def test_null_mx(monkeypatch):
    monkeypatch.setattr(dns_check, "is_null_mx", lambda d: True)
    factory = _factory()
    v, f, _ = _verifier(factory=factory)
    r = v.verify("jane@acme.com", citation_url=None)
    assert r.status == "rejected"
    assert "null" in r.notes.lower()
    assert f.instances == []


def test_assert_available_blocked(monkeypatch):
    def boom(addr, timeout):
        raise OSError("blocked")
    monkeypatch.setattr("socket.create_connection", boom)
    v, _, _ = _verifier()
    with pytest.raises(VerifierUnavailable) as exc:
        v.assert_available()
    assert "Port 25 blocked" in exc.value.args[0]
    assert "Dartmouth VPN" in exc.value.args[0]


def test_assert_available_open(monkeypatch):
    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("socket.create_connection", lambda *a, **kw: FakeSock())
    v, _, _ = _verifier()
    v.assert_available()  # no exception
