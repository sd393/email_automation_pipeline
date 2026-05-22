"""Canonical Pydantic row models for every CSV the pipeline reads/writes, plus
thin read/write helpers.

Every model has ``extra="forbid"`` and every ``Optional[X]`` carries ``default=None``
so the schemas comply with OpenAI structured-output strict mode.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict


class DomainRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str
    domain: str
    domain_inferred: bool
    category: str
    source_url: str
    notes: str


class ContactRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str
    domain: str
    name: str
    role: str
    leverage_rationale: str
    email_if_known: Optional[str] = None
    email_source_url: Optional[str] = None
    confidence: float


class EmailRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    company: str
    domain: str
    role: str
    category: str
    confidence: Literal["verified-smtp", "verified-web", "verified-api"]
    source_url: str
    leverage_rationale: str


class OutboxRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_email: str
    to_name: str
    subject: str
    body_html: str
    body_plain: str
    first_name_used: str


class SentLogRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    to_email: str
    gmail_message_id: str
    status: Literal["sent", "quota_exceeded", "skipped_suppressed", "error"]
    error_message: Optional[str] = None


class SuppressionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    reason: Literal["hard_bounce", "manual_optout", "reply_optout"]
    source: str
    added_at: datetime


class MasterContactRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    name: str
    domain: str
    role: str
    first_seen_campaign: str
    first_seen_at: datetime


ALL_ROW_MODELS: tuple[Type[BaseModel], ...] = (
    DomainRow,
    ContactRow,
    EmailRow,
    OutboxRow,
    SentLogRow,
    SuppressionRow,
    MasterContactRow,
)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _field_order(model: Type[BaseModel]) -> list[str]:
    return list(model.model_fields.keys())


def _row_to_csv_dict(row: BaseModel) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in _field_order(type(row)):
        value = getattr(row, name)
        if value is None:
            out[name] = ""
        elif isinstance(value, datetime):
            out[name] = value.isoformat()
        elif isinstance(value, bool):
            out[name] = "true" if value else "false"
        else:
            out[name] = str(value)
    return out


def _csv_dict_to_kwargs(model: Type[BaseModel], row: dict[str, str]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    fields = model.model_fields
    for name, field in fields.items():
        if name not in row:
            continue
        raw = row[name]
        if raw == "":
            if _is_optional(field.annotation):
                kwargs[name] = None
            else:
                kwargs[name] = raw
        else:
            kwargs[name] = raw
    return kwargs


def _is_optional(annotation: Any) -> bool:
    return type(None) in getattr(annotation, "__args__", ())


# ---------------------------------------------------------------------------
# Public I/O
# ---------------------------------------------------------------------------

def read_csv(path: Path, model: Type[BaseModel]) -> list[BaseModel]:
    """Read every row from ``path`` and validate against ``model``."""
    rows: list[BaseModel] = []
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            kwargs = _csv_dict_to_kwargs(model, raw)
            rows.append(model(**kwargs))
    return rows


def write_csv_row(path: Path, row: BaseModel) -> None:
    """Append ``row`` to the CSV at ``path``.

    If the file does not exist, create it atomically with the header line first,
    then append the row. If it exists, just append.
    """
    p = Path(path)
    fields = _field_order(type(row))
    row_dict = _row_to_csv_dict(row)
    if not p.exists():
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row_dict)
        os.replace(tmp, p)
        return
    with p.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow(row_dict)


def rewrite_csv(path: Path, rows: list[BaseModel]) -> None:
    """Atomically rewrite ``path`` with all rows. Type of first row determines schema."""
    p = Path(path)
    if not rows:
        if p.exists():
            p.unlink()
        return
    model = type(rows[0])
    fields = _field_order(model)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            if type(r) is not model:
                raise TypeError(
                    f"rewrite_csv requires homogeneous rows; got {type(r).__name__} after {model.__name__}"
                )
            writer.writerow(_row_to_csv_dict(r))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# OpenAI strict-mode schema introspection
# ---------------------------------------------------------------------------

def openai_strict_schema(model: Type[BaseModel]) -> dict[str, Any]:
    """Return the JSON schema for ``model`` as it would appear to OpenAI in strict mode.

    Strict-mode rules enforced:
      * Every object has ``additionalProperties: false``.
      * Every property is in ``required`` (Optional fields become nullable types instead).
    """
    schema = model.model_json_schema()
    return _strictify(_inline_refs(schema, schema.get("$defs", {})))


def _inline_refs(schema: Any, defs: dict[str, Any]) -> Any:
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            assert ref.startswith("#/$defs/"), f"unexpected $ref: {ref}"
            target = defs[ref.split("/")[-1]]
            return _inline_refs(target, defs)
        return {k: _inline_refs(v, defs) for k, v in schema.items() if k != "$defs"}
    if isinstance(schema, list):
        return [_inline_refs(item, defs) for item in schema]
    return schema


def _strictify(schema: Any) -> Any:
    if isinstance(schema, dict):
        out = {k: _strictify(v) for k, v in schema.items()}
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
            props = out.get("properties", {})
            out["required"] = list(props.keys())
        return out
    if isinstance(schema, list):
        return [_strictify(item) for item in schema]
    return schema
