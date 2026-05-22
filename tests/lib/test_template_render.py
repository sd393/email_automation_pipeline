"""Tests for scripts.lib.template_render."""

from __future__ import annotations

import pytest

from scripts.lib.template_render import TemplateError, find_slots, render


def test_render_substitutes_all_slots():
    text = "Hi {{first_name}}, your company {{company}} would like {{value_prop}}.\n— {{from_name}}"
    out = render(text, {
        "first_name": "Jane", "company": "Acme", "value_prop": "X",
        "name": "Jane Doe", "role": "CEO", "from_name": "Test",
    })
    assert "Hi Jane" in out and "your company Acme" in out and "X" in out


def test_extra_values_ignored():
    text = "Hello {{first_name}}"
    out = render(text, {"first_name": "Jane", "company": "X", "name": "J", "role": "r",
                        "value_prop": "v", "from_name": "f"})
    assert out == "Hello Jane"


def test_unknown_slot_raises():
    text = "Hi {{first_name}} from {{nonexistent_slot}}"
    with pytest.raises(TemplateError):
        render(text, {"first_name": "x"})


def test_missing_value_raises():
    text = "Hi {{first_name}}"
    with pytest.raises(TemplateError) as exc:
        render(text, {})  # no first_name
    assert "first_name" in str(exc.value)


def test_find_slots_with_whitespace_and_repeats():
    text = "{{ first_name }} {{first_name}} {{ company }}"
    assert find_slots(text) == {"first_name", "company"}
