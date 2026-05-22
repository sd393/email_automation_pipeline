"""Tests for scripts.lib.dns_check (mocked dnspython)."""

from __future__ import annotations

import dns.resolver
import pytest

from scripts.lib import dns_check


@pytest.fixture(autouse=True)
def _clear_cache():
    dns_check.clear_cache()
    yield
    dns_check.clear_cache()


def _patch_resolve(mocker, mapping):
    """mapping: {(domain, rdtype): answer | Exception}."""

    def fake_resolve(self, name, rdtype):
        key = (str(name).rstrip(".").lower(), rdtype)
        if key not in mapping:
            raise dns.resolver.NXDOMAIN()
        v = mapping[key]
        if isinstance(v, Exception):
            raise v
        return v

    return mocker.patch("dns.resolver.Resolver.resolve", autospec=True, side_effect=fake_resolve)


def test_mx_records_sorted_by_preference(mocker, fake_dns_answer):
    answer = fake_dns_answer([(20, "mx2.acme.com."), (10, "mx1.acme.com.")])
    _patch_resolve(mocker, {("acme.com", "MX"): answer})
    assert dns_check.mx_records("acme.com") == ("mx1.acme.com", "mx2.acme.com")


def test_mx_records_no_answer(mocker):
    _patch_resolve(mocker, {("nada.com", "MX"): dns.resolver.NoAnswer()})
    assert dns_check.mx_records("nada.com") == ()


def test_mx_records_nxdomain(mocker):
    _patch_resolve(mocker, {})
    assert dns_check.mx_records("none.example") == ()


def test_mx_records_timeout_raises(mocker):
    import dns.exception
    _patch_resolve(mocker, {("slow.com", "MX"): dns.exception.Timeout()})
    with pytest.raises(dns.exception.Timeout):
        dns_check.mx_records("slow.com")


def test_is_null_mx_true(mocker, fake_dns_answer):
    answer = fake_dns_answer([(0, ".")])
    _patch_resolve(mocker, {("null.com", "MX"): answer})
    assert dns_check.is_null_mx("null.com") is True


def test_is_null_mx_false_multiple_mx(mocker, fake_dns_answer):
    answer = fake_dns_answer([(10, "mx.acme.com."), (20, "mx2.acme.com.")])
    _patch_resolve(mocker, {("acme.com", "MX"): answer})
    assert dns_check.is_null_mx("acme.com") is False


def test_has_mail_mx_present(mocker, fake_dns_answer):
    answer = fake_dns_answer([(10, "mx.acme.com.")])
    _patch_resolve(mocker, {("acme.com", "MX"): answer})
    assert dns_check.has_mail("acme.com") is True


def test_has_mail_no_mx_but_a_record(mocker, fake_dns_answer):
    mapping = {
        ("noemail.com", "MX"): dns.resolver.NoAnswer(),
        ("noemail.com", "A"): fake_dns_answer([(0, "ignored")]),  # answer object truthy
    }
    _patch_resolve(mocker, mapping)
    assert dns_check.has_mail("noemail.com") is True


def test_has_mail_no_mx_no_a(mocker):
    mapping = {
        ("nothing.com", "MX"): dns.resolver.NoAnswer(),
        ("nothing.com", "A"): dns.resolver.NoAnswer(),
    }
    _patch_resolve(mocker, mapping)
    assert dns_check.has_mail("nothing.com") is False


def test_has_mail_null_mx_false(mocker, fake_dns_answer):
    answer = fake_dns_answer([(0, ".")])
    _patch_resolve(mocker, {("null.com", "MX"): answer})
    assert dns_check.has_mail("null.com") is False


def test_lru_cache_hits(mocker, fake_dns_answer):
    answer = fake_dns_answer([(10, "mx.acme.com.")])
    mock = _patch_resolve(mocker, {("acme.com", "MX"): answer})
    dns_check.mx_records("acme.com")
    dns_check.mx_records("acme.com")
    dns_check.mx_records("acme.com")
    assert mock.call_count == 1
