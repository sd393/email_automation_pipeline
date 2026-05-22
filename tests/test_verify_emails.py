"""Tests for scripts/verify_emails.py (Stage 3, closes M2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.lib.csv_schema import ContactRow, DomainRow, EmailRow, read_csv, write_csv_row
from scripts.lib.verifiers.base import VerificationResult, VerifierUnavailable
from scripts.verify_emails import _run


@dataclass
class FakeVerifier:
    name: str
    by_email: dict[str, VerificationResult] = field(default_factory=dict)
    default: VerificationResult | None = None
    available: bool = True
    raise_on_verify: Exception | None = None
    call_count: int = 0
    calls: list[str] = field(default_factory=list)

    def assert_available(self):
        if not self.available:
            raise VerifierUnavailable(f"{self.name} unavailable: test setup")

    def verify(self, email, *, citation_url):
        self.call_count += 1
        self.calls.append(email)
        if self.raise_on_verify is not None:
            raise self.raise_on_verify
        if email in self.by_email:
            return self.by_email[email]
        if self.default is not None:
            return self.default
        return VerificationResult(status="unknown", confidence="", source_url="", notes="default")


def _accepted(conf, url="https://verified-smtp/"):
    return VerificationResult(status="accepted", confidence=conf, source_url=url, notes="")


def _rejected():
    return VerificationResult(status="rejected", confidence="", source_url="", notes="rejected")


def _unknown():
    return VerificationResult(status="unknown", confidence="", source_url="", notes="unknown")


def _catchall():
    return VerificationResult(status="catchall", confidence="", source_url="", notes="catchall")


def _contact(email, domain="acme.com", name="Jane", role="Founder"):
    return ContactRow(
        company_name=domain.split(".")[0].title(),
        domain=domain,
        name=name,
        role=role,
        leverage_rationale="r",
        email_if_known=email,
        email_source_url=f"https://{domain}/team" if email else None,
        confidence=0.9,
    )


def _fast_brief(sample_brief_yaml):
    """Same brief but with verifier rate limits set high so tests don't sleep."""
    data = yaml.safe_load(sample_brief_yaml)
    data["verifier"]["rate_per_sec"] = 1000.0
    data["verifier"]["per_hour_cap"] = 100000
    data["verifier"]["burst"] = 1000
    return yaml.safe_dump(data)


def _setup(tmp_campaign_dir, sample_brief_yaml, contacts, domains_for_categories=None):
    (tmp_campaign_dir / "brief.yaml").write_text(_fast_brief(sample_brief_yaml), encoding="utf-8")
    for c in contacts:
        write_csv_row(tmp_campaign_dir / "contacts.csv", c)
    if domains_for_categories:
        for d in domains_for_categories:
            write_csv_row(tmp_campaign_dir / "domains.csv", d)


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

def test_chain_walks_in_order(tmp_campaign_dir, sample_brief_yaml):
    contacts = [
        _contact("c1@acme.com", "acme.com", name="C1"),
        _contact("c2@beta.com", "beta.com", name="C2"),
        _contact("c3@gamma.com", "gamma.com", name="C3"),
    ]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)
    smtp = FakeVerifier("smtp_probe", by_email={
        "c1@acme.com": _accepted("verified-smtp"),
        "c2@beta.com": _catchall(),
        "c3@gamma.com": _rejected(),
    })
    web = FakeVerifier("web_citation", by_email={
        "c2@beta.com": _accepted("verified-web", url="https://beta.com/team"),
        "c3@gamma.com": _unknown(),
    })
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp, web])
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    by_email = {r.email: r for r in rows}
    assert "c1@acme.com" in by_email
    assert by_email["c1@acme.com"].confidence == "verified-smtp"
    assert "c2@beta.com" in by_email
    assert by_email["c2@beta.com"].confidence == "verified-web"
    assert by_email["c2@beta.com"].source_url == "https://beta.com/team"
    assert "c3@gamma.com" not in by_email


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def test_missing_contacts_exits_2(tmp_campaign_dir, sample_brief_yaml, capsys):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[FakeVerifier("smtp_probe")])
    assert rc == 2
    assert "No contacts" in capsys.readouterr().err


