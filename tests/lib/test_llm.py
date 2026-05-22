"""Tests for scripts.lib.llm (mocked OpenAI client)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from scripts.lib.llm import COST_PER_WEB_SEARCH, COSTS, LLMClient, ParseResult


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    confidence: float


class Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Result]


# ---------------------------------------------------------------------------
# Fake client primitives
# ---------------------------------------------------------------------------

@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class FakeRefusal:
    text: str = "I can't help with that."


@dataclass
class FakeOutputItem:
    type: str = "message"
    refusal: Any = None


@dataclass
class FakeResponse:
    output_parsed: Any = None
    usage: FakeUsage = None  # type: ignore[assignment]
    output: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.usage is None:
            self.usage = FakeUsage()
        if self.output is None:
            self.output = []


class FakeModels:
    def __init__(self, reachable):
        self._reachable = set(reachable)
        self.retrieve_calls: list[str] = []

    def retrieve(self, m):
        self.retrieve_calls.append(m)
        if m not in self._reachable:
            raise RuntimeError(f"model {m} unreachable")
        return {"id": m}


class FakeResponses:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if not self.behaviors:
            raise AssertionError("no more behaviors queued")
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


class FakeClient:
    def __init__(self, reachable, behaviors=()):
        self.models = FakeModels(reachable)
        self.responses = FakeResponses(behaviors)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _client(behaviors=(), reachable=("gpt-4.1-mini", "gpt-5")):
    return LLMClient(client=FakeClient(reachable, behaviors), sleep=lambda s: None)


def test_parse_returns_parsed_instance():
    parsed = Result(value="hello", confidence=0.9)
    fake = FakeResponse(output_parsed=parsed)
    llm = _client([fake])
    r = llm.parse([{"role": "user", "content": "x"}], Result)
    assert r.parsed == parsed
    assert r.refused is False
    assert r.cost.usd > 0


def test_parse_retries_on_429(mocker):
    class FakeRateLimit(Exception):
        status_code = 429

    parsed = Result(value="ok", confidence=0.9)
    fake = FakeResponse(output_parsed=parsed, usage=FakeUsage(input_tokens=10, output_tokens=5))
    client = FakeClient(("gpt-4.1-mini", "gpt-5"), [FakeRateLimit(), fake])
    llm = LLMClient(client=client, sleep=lambda s: None)
    r = llm.parse([{"role": "user", "content": "x"}], Result)
    assert r.parsed == parsed
    assert len(client.responses.calls) == 2


def test_parse_refusal_no_retry():
    refusal_item = FakeOutputItem(type="message", refusal=FakeRefusal(text="nope"))
    fake = FakeResponse(output_parsed=None, output=[refusal_item])
    llm = _client([fake])
    r = llm.parse([{"role": "user", "content": "x"}], Result)
    assert r.refused is True
    assert r.parsed is None
    assert "nope" in r.refusal_text


def test_parse_empty_output_returns_none():
    fake = FakeResponse(output_parsed=None, output=[])
    llm = _client([fake])
    r = llm.parse([{"role": "user", "content": "x"}], Result)
    assert r.refused is False
    assert r.parsed is None
    assert r.low_confidence is False


def test_parse_low_confidence_flagged():
    parsed = Result(value="hello", confidence=0.2)
    fake = FakeResponse(output_parsed=parsed)
    llm = _client([fake])
    r = llm.parse([{"role": "user", "content": "x"}], Result)
    assert r.parsed == parsed
    assert r.low_confidence is True


def test_cascade_empty_escalates_to_tier2():
    parsed = Result(value="from-tier2", confidence=0.9)
    fake1 = FakeResponse(output_parsed=None, output=[])
    fake2 = FakeResponse(output_parsed=parsed)
    llm = _client([fake1, fake2])
    r = llm.cascade([{"role": "user", "content": "x"}], Result)
    assert r.parsed == parsed
    # cost is sum of two calls — input tokens added
    assert r.cost.input_tokens == 200


def test_cascade_refusal_does_not_escalate():
    refusal_item = FakeOutputItem(type="message", refusal=FakeRefusal(text="no"))
    fake1 = FakeResponse(output_parsed=None, output=[refusal_item])
    llm = _client([fake1])  # only one behavior queued; cascade must not call tier2
    r = llm.cascade([{"role": "user", "content": "x"}], Result)
    assert r.refused is True


def test_cascade_low_conf_then_high_conf_picks_tier2():
    low = Result(value="low", confidence=0.2)
    high = Result(value="high", confidence=0.95)
    fake1 = FakeResponse(output_parsed=low)
    fake2 = FakeResponse(output_parsed=high)
    llm = _client([fake1, fake2])
    r = llm.cascade([{"role": "user", "content": "x"}], Result)
    assert r.parsed == high
    assert r.low_confidence is False


def test_probe_picks_first_reachable():
    client = FakeClient(reachable=["gpt-5"])
    llm = LLMClient(
        client=client,
        sleep=lambda s: None,
        fallbacks=["gpt-broken", "gpt-5", "gpt-4.1"],
    )
    assert llm.available_model == "gpt-5"
    assert client.models.retrieve_calls == ["gpt-broken", "gpt-5"]


def test_probe_all_unreachable_raises():
    client = FakeClient(reachable=[])
    with pytest.raises(RuntimeError) as exc:
        LLMClient(client=client, sleep=lambda s: None, fallbacks=["a", "b"])
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_cost_calculation_known_value():
    parsed = Result(value="x", confidence=0.9)
    web_item = FakeOutputItem(type="web_search_call")
    fake = FakeResponse(
        output_parsed=parsed,
        usage=FakeUsage(input_tokens=1_000_000, output_tokens=1_000_000),
        output=[web_item, web_item],
    )
    llm = _client([fake])
    r = llm.parse([{"role": "user", "content": "x"}], Result)
    rates = COSTS["gpt-4.1-mini"]
    expected = rates["input_per_m"] + rates["output_per_m"] + 2 * COST_PER_WEB_SEARCH
    assert r.cost.usd == pytest.approx(expected)
    assert r.cost.web_search_calls == 2


def test_temperature_passed_through():
    parsed = Result(value="x", confidence=0.9)
    fake = FakeResponse(output_parsed=parsed)
    client = FakeClient(("gpt-4.1-mini", "gpt-5"), [fake])
    llm = LLMClient(client=client, sleep=lambda s: None)
    llm.parse([{"role": "user", "content": "x"}], Result, temperature=0.7)
    assert client.responses.calls[0]["temperature"] == 0.7


def test_nested_low_confidence_detected():
    inner = Result(value="x", confidence=0.1)
    nested = Nested(items=[Result(value="a", confidence=0.95), inner])
    fake = FakeResponse(output_parsed=nested)
    llm = _client([fake])
    r = llm.parse([{"role": "user", "content": "x"}], Nested)
    assert r.low_confidence is True
