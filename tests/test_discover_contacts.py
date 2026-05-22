"""Tests for scripts/discover_contacts.py (Stage 2, M2 first half)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.lib import dns_check
from scripts.lib.csv_schema import DomainRow, read_csv, write_csv_row
from scripts.lib.llm import CostReport, ParseResult
from scripts.discover_contacts import (
    ALL_LLM_MODELS,
    DiscoveryPerson,
    DiscoveryResponse,
    _run,
)


@dataclass
class FakeLLM:
    """Per-domain canned responses. Use ``respond_for(domain, ...)`` to register."""
    by_domain: dict[str, Any] = field(default_factory=dict)
    default: Any = None
    calls: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def respond_for(self, domain, parsed=None, refused=False, exc=None, cost=0.01):
        if exc is not None:
            self.by_domain[domain] = exc
        else:
            self.by_domain[domain] = ParseResult(
                parsed=parsed,
                refused=refused,
                refusal_text="" if not refused else "refused",
                low_confidence=False,
                cost=CostReport(model="fake", usd=cost),
            )

    def cascade(self, messages, text_format, **kwargs):
        # Pull domain from the user message
        user_msg = next(m["content"] for m in messages if m["role"] == "user")
        domain = next(line.split(": ", 1)[1] for line in user_msg.splitlines() if line.startswith("Domain:"))
        with self._lock:
            self.calls.append({"domain": domain})
        b = self.by_domain.get(domain, self.default)
        if b is None:
            raise AssertionError(f"FakeLLM: no response queued for {domain}")
        if isinstance(b, Exception):
            raise b
        return b

    def parse(self, *a, **kw):
        return self.cascade(*a, **kw)


def _people(n):
    return [
        DiscoveryPerson(
            name=f"P{i}", role="Founder",
            leverage_rationale="founder = decider",
            email_if_known=f"p{i}@x.com" if i % 2 == 0 else None,
            email_source_url=f"https://x.com/team#{i}" if i % 2 == 0 else None,
            confidence=0.9,
        )
        for i in range(n)
    ]


def _setup_campaign(tmp_campaign_dir, sample_brief_yaml, domain_rows):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    domains_csv = tmp_campaign_dir / "domains.csv"
    for row in domain_rows:
        write_csv_row(domains_csv, row)


def _drow(domain, name=None):
    return DomainRow(
        company_name=name or domain.replace(".com", "").title(),
        domain=domain,
        domain_inferred=False,
        category="retail",
        source_url=f"https://list.example/{domain}",
        notes="",
    )


@pytest.fixture(autouse=True)
def _patch_dns(monkeypatch):
    dns_check.clear_cache()
    monkeypatch.setattr(dns_check, "has_mail", lambda d: True)


# ---------------------------------------------------------------------------
# Strict-mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", ALL_LLM_MODELS, ids=lambda m: m.__name__)
def test_strict_mode(model):
    from scripts.lib.csv_schema import openai_strict_schema
    schema = openai_strict_schema(model)

    def walk(s):
        if isinstance(s, dict):
            if s.get("type") == "object" or "properties" in s:
                assert s.get("additionalProperties") is False
                assert set(s.get("required", [])) == set(s.get("properties", {}).keys())
            for v in s.values():
                walk(v)

    walk(schema)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_three_domains(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow(f"x{i}.com") for i in range(3)])
    llm = FakeLLM()
    for i in range(3):
        llm.respond_for(f"x{i}.com", parsed=DiscoveryResponse(corrected_domain=None, people=_people(3)))
    rc = _run(tmp_campaign_dir, resume=False, workers=2, llm=llm)
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "contacts.csv", __import__("scripts.lib.csv_schema").lib.csv_schema.ContactRow)
    assert len(rows) == 9


# ---------------------------------------------------------------------------
# LLM behavior
# ---------------------------------------------------------------------------

def test_refusal_marks_discovery_fail(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("x0.com"), _drow("x1.com")])
    llm = FakeLLM()
    llm.respond_for("x0.com", refused=True)
    llm.respond_for("x1.com", parsed=DiscoveryResponse(people=_people(2)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "discover_contacts.json").read_text())
    assert progress["x0.com"]["status"] == "discovery_fail"
    assert progress["x1.com"]["status"] == "ok"


def test_empty_people_marks_no_people(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("x0.com")])
    llm = FakeLLM()
    llm.respond_for("x0.com", parsed=DiscoveryResponse(people=[]))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "discover_contacts.json").read_text())
    assert progress["x0.com"]["status"] == "no_people"


def test_corrected_domain_used(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("huckberry.co")])
    llm = FakeLLM()
    llm.respond_for("huckberry.co",
                    parsed=DiscoveryResponse(corrected_domain="huckberry.com", people=_people(2)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0
    from scripts.lib.csv_schema import ContactRow
    rows = read_csv(tmp_campaign_dir / "contacts.csv", ContactRow)
    assert all(r.domain == "huckberry.com" for r in rows)


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def test_dns_no_mail_marks_dns_fail(tmp_campaign_dir, sample_brief_yaml, monkeypatch):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("good.com"), _drow("bad.com")])
    monkeypatch.setattr(dns_check, "has_mail", lambda d: d != "bad.com")
    llm = FakeLLM()
    llm.respond_for("good.com", parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "discover_contacts.json").read_text())
    assert progress["bad.com"]["status"] == "dns_fail"
    assert progress["good.com"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Concurrency (queue-based writer)
# ---------------------------------------------------------------------------

def test_worker_exc_isolated(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml,
                    [_drow("ok.com"), _drow("boom.com"), _drow("ok2.com")])
    llm = FakeLLM()
    llm.respond_for("ok.com", parsed=DiscoveryResponse(people=_people(1)))
    llm.respond_for("boom.com", exc=RuntimeError("worker crashed"))
    llm.respond_for("ok2.com", parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=2, llm=llm)
    assert rc == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "discover_contacts.json").read_text())
    assert progress["boom.com"]["status"] == "worker_exc"
    assert progress["boom.com"]["exception_type"] == "RuntimeError"
    assert progress["ok.com"]["status"] == "ok"
    assert progress["ok2.com"]["status"] == "ok"


def test_worker_exc_retriable_on_resume(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("flaky.com")])
    llm1 = FakeLLM()
    llm1.respond_for("flaky.com", exc=RuntimeError("transient"))
    rc1 = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm1)
    assert rc1 == 0
    llm2 = FakeLLM()
    llm2.respond_for("flaky.com", parsed=DiscoveryResponse(people=_people(1)))
    rc2 = _run(tmp_campaign_dir, resume=True, workers=1, llm=llm2)
    assert rc2 == 0
    from scripts.lib.csv_schema import ContactRow
    rows = read_csv(tmp_campaign_dir / "contacts.csv", ContactRow)
    assert len(rows) == 1


def test_concurrent_write_no_dupes(tmp_campaign_dir, sample_brief_yaml):
    domains = [_drow(f"d{i:02d}.com") for i in range(50)]
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, domains)
    llm = FakeLLM()
    for d in domains:
        llm.respond_for(d.domain, parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=5, llm=llm)
    assert rc == 0
    from scripts.lib.csv_schema import ContactRow
    rows = read_csv(tmp_campaign_dir / "contacts.csv", ContactRow)
    assert len(rows) == 50
    # No row loss, no duplicate domain
    domains_in_csv = sorted({r.domain for r in rows})
    assert domains_in_csv == sorted({d.domain for d in domains})


# ---------------------------------------------------------------------------
# Halt: auth errors
# ---------------------------------------------------------------------------

class FakeAuthError(Exception):
    """type(e).__name__ matches HALT_EXCEPTION_NAMES."""


FakeAuthError.__name__ = "AuthenticationError"


def test_auth_error_halts_stage(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("a.com"), _drow("b.com")])
    llm = FakeLLM()
    llm.respond_for("a.com", exc=FakeAuthError("401"))
    llm.respond_for("b.com", parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 2


# ---------------------------------------------------------------------------
# Failure budget
# ---------------------------------------------------------------------------

def test_failure_budget_halts_above_threshold(tmp_campaign_dir, sample_brief_yaml):
    domains = [_drow(f"d{i:02d}.com") for i in range(40)]
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, domains)
    llm = FakeLLM()
    # First 25 worker_exc, rest ok — we want >20% failure once n_processed > 20
    for i, d in enumerate(domains):
        if i < 25:
            llm.respond_for(d.domain, exc=RuntimeError("boom"))
        else:
            llm.respond_for(d.domain, parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 2


def test_failure_budget_does_not_halt_low_n(tmp_campaign_dir, sample_brief_yaml):
    domains = [_drow(f"d{i}.com") for i in range(10)]
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, domains)
    llm = FakeLLM()
    for i, d in enumerate(domains):
        if i < 3:
            llm.respond_for(d.domain, exc=RuntimeError("boom"))
        else:
            llm.respond_for(d.domain, parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0  # 30% failure but n_processed < 20 → no halt


# ---------------------------------------------------------------------------
# Per-company cap
# ---------------------------------------------------------------------------

def test_contacts_per_company_cap(tmp_campaign_dir, sample_brief_yaml):
    # Brief default is 3
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("big.com")])
    llm = FakeLLM()
    llm.respond_for("big.com", parsed=DiscoveryResponse(people=_people(7)))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0
    from scripts.lib.csv_schema import ContactRow
    rows = read_csv(tmp_campaign_dir / "contacts.csv", ContactRow)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def test_missing_domains_csv(tmp_campaign_dir, sample_brief_yaml, capsys):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=FakeLLM())
    assert rc == 2
    assert "No domains" in capsys.readouterr().err


def test_missing_brief_exits_3(tmp_campaign_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_campaign_dir, resume=False, workers=1, llm=FakeLLM())
    assert exc.value.code == 3


def test_brief_hash_mismatch(tmp_campaign_dir, sample_brief_yaml, capsys):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("a.com")])
    llm = FakeLLM()
    llm.respond_for("a.com", parsed=DiscoveryResponse(people=_people(1)))
    rc1 = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc1 == 0
    # Mutate brief
    data = yaml.safe_load((tmp_campaign_dir / "brief.yaml").read_text())
    data["target"]["segment"] = "Mutated"
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rc2 = _run(tmp_campaign_dir, resume=False, workers=1, llm=FakeLLM())
    assert rc2 == 2
    assert "Brief changed since previous stage" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Observability cadence
# ---------------------------------------------------------------------------

def test_milestone_cadence(tmp_campaign_dir, sample_brief_yaml, capsys):
    domains = [_drow(f"d{i:02d}.com") for i in range(60)]
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, domains)
    llm = FakeLLM()
    for d in domains:
        llm.respond_for(d.domain, parsed=DiscoveryResponse(people=_people(1)))
    rc = _run(tmp_campaign_dir, resume=False, workers=2, llm=llm)
    assert rc == 0
    activity = (tmp_campaign_dir / "activity.log").read_text()
    assert sum(1 for line in activity.splitlines() if "milestone:" in line) >= 2


# ---------------------------------------------------------------------------
# email passthrough
# ---------------------------------------------------------------------------

def test_email_if_known_preserved(tmp_campaign_dir, sample_brief_yaml):
    _setup_campaign(tmp_campaign_dir, sample_brief_yaml, [_drow("x.com")])
    llm = FakeLLM()
    people = [
        DiscoveryPerson(
            name="A", role="CEO", leverage_rationale="r",
            email_if_known="a@x.com", email_source_url="https://x.com/team",
            confidence=0.9,
        ),
        DiscoveryPerson(
            name="B", role="CTO", leverage_rationale="r",
            email_if_known=None, email_source_url=None, confidence=0.9,
        ),
    ]
    llm.respond_for("x.com", parsed=DiscoveryResponse(people=people))
    rc = _run(tmp_campaign_dir, resume=False, workers=1, llm=llm)
    assert rc == 0
    from scripts.lib.csv_schema import ContactRow
    rows = sorted(read_csv(tmp_campaign_dir / "contacts.csv", ContactRow), key=lambda r: r.name)
    assert rows[0].email_if_known == "a@x.com"
    assert rows[1].email_if_known is None