def test_verifier_unavailable_exits_2(tmp_campaign_dir, sample_brief_yaml, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, [_contact("a@x.com")])
    bad = FakeVerifier("smtp_probe", available=False)
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[bad])
    assert rc == 2
    err = capsys.readouterr().err
    assert "smtp_probe unavailable" in err
    assert not (tmp_campaign_dir / "emails.csv").exists()


def test_brief_hash_mismatch(tmp_campaign_dir, sample_brief_yaml, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, [_contact("a@x.com")])
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc1 = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp])
    assert rc1 == 0
    data = yaml.safe_load(sample_brief_yaml)
    data["target"]["segment"] = "Mutated"
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rc2 = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp])
    assert rc2 == 2
    assert "Brief changed" in capsys.readouterr().err


def test_brief_invalid_exits_3(tmp_campaign_dir, sample_brief_yaml, capsys):
    data = yaml.safe_load(sample_brief_yaml)
    data["who_to_contact"]["priority_roles"] = []
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[FakeVerifier("x")])
    assert exc.value.code == 3


# ---------------------------------------------------------------------------
# Pattern-only / suppression / cap
# ---------------------------------------------------------------------------

def test_pattern_only_skipped(tmp_campaign_dir, sample_brief_yaml):
    contact = _contact(None, "x.com", name="P")
    _setup(tmp_campaign_dir, sample_brief_yaml, [contact])
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp])
    assert rc == 0
    assert smtp.call_count == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "verify_emails.json").read_text())
    assert any(v["status"] == "pattern_only_skipped" for v in progress.values())
    assert not (tmp_campaign_dir / "emails.csv").exists()


def test_company_cap(tmp_campaign_dir, sample_brief_yaml):
    # Brief default contacts_per_company=3
    contacts = [_contact(f"c{i}@same.com", "same.com", name=f"P{i}") for i in range(5)]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp])
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert len(rows) == 3
    # 2 contacts skipped under the cap
    progress = json.loads((tmp_campaign_dir / "progress" / "verify_emails.json").read_text())
    cap_skipped = sum(1 for v in progress.values() if v["status"] == "company_cap_reached")
    assert cap_skipped == 2


def test_suppression_gate(tmp_campaign_dir, sample_brief_yaml, tmp_path):
    contacts = [_contact("blocked@x.com", "x.com"), _contact("ok@x.com", "x.com")]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)
    # Seed suppression
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    from scripts.lib.dedup import Deduper
    d = Deduper(scope="all_campaigns", data_dir=data_dir)
    d.load_global()
    d.append_suppressed("blocked@x.com", "hard_bounce", "msg-1")
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp], data_dir=data_dir)
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert {r.email for r in rows} == {"ok@x.com"}


# ---------------------------------------------------------------------------
# Chain ordering
# ---------------------------------------------------------------------------

def test_chain_order_matters(tmp_campaign_dir, sample_brief_yaml):
    contacts = [_contact("a@b.com")]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    web = FakeVerifier("web_citation", default=_accepted("verified-web"))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[web, smtp])
    assert rc == 0
    assert web.call_count == 1
    assert smtp.call_count == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert rows[0].confidence == "verified-web"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def test_resume_skips_verified(tmp_campaign_dir, sample_brief_yaml):
    contacts = [_contact("a@x.com"), _contact("b@x.com", "x.com", name="B")]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc1 = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp])
    assert rc1 == 0
    rows1 = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    # second run: should NOT re-call the verifier for already-verified rows
    smtp2 = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc2 = _run(tmp_campaign_dir, resume=True, workers=1, verifier_chain=[smtp2])
    assert rc2 == 0
    assert smtp2.call_count == 0
    rows2 = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert {r.email for r in rows1} == {r.email for r in rows2}


