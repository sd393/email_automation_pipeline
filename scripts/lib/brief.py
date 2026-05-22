"""Pydantic schema + loader for brief.yaml.

The brief is the *only* allowed source of segment-specific values. Every stage
script loads a brief through ``load()`` and reads from a typed ``Brief``.
Validation is strict (``extra="forbid"`` everywhere) so structural errors fail
at brief-load time, not three stages later.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BriefValidationError(Exception):
    """Carries structured fields so the main-wrapper exit-3 contract can emit a JSON line."""

    def __init__(self, field: str, message: str, brief_path: Path) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message
        self.brief_path = Path(brief_path)


class TargetSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment: str
    include: list[str]
    exclude: list[str]
    geography: str
    target_domain_count: int

    @field_validator("segment", "geography")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("include")
    @classmethod
    def _include_nonempty(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("must contain at least one item")
        return v

    @field_validator("target_domain_count")
    @classmethod
    def _positive_count(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be > 0")
        return v


class WhoToContactSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority_roles: list[str]
    deprioritize: list[str]
    contacts_per_company: int = 3

    @field_validator("priority_roles")
    @classmethod
    def _at_least_one(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("must contain at least one role")
        return v

    @field_validator("contacts_per_company")
    @classmethod
    def _cap(cls, v: int) -> int:
        if v < 1 or v > 12:
            raise ValueError("must be between 1 and 12")
        return v


class MessageSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: Path
    value_prop: str
    personalize_first_name: bool = True
    from_name: str
    from_gmail: str
    reply_to: Optional[str] = None

    @field_validator("value_prop", "from_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("from_gmail")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError(f"does not look like an email: {v!r}")
        return v

    @field_validator("template")
    @classmethod
    def _template_exists(cls, v: Path) -> Path:
        p = Path(v)
        if not p.is_file():
            raise ValueError(f"template path does not exist: {p}")
        return p


VerifierName = Literal["smtp_probe", "web_citation", "api_provider"]


class VerifierSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain: list[VerifierName]
    rate_per_sec: float = 0.5
    per_hour_cap: int = 50
    burst: int = 10
    greylist_retry: bool = True

    @field_validator("chain")
    @classmethod
    def _at_least_one(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("must contain at least one verifier")
        return v


class SendingSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    send_test_count: int = 10
    send_rate_per_day: int
    throttle_seconds: float

    @field_validator("send_test_count")
    @classmethod
    def _test_count_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("send_rate_per_day")
    @classmethod
    def _daily_cap(cls, v: int) -> int:
        if v < 1 or v > 2000:
            raise ValueError("must be between 1 and 2000 (Workspace safety cap)")
        return v

    @field_validator("throttle_seconds")
    @classmethod
    def _throttle_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be > 0")
        return v


class SafetySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["this_campaign", "all_campaigns"] = "all_campaigns"


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    created_at: date
    target: TargetSection
    who_to_contact: WhoToContactSection
    message: MessageSection
    verifier: VerifierSection
    sending: SendingSection
    safety: SafetySection
    notes: Optional[str] = None

    @field_validator("slug")
    @classmethod
    def _kebab(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError(f"must be kebab-case (got {v!r})")
        return v


def load(path: Path) -> Brief:
    """Read YAML from ``path``, validate, return ``Brief``.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        BriefValidationError: on any schema/validator failure.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"brief.yaml not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if raw is None:
        raise BriefValidationError(field="<root>", message="brief is empty", brief_path=p)
    if not isinstance(raw, dict):
        raise BriefValidationError(
            field="<root>", message="brief root must be a mapping", brief_path=p
        )
    try:
        return Brief.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        raise BriefValidationError(field=loc, message=first["msg"], brief_path=p) from exc
