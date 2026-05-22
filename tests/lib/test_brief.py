"""Tests for scripts.lib.brief."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.lib.brief import Brief, BriefValidationError, load


def _write_brief(tmp: Path, yaml_text: str) -> Path:
    p = tmp / "brief.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return p


def _mutate(yaml_text: str, mutator) -> str:
    data = yaml.safe_load(yaml_text)
    mutator(data)
    return yaml.safe_dump(data)


def test_load_valid_brief(sample_brief: Brief):
    assert sample_brief.slug == "test-campaign"
    assert sample_brief.target.target_domain_count == 20
    assert sample_brief.sending.send_rate_per_day == 100
    assert sample_brief.message.value_prop


def test_missing_target_segment(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d["target"].pop("segment"))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert exc.value.field.startswith("target.segment")


def test_empty_priority_roles(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d["who_to_contact"].__setitem__("priority_roles", []))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert "who_to_contact.priority_roles" in exc.value.field


def test_send_rate_above_cap(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d["sending"].__setitem__("send_rate_per_day", 5000))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert "send_rate_per_day" in exc.value.field


def test_slug_not_kebab(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d.__setitem__("slug", "Foo Bar"))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert exc.value.field == "slug"


def test_unknown_top_level_field(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d.__setitem__("bogus", "field"))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert "bogus" in exc.value.field or "bogus" in exc.value.message


def test_template_missing(tmp_campaign_dir, sample_brief_yaml):
    missing = tmp_campaign_dir / "no_such_template.md"
    bad = _mutate(sample_brief_yaml, lambda d: d["message"].__setitem__("template", str(missing)))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert "template" in exc.value.field
    assert str(missing) in exc.value.message


def test_bad_email_shape(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d["message"].__setitem__("from_gmail", "not-an-email"))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert "from_gmail" in exc.value.field


def test_error_carries_structured_attrs(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d.__setitem__("slug", "Foo Bar"))
    p = _write_brief(tmp_campaign_dir, bad)
    try:
        load(p)
    except BriefValidationError as exc:
        assert isinstance(exc.field, str)
        assert isinstance(exc.message, str)
        assert isinstance(exc.brief_path, Path)
        assert exc.brief_path == p


def test_nonexistent_path():
    with pytest.raises(FileNotFoundError):
        load(Path("/tmp/definitely-not-a-brief.yaml"))


def test_contacts_per_company_above_cap(tmp_campaign_dir, sample_brief_yaml):
    bad = _mutate(sample_brief_yaml, lambda d: d["who_to_contact"].__setitem__("contacts_per_company", 50))
    p = _write_brief(tmp_campaign_dir, bad)
    with pytest.raises(BriefValidationError) as exc:
        load(p)
    assert "contacts_per_company" in exc.value.field