def test_verifier_exc_retriable_on_resume(tmp_campaign_dir, sample_brief_yaml):
    contacts = [_contact("a@x.com")]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)
    smtp1 = FakeVerifier("smtp_probe", raise_on_verify=RuntimeError("boom"))
    rc1 = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp1])
    assert rc1 == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "verify_emails.json").read_text())
    assert any(v["status"] == "verifier_exc" for v in progress.values())
    smtp2 = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc2 = _run(tmp_campaign_dir, resume=True, workers=1, verifier_chain=[smtp2])
    assert rc2 == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert rows[0].email == "a@x.com"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_50_concurrent_no_loss(tmp_campaign_dir, sample_brief_yaml):
    # Bump rate limits so the test doesn't take real wall-clock seconds.
    data = yaml.safe_load(sample_brief_yaml)
    data["verifier"]["rate_per_sec"] = 1000.0
    data["verifier"]["per_hour_cap"] = 10000
    data["verifier"]["burst"] = 100
    yaml_text = yaml.safe_dump(data)
    contacts = [_contact(f"c{i}@d{i:03d}.com", f"d{i:03d}.com", name=f"P{i}") for i in range(50)]
    _setup(tmp_campaign_dir, yaml_text, contacts)
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc = _run(tmp_campaign_dir, resume=False, workers=5, verifier_chain=[smtp])
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert len(rows) == 50
    assert len({r.email for r in rows}) == 50


# ---------------------------------------------------------------------------
# Failure budget
# ---------------------------------------------------------------------------

def test_failure_budget_halts(tmp_campaign_dir, sample_brief_yaml):
    contacts = [_contact(f"c{i}@d{i:03d}.com", f"d{i:03d}.com", name=f"P{i}") for i in range(40)]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts)

    # Set up a verifier that raises for the first 25 emails.
    raising_emails = {f"c{i}@d{i:03d}.com" for i in range(25)}

    class Failer(FakeVerifier):
        def verify(self, email, *, citation_url):
            self.call_count += 1
            if email in raising_emails:
                raise RuntimeError("boom")
            return _accepted("verified-smtp")

    rc = _run(tmp_campaign_dir, resume=False, workers=1,
              verifier_chain=[Failer("smtp_probe", default=_accepted("verified-smtp"))])
    assert rc == 2


# ---------------------------------------------------------------------------
# EmailRow fields
# ---------------------------------------------------------------------------

def test_category_lookup_from_domains_csv(tmp_campaign_dir, sample_brief_yaml):
    contacts = [_contact("a@x.com", "x.com")]
    _setup(tmp_campaign_dir, sample_brief_yaml, contacts, domains_for_categories=[
        DomainRow(
            company_name="Xco", domain="x.com", domain_inferred=False,
            category="premium-retail", source_url="https://list/x", notes="",
        ),
    ])
    smtp = FakeVerifier("smtp_probe", default=_accepted("verified-smtp"))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, verifier_chain=[smtp])
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "emails.csv", EmailRow)
    assert rows[0].category == "premium-retail"


def test_verifier_disabled_in_config(tmp_campaign_dir, sample_brief_yaml, monkeypatch, capsys):
    """If the brief asks for a verifier but verifiers.yaml has enabled=false → exit 2."""
    data = yaml.safe_load(sample_brief_yaml)
    data["verifier"]["chain"] = ["api_provider"]
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    write_csv_row(tmp_campaign_dir / "contacts.csv", _contact("a@x.com"))
    # config/verifiers.yaml in the repo has api_provider.enabled=false (the default).
    rc = _run(tmp_campaign_dir, resume=False, workers=1)  # no verifier_chain override
    assert rc == 2
    err = capsys.readouterr().err
    assert "api_provider" in err
    assert "disabled" in err
