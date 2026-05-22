"""Tiny ``{{slot}}`` substituter for compose_emails templates.

Supports exactly the slots in :data:`ALLOWED_SLOTS`. No Jinja dependency.
"""

from __future__ import annotations

import re


ALLOWED_SLOTS: frozenset[str] = frozenset({
    "first_name", "name", "company", "role", "value_prop", "from_name",
})


SLOT_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class TemplateError(Exception):
    """Raised on unknown slot or missing value."""


def find_slots(template_text: str) -> set[str]:
    return set(SLOT_RE.findall(template_text))


def render(template_text: str, values: dict[str, str]) -> str:
    unknown = find_slots(template_text) - ALLOWED_SLOTS
    if unknown:
        raise TemplateError(f"unknown slot: {sorted(unknown)[0]}")

    def _repl(match: re.Match[str]) -> str:
        slot = match.group(1)
        if slot not in values:
            raise TemplateError(f"slot missing from values: {slot}")
        return str(values[slot])

    return SLOT_RE.sub(_repl, template_text)
