"""Tests for scripts.lib.first_name."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.lib.first_name import (
    FirstNameCache,
    FirstNameResult,
    extract,
    is_ambiguous,
    naive_first,
    strip_title,
)


@dataclass
class FakeCost:
    usd: float = 0.0001


@dataclass
class FakeParseResult:
    parsed: object | None
    refused: bool = False
    refusal_text: str = ""
    low_confidence: bool = False
    cost: FakeCost = field(default_factory=FakeCost)


class FakeLLM:
    def __init__(self):
        self.call_count = 0
        self.last_kwargs = None
        self.next_result: FakeParseResult | None = None

    def queue(self, first_name=None, refused=False):
        if refused:
            self.next_result = FakeParseResult(parsed=None, refused=True)
        else:
            self.next_result = FakeParseResult(parsed=FirstNameResult(first_name=first_name))

    def parse(self, messages, text_format, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        if self.next_result is None:
            return FakeParseResult(parsed=FirstNameResult(first_name="Default"))
        out = self.next_result
        self.next_result = None
        return out


@pytest.fixture
def cache(tmp_path):
    c = FirstNameCache(tmp_path / "first_name_cache.json")
    c.load()
    return c


# ---------------------------------------------------------------------------
# Strip + naive
# ---------------------------------------------------------------------------

def test_strip_title():
    assert strip_title("Dr. Robert Smith") == "Robert Smith"
    assert strip_title("Mr Robert Smith") == "Robert Smith"
    assert strip_title("Lady Diana") == "Diana"
    assert strip_title("Robert Smith") == "Robert Smith"


def test_naive_first():
    assert naive_first("Robert Smith") == "Robert"
    assert naive_first("Andy") == "Andy"


# ---------------------------------------------------------------------------
# Ambiguity rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Marie-Claire Dupont",     # rule 1: hyphen
    "Robert Smith Jr.",         # rule 2: suffix
    "Robert Smith II",          # rule 2: suffix II
    "李伟",                       # rule 3: non-latin
    "the Foo Bar",              # rule 4: NOT_A_NAME first token
    "Mary Jane Smith",          # rule 5: three+ tokens short
])
def test_ambiguous_names(name):
    assert is_ambiguous(name) is True


@pytest.mark.parametrize("name", [
    "Robert Smith",
    "Andy",
    "Robert J. Smith",          # rule 5 short-circuited by middle initial
    "Jane Doe",
])
def test_unambiguous_names(name):
    assert is_ambiguous(name) is False


# ---------------------------------------------------------------------------
# Personalize=False
# ---------------------------------------------------------------------------

def test_no_personalize_returns_naive(cache):
    llm = FakeLLM()
    name, calls, _ = extract("Dr. Robert Smith", personalize=False, llm_client=llm, cache=cache)
    assert name == "Robert"
    assert calls == 0
    assert llm.call_count == 0


def test_no_personalize_marie_claire_naive(cache):
    """Personalize=False short-circuits even ambiguous names — naive split wins."""
    llm = FakeLLM()
    name, calls, _ = extract("Marie-Claire Dupont", personalize=False, llm_client=llm, cache=cache)
    assert name == "Marie-Claire"  # naive split takes the first whitespace token
    assert llm.call_count == 0


# ---------------------------------------------------------------------------
# Personalize=True
# ---------------------------------------------------------------------------

def test_unambiguous_no_llm(cache):
    llm = FakeLLM()
    name, calls, _ = extract("Robert Smith", personalize=True, llm_client=llm, cache=cache)
    assert name == "Robert"
    assert llm.call_count == 0


def test_ambiguous_calls_llm(cache):
    llm = FakeLLM()
    llm.queue(first_name="Marie-Claire")
    name, calls, _ = extract("Marie-Claire Dupont", personalize=True, llm_client=llm, cache=cache)
    assert name == "Marie-Claire"
    assert llm.call_count == 1
    assert calls == 1


def test_llm_receives_temperature_zero(cache):
    llm = FakeLLM()
    llm.queue(first_name="Marie-Claire")
    extract("Marie-Claire Dupont", personalize=True, llm_client=llm, cache=cache)
    assert llm.last_kwargs.get("temperature") == 0.0


def test_llm_refusal_falls_back(cache):
    llm = FakeLLM()
    llm.queue(refused=True)
    name, calls, _ = extract("Marie-Claire Dupont", personalize=True, llm_client=llm, cache=cache)
    assert name == "Marie-Claire"
    assert llm.call_count == 1


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_hit_skips_llm(cache):
    llm = FakeLLM()
    llm.queue(first_name="Marie-Claire")
    extract("Marie-Claire Dupont", personalize=True, llm_client=llm, cache=cache)
    extract("Marie-Claire Dupont", personalize=True, llm_client=llm, cache=cache)
    assert llm.call_count == 1


def test_cache_survives_reload(tmp_path):
    c1 = FirstNameCache(tmp_path / "cache.json")
    c1.load()
    c1.set("Marie-Claire Dupont", "Marie-Claire")
    c2 = FirstNameCache(tmp_path / "cache.json")
    c2.load()
    assert c2.get("Marie-Claire Dupont") == "Marie-Claire"


def test_cache_ignores_corrupt_file(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text("{ not json", encoding="utf-8")
    c = FirstNameCache(p)
    c.load()
    assert c.get("anything") is None
