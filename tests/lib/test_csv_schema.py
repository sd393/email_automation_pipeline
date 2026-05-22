"""Tests for scripts.lib.csv_schema, including the OpenAI strict-mode gate."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from scripts.lib.csv_schema import (
    ALL_ROW_MODELS,
    ContactRow,
    DomainRow,
    EmailRow,
    MasterContactRow,
    OutboxRow,
    SentLogRow,
    SuppressionRow,
    openai_strict_schema,
    read_csv,
    rewrite_csv,
    write_csv_row,
)


def _sample(model):
    if model is DomainRow:
        return DomainRow(
            company_name="Acme",
            domain="acme.com",
            domain_inferred=False,
            category="retail",
            source_url="https://example.com/list",
            notes="",
        )
    if model is ContactRow:
        return ContactRow(
            company_name="Acme",
            domain="acme.com",
            name="Jane Doe",
            role="Founder",
            leverage_rationale="founder = decider",
            email_if_known="jane@acme.com",
            email_source_url="https://acme.com/team",
            confidence=0.8,
        )
    if model is EmailRow:
        return EmailRow(
            name="Jane Doe",
            email="jane@acme.com",
            company="Acme",
            domain="acme.com",
            role="Founder",
            category="retail",
            confidence="verified-smtp",
            source_url="https://acme.com/team",
            leverage_rationale="founder",
        )
    if model is OutboxRow:
        return OutboxRow(
            to_email="jane@acme.com",
            to_name="Jane Doe",
            subject="Quick question",
            body_html="<p>hi</p>",
            body_plain="hi",
            first_name_used="Jane",
        )
    if model is SentLogRow:
        return SentLogRow(
            timestamp=datetime(2026, 5, 22, 12, 0, 0),
            to_email="jane@acme.com",
            gmail_message_id="abc",
            status="sent",
            error_message=None,
        )
    if model is SuppressionRow:
        return SuppressionRow(
            email="jane@acme.com",
            reason="hard_bounce",
            source="msg-id-1",
            added_at=datetime(2026, 5, 22, 12, 0, 0),
        )
    if model is MasterContactRow:
        return MasterContactRow(
            email="jane@acme.com",
            name="Jane Doe",
            domain="acme.com",
            role="Founder",
            first_seen_campaign="2026-05_test",
            first_seen_at=datetime(2026, 5, 22, 12, 0, 0),
        )
    raise AssertionError(model)


@pytest.mark.parametrize("model", ALL_ROW_MODELS, ids=lambda m: m.__name__)
def test_round_trip(tmp_path, model):
    row = _sample(model)
    path = tmp_path / "out.csv"
    write_csv_row(path, row)
    loaded = read_csv(path, model)
    assert len(loaded) == 1
    assert loaded[0].model_dump() == row.model_dump()


@pytest.mark.parametrize("model", ALL_ROW_MODELS, ids=lambda m: m.__name__)
def test_append_does_not_duplicate_header(tmp_path, model):
    row = _sample(model)
    path = tmp_path / "out.csv"
    write_csv_row(path, row)
    write_csv_row(path, row)
    text = path.read_text(encoding="utf-8")
    header_count = text.splitlines().count(",".join(model.model_fields.keys()))
    assert header_count == 1
    loaded = read_csv(path, model)
    assert len(loaded) == 2


def test_missing_required_field_raises():
    with pytest.raises(ValidationError):
        DomainRow(company_name="Acme")  # type: ignore[call-arg]


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        ContactRow(
            company_name="Acme",
            domain="acme.com",
            name="Jane",
            role="Founder",
            leverage_rationale="r",
            confidence=0.5,
            sneaky="value",  # type: ignore[call-arg]
        )


def test_optional_field_defaults_to_none():
    row = ContactRow(
        company_name="Acme",
        domain="acme.com",
        name="Jane",
        role="Founder",
        leverage_rationale="r",
        confidence=0.5,
    )
    assert row.email_if_known is None
    assert row.email_source_url is None


def test_rewrite_csv_atomic(tmp_path):
    path = tmp_path / "out.csv"
    rows = [_sample(DomainRow), _sample(DomainRow)]
    rewrite_csv(path, rows)
    loaded = read_csv(path, DomainRow)
    assert len(loaded) == 2


@pytest.mark.parametrize("model", ALL_ROW_MODELS, ids=lambda m: m.__name__)
def test_openai_strict_mode_compliance(model):
    """THIS GATES M0. Every model's JSON schema must satisfy OpenAI strict mode."""
    schema = openai_strict_schema(model)
    _assert_strict(schema)


def _assert_strict(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            assert schema.get("additionalProperties") is False, (
                f"object schema must have additionalProperties:false, got {schema}"
            )
            props = list(schema.get("properties", {}).keys())
            required = schema.get("required", [])
            assert set(required) == set(props), (
                f"every property must be required (props={props}, required={required})"
            )
        for v in schema.values():
            _assert_strict(v)
    elif isinstance(schema, list):
        for item in schema:
            _assert_strict(item)
