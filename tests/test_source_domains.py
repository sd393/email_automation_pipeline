"""Tests for scripts/source_domains.py (Stage 1, M1)."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.lib import dns_check
from scripts.lib.csv_schema import openai_strict_schema
from scripts.lib.llm import CostReport, ParseResult
from scripts.source_domains import (
    ALL_LLM_MODELS,
    DomainExtractionItem,
    DomainExtractionResponse,
    SearchQuery,
    SearchQueryResponse,
    _run,
    normalize_domain,
)


@dataclass
class FakeLLM:
    behaviors: list[Any] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def queue(self, parsed_or_exc, refused=False, refusal_text="", low=False, cost_usd=0.01):
        if isinstance(parsed_or_exc, Exception):
            self.behaviors.append(parsed_or_exc)
            return
        self.behaviors.append(
            ParseResult(
                parsed=parsed_or_exc,
                refused=refused,
                refusal_text=refusal_text,
                low_confidence=low,
                cost=CostReport(model="fake", usd=cost_usd),
            )
        )

    def cascade(self, messages, text_format, **kwargs):
        self.calls.append({"messages": messages, "text_format": text_format, **kwargs})
        if not self.behaviors:
            raise AssertionError("FakeLLM: no behaviors queued")
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b

    def parse(self, messages, text_format, **kwargs):
        return self.cascade(messages, text_format, **kwargs)


def _q(n):
    return SearchQueryResponse(queries=[SearchQuery(query=f"q{i}", sub_segment="s") for i in range(n)])


def _resp(items):
    return DomainExtractionResponse(retailers=items)


def _item(domain, **overrides):
    base = dict(
        company_name=overrides.pop("company_name", domain.replace(".com", "").title()),
        domain=domain,
        domain_inferred=False,
        is_excluded=False,
        exclude_reason=None,
        category="retail",
        source_url=f"https://list.example/{domain}",
        notes="",
    )
    base.update(overrides)
    return DomainExtractionItem(**base)


@pytest.fixture(autouse=True)
def _patch_dns(monkeypatch):
    """Default: every domain has mail. Tests override as needed."""
    dns_check.clear_cache()
    monkeypatch.setattr(dns_check, "has_mail", lambda d: True)
    monkeypatch.setattr(dns_check, "is_null_mx", lambda d: False)


@pytest.fixture
def small_brief(tmp_campaign_dir, sample_brief_yaml):
    """Write the sample brief in tmp_campaign_dir; return its path."""
    p = tmp_campaign_dir / "brief.yaml"
    p.write_text(sample_brief_yaml, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Strict-mode compliance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", ALL_LLM_MODELS, ids=lambda m: m.__name__)
def test_llm_response_models_strict_mode(model):
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
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_domain():
    assert normalize_domain("Https://Www.RetailerX.com/path?q=1") == "retailerx.com"
    assert normalize_domain("  https://shop.example.co.uk/  ") == "shop.example.co.uk"
    assert normalize_domain("not a url") is None
    assert normalize_domain("") is None
    assert normalize_domain(None) is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path(small_brief, tmp_campaign_dir):
    import yaml
    data = yaml.safe_load(small_brief.read_text())
    data["target"]["target_domain_count"] = 20
    small_brief.write_text(yaml.safe_dump(data), encoding="utf-8")

    llm = FakeLLM()
    llm.queue(_q(4))
    for i in range(4):
        items = [_item(f"r{i}-{j}.com") for j in range(5)]
        llm.queue(_resp(items))

    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    rows = (tmp_campaign_dir / "domains.csv").read_text().strip().splitlines()
    assert len(rows) == 21  # header + 20


def test_excluded_rows_dropped(small_brief, tmp_campaign_dir):
    llm = FakeLLM()
    llm.queue(_q(1))
    items = [
        _item("ok1.com"),
        _item("excluded.com", is_excluded=True, exclude_reason="too big"),
        _item("ok2.com"),
    ]
    llm.queue(_resp(items))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    text = (tmp_campaign_dir / "domains.csv").read_text()
    assert "ok1.com" in text
    assert "ok2.com" in text
    assert "excluded.com" not in text


def test_within_run_dedup(small_brief, tmp_campaign_dir):
    llm = FakeLLM()
    llm.queue(_q(3))
    for i in range(3):
        llm.queue(_resp([_item("dup.com"), _item(f"uniq{i}.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    text = (tmp_campaign_dir / "domains.csv").read_text()
    # Count occurrences in the domain column (second field), not anywhere
    domain_col = [line.split(",")[1] for line in text.strip().splitlines()[1:]]
    assert domain_col.count("dup.com") == 1
    assert sorted(domain_col) == ["dup.com", "uniq0.com", "uniq1.com", "uniq2.com"]


def test_cross_campaign_dedup_all_scope(small_brief, tmp_campaign_dir, tmp_path, monkeypatch):
    # Switch the brief's scope to all_campaigns and re-write it (must happen
    # BEFORE any run touches the campaign so brief_hash matches).
    data = yaml.safe_load(small_brief.read_text())
    data["safety"]["scope"] = "all_campaigns"
    small_brief.write_text(yaml.safe_dump(data), encoding="utf-8")

    from scripts.lib.dedup import Deduper
    data_dir = tmp_path / "shared-data"
    data_dir.mkdir()
    pre = Deduper(scope="all_campaigns", data_dir=data_dir)
    pre.load_global()
    pre.append_contact("a@huckberry.com", "huckberry.com", "A", "CEO", "old-campaign")
    monkeypatch.setattr("scripts.source_domains.Deduper", lambda scope: Deduper(scope=scope, data_dir=data_dir))

    llm = FakeLLM()
    llm.queue(_q(1))
    llm.queue(_resp([_item("huckberry.com"), _item("new.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    domains = [line.split(",")[1] for line in
               (tmp_campaign_dir / "domains.csv").read_text().strip().splitlines()[1:]]
    assert "huckberry.com" not in domains
    assert "new.com" in domains


def test_cross_campaign_dedup_this_scope(small_brief, tmp_campaign_dir, tmp_path, monkeypatch):
    # Brief default is `this_campaign` (per sample_brief_yaml) → cross-campaign known should not block.
    from scripts.lib.dedup import Deduper
    data_dir = tmp_path / "shared-data"
    data_dir.mkdir()
    pre = Deduper(scope="all_campaigns", data_dir=data_dir)
    pre.load_global()
    pre.append_contact("a@huckberry.com", "huckberry.com", "A", "CEO", "old-campaign")
    monkeypatch.setattr("scripts.source_domains.Deduper", lambda scope: Deduper(scope=scope, data_dir=data_dir))

    llm = FakeLLM()
    llm.queue(_q(1))
    llm.queue(_resp([_item("huckberry.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    domains = [line.split(",")[1] for line in
               (tmp_campaign_dir / "domains.csv").read_text().strip().splitlines()[1:]]
    assert "huckberry.com" in domains


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def test_dns_no_mail_drops_row(small_brief, tmp_campaign_dir, monkeypatch):
    monkeypatch.setattr(dns_check, "has_mail", lambda d: d != "noemail.com")
    llm = FakeLLM()
    llm.queue(_q(1))
    llm.queue(_resp([_item("noemail.com"), _item("ok.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    text = (tmp_campaign_dir / "domains.csv").read_text()
    assert "ok.com" in text
    assert "noemail.com" not in text


# ---------------------------------------------------------------------------
# LLM behavior
# ---------------------------------------------------------------------------

def test_refusal_marks_search_fail(small_brief, tmp_campaign_dir):
    llm = FakeLLM()
    llm.queue(_q(2))
    # First query refused (parsed=None, refused=True)
    llm.behaviors.append(ParseResult(
        parsed=None, refused=True, refusal_text="nope", low_confidence=False,
        cost=CostReport(model="fake"),
    ))
    llm.queue(_resp([_item("ok.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    text = (tmp_campaign_dir / "domains.csv").read_text()
    assert "ok.com" in text
    # Progress recorded search_fail
    progress = json.loads((tmp_campaign_dir / "progress" / "source_domains.json").read_text())
    assert any(v.get("status") == "search_fail" for v in progress.values())


def test_empty_cascade_marks_search_fail(small_brief, tmp_campaign_dir):
    llm = FakeLLM()
    llm.queue(_q(1))
    llm.behaviors.append(ParseResult(
        parsed=None, refused=False, refusal_text="", low_confidence=False,
        cost=CostReport(model="fake"),
    ))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    progress = json.loads((tmp_campaign_dir / "progress" / "source_domains.json").read_text())
    assert any(v.get("status") == "search_fail" for v in progress.values())


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def test_resume_skips_done_queries(small_brief, tmp_campaign_dir):
    # Run 1 — one query produces one row
    llm = FakeLLM()
    llm.queue(_q(2))
    llm.queue(_resp([_item("a.com")]))
    llm.queue(_resp([_item("b.com")]))
    rc1 = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc1 == 0
    text1 = (tmp_campaign_dir / "domains.csv").read_text()
    assert "a.com" in text1 and "b.com" in text1

    # Run 2 with --resume; if any query were re-processed, the LLM would have no behaviors.
    llm2 = FakeLLM()
    llm2.queue(_q(2))  # query generation still happens; queries already processed are skipped
    rc2 = _run(tmp_campaign_dir, resume=True, llm=llm2)
    assert rc2 == 0
    text2 = (tmp_campaign_dir / "domains.csv").read_text()
    assert text1 == text2


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------

def test_target_caps_output(small_brief, tmp_campaign_dir):
    data = yaml.safe_load(small_brief.read_text())
    data["target"]["target_domain_count"] = 5
    small_brief.write_text(yaml.safe_dump(data), encoding="utf-8")

    llm = FakeLLM()
    llm.queue(_q(5))
    # First query returns 10 — but the cap is 5
    llm.queue(_resp([_item(f"d{i}.com") for i in range(10)]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    rows = (tmp_campaign_dir / "domains.csv").read_text().strip().splitlines()
    assert len(rows) == 6  # header + 5


def test_target_undermet_still_exits_0(small_brief, tmp_campaign_dir):
    data = yaml.safe_load(small_brief.read_text())
    data["target"]["target_domain_count"] = 100
    small_brief.write_text(yaml.safe_dump(data), encoding="utf-8")

    llm = FakeLLM()
    llm.queue(_q(2))
    llm.queue(_resp([_item("a.com")]))
    llm.queue(_resp([_item("b.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    status_text = (tmp_campaign_dir / "status.md").read_text()
    assert "COMPLETED" in status_text


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def test_missing_brief_exits_3(tmp_campaign_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_campaign_dir, resume=False, llm=FakeLLM())
    assert exc.value.code == 3
    err = capsys.readouterr().err.strip()
    payload = json.loads(err.splitlines()[-1])
    assert payload["error"] == "BriefValidationError"


def test_invalid_brief_exits_3(tmp_campaign_dir, sample_brief_yaml, capsys):
    data = yaml.safe_load(sample_brief_yaml)
    data["who_to_contact"]["priority_roles"] = []
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _run(tmp_campaign_dir, resume=False, llm=FakeLLM())
    assert exc.value.code == 3


def test_brief_hash_mismatch_exits_2(small_brief, tmp_campaign_dir, capsys):
    llm = FakeLLM()
    llm.queue(_q(1))
    llm.queue(_resp([_item("a.com")]))
    rc = _run(tmp_campaign_dir, resume=False, llm=llm)
    assert rc == 0
    # mutate brief
    data = yaml.safe_load(small_brief.read_text())
    data["target"]["segment"] = "Different segment"
    small_brief.write_text(yaml.safe_dump(data), encoding="utf-8")
    rc2 = _run(tmp_campaign_dir, resume=False, llm=FakeLLM())
    assert rc2 == 2
    err = capsys.readouterr().err
    assert "Brief changed since previous stage" in err


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_no_thread_pool_executor_referenced():
    src = Path("scripts/source_domains.py").read_text()
    assert "ThreadPoolExecutor" not in src
    assert "ProcessPoolExecutor" not in src
