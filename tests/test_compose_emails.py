"""Tests for scripts/compose_emails.py (Stage 4)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from scripts.lib.csv_schema import EmailRow, OutboxRow, read_csv, write_csv_row
from scripts.lib.first_name import FirstNameResult
from scripts.compose_emails import _run


@dataclass
class FakeCost:
    usd: float = 0.0001


@dataclass
class FakeParse:
    parsed: object | None
    refused: bool = False
    refusal_text: str = ""
    low_confidence: bool = False
    cost: FakeCost = field(default_factory=FakeCost)


class FakeLLM:
    def __init__(self, name_map=None):
        self.name_map = name_map or {}
        self.call_count = 0
        self.calls: list[str] = []

    def parse(self, messages, text_format, **kwargs):
        self.call_count += 1
        user = next(m["content"] for m in messages if m["role"] == "user")
        self.calls.append(user)
        first = self.name_map.get(user, user.split()[0])
        return FakeParse(parsed=FirstNameResult(first_name=first))


def _email(name, email, company="Acme", domain="acme.com", role="Founder"):
    return EmailRow(
        name=name, email=email, company=company, domain=domain, role=role,
        category="retail", confidence="verified-smtp",
        source_url="https://verified-smtp/", leverage_rationale="r",
    )


def _setup(tmp_campaign_dir, sample_brief_yaml, emails, template_text=None):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    if template_text is not None:
        # The template referenced by sample_brief_yaml is in tmp_campaign_dir already.
        # Find its path from the brief and overwrite.
        data = yaml.safe_load(sample_brief_yaml)
        Path(data["message"]["template"]).write_text(template_text, encoding="utf-8")
    for e in emails:
        write_csv_row(tmp_campaign_dir / "emails.csv", e)


GOOD_TEMPLATE = (
    "Subject: Quick question, {{first_name}}\n"
    "\n"
    "Hi {{first_name}}, saw your work at {{company}}.\n"
    "\n"
    "{{value_prop}}\n"
    "\n"
    "— {{from_name}}\n"
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path(tmp_campaign_dir, sample_brief_yaml):
    emails = [
        _email("Robert Smith", "r@x.com", company="X", domain="x.com"),
        _email("Jane Doe", "j@y.com", company="Y", domain="y.com"),
        _email("Andy Andrews", "a@z.com", company="Z", domain="z.com"),
    ]
    _setup(tmp_campaign_dir, sample_brief_yaml, emails, template_text=GOOD_TEMPLATE)
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)
    assert len(rows) == 3
    by_email = {r.to_email: r for r in rows}
    assert by_email["r@x.com"].first_name_used == "Robert"
    assert by_email["j@y.com"].subject == "Quick question, Jane"
    assert "Hi Jane" in by_email["j@y.com"].body_plain


# ---------------------------------------------------------------------------
# Subject extraction
# ---------------------------------------------------------------------------

def test_no_subject_prefix_first_line_is_subject(tmp_campaign_dir, sample_brief_yaml):
    _setup(tmp_campaign_dir, sample_brief_yaml,
           [_email("Jane", "j@x.com")],
           template_text="Quick note\n\nBody text here.")
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)
    assert rows[0].subject == "Quick note"
    assert "Body text here." in rows[0].body_plain


# ---------------------------------------------------------------------------
# First-name integration
# ---------------------------------------------------------------------------

def test_dr_robert_smith_stripped(tmp_campaign_dir, sample_brief_yaml):
    _setup(tmp_campaign_dir, sample_brief_yaml,
           [_email("Dr. Robert Smith", "r@x.com")],
           template_text=GOOD_TEMPLATE)
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)
    assert rows[0].first_name_used == "Robert"


def test_marie_claire_personalize_true(tmp_campaign_dir, sample_brief_yaml):
    _setup(tmp_campaign_dir, sample_brief_yaml,
           [_email("Marie-Claire Dupont", "m@x.com")],
           template_text=GOOD_TEMPLATE)
    llm = FakeLLM(name_map={"Marie-Claire Dupont": "Marie-Claire"})
    rc = _run(tmp_campaign_dir, resume=False, llm_client=llm)
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)
    assert rows[0].first_name_used == "Marie-Claire"
    assert llm.call_count == 1


def test_personalize_false_uses_naive(tmp_campaign_dir, sample_brief_yaml):
    data = yaml.safe_load(sample_brief_yaml)
    data["message"]["personalize_first_name"] = False
    yaml_text = yaml.safe_dump(data)
    _setup(tmp_campaign_dir, yaml_text,
           [_email("Marie-Claire Dupont", "m@x.com")],
           template_text=GOOD_TEMPLATE)
    llm = FakeLLM()
    rc = _run(tmp_campaign_dir, resume=False, llm_client=llm)
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)
    assert rows[0].first_name_used == "Marie-Claire"  # naive split
    assert llm.call_count == 0


# ---------------------------------------------------------------------------
# Cache integration
# ---------------------------------------------------------------------------

def test_cache_means_one_llm_call_for_duplicate_names(tmp_campaign_dir, sample_brief_yaml):
    _setup(tmp_campaign_dir, sample_brief_yaml,
           [_email("Marie-Claire Dupont", "m1@x.com"),
            _email("Marie-Claire Dupont", "m2@y.com", company="Y", domain="y.com")],
           template_text=GOOD_TEMPLATE)
    llm = FakeLLM(name_map={"Marie-Claire Dupont": "Marie-Claire"})
    rc = _run(tmp_campaign_dir, resume=False, llm_client=llm)
    assert rc == 0
    rows = read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)
    assert all(r.first_name_used == "Marie-Claire" for r in rows)
    assert llm.call_count == 1


def test_resume_does_not_recall_llm(tmp_campaign_dir, sample_brief_yaml):
    _setup(tmp_campaign_dir, sample_brief_yaml,
           [_email("Marie-Claire Dupont", "m1@x.com")],
           template_text=GOOD_TEMPLATE)
    llm1 = FakeLLM(name_map={"Marie-Claire Dupont": "Marie-Claire"})
    rc1 = _run(tmp_campaign_dir, resume=False, llm_client=llm1)
    assert rc1 == 0
    # Re-run with --resume; the row is already composed, no LLM call needed.
    llm2 = FakeLLM(name_map={"Marie-Claire Dupont": "Marie-Claire"})
    rc2 = _run(tmp_campaign_dir, resume=True, llm_client=llm2)
    assert rc2 == 0
    assert llm2.call_count == 0


# ---------------------------------------------------------------------------
# Lints (warnings)
# ---------------------------------------------------------------------------

def test_lint_all_caps_subject(tmp_campaign_dir, sample_brief_yaml):
    template = "Subject: OFFER INSIDE!!!\n\nHello {{first_name}}.\n\nBody."
    _setup(tmp_campaign_dir, sample_brief_yaml, [_email("Jane", "j@x.com")], template_text=template)
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 0
    activity = (tmp_campaign_dir / "activity.log").read_text()
    assert "lint: subject is all caps" in activity
    assert len(read_csv(tmp_campaign_dir / "outbox.csv", OutboxRow)) == 1


def test_lint_url_shortener(tmp_campaign_dir, sample_brief_yaml):
    template = "Subject: Hi {{first_name}}\n\nCheck bit.ly/foo for more.\n\n"
    _setup(tmp_campaign_dir, sample_brief_yaml, [_email("Jane", "j@x.com")], template_text=template)
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 0
    assert "URL shortener" in (tmp_campaign_dir / "activity.log").read_text()


def test_lint_no_newlines(tmp_campaign_dir, sample_brief_yaml):
    template = "Subject: Hi {{first_name}}\n\nOne line only."
    _setup(tmp_campaign_dir, sample_brief_yaml, [_email("Jane", "j@x.com")], template_text=template)
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 0
    assert "paragraph breaks" in (tmp_campaign_dir / "activity.log").read_text()


# ---------------------------------------------------------------------------
# Template errors
# ---------------------------------------------------------------------------

def test_missing_template_file_exits_3(tmp_campaign_dir, sample_brief_yaml, capsys):
    """Brief schema validates template existence at load; missing file → exit 3."""
    data = yaml.safe_load(sample_brief_yaml)
    Path(data["message"]["template"]).unlink()  # remove the template file
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    write_csv_row(tmp_campaign_dir / "emails.csv", _email("Jane", "j@x.com"))
    with pytest.raises(SystemExit) as exc:
        _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert exc.value.code == 3
    err = capsys.readouterr().err
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["error"] == "BriefValidationError"
    assert "template" in payload["field"]


def test_unknown_slot_exits_2(tmp_campaign_dir, sample_brief_yaml, capsys):
    template = "Subject: Hi {{first_name}}\n\n{{nonexistent}}"
    _setup(tmp_campaign_dir, sample_brief_yaml, [_email("Jane", "j@x.com")], template_text=template)
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 2
    assert "nonexistent" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def test_missing_emails_csv(tmp_campaign_dir, sample_brief_yaml, capsys):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc == 2
    assert "verify_emails.py" in capsys.readouterr().err


def test_brief_hash_mismatch(tmp_campaign_dir, sample_brief_yaml, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, [_email("Jane", "j@x.com")], template_text=GOOD_TEMPLATE)
    rc1 = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc1 == 0
    data = yaml.safe_load((tmp_campaign_dir / "brief.yaml").read_text())
    data["target"]["segment"] = "Mutated"
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rc2 = _run(tmp_campaign_dir, resume=False, llm_client=FakeLLM())
    assert rc2 == 2
    assert "Brief changed" in capsys.readouterr().err
