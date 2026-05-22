"""Thin wrapper around openai.OpenAI for structured-output + cascade calls.

Public surface:
    LLMClient.parse(...)   — single structured-output call, retries on transient.
    LLMClient.cascade(...) — tier1 first, escalate to tier2 on empty/low-confidence.

The wrapper distinguishes three failure modes (review issue #5):
    * refusal       — model safety-refused; caller MUST NOT retry/escalate.
    * empty parsed  — output present but no parse; caller MAY retry/escalate.
    * low confidence — parsed instance valid, ``confidence < threshold``; caller MAY escalate.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Type

from pydantic import BaseModel


COSTS: dict[str, dict[str, float]] = {
    "gpt-4.1-mini": {"input_per_m": 0.15, "output_per_m": 0.60},
    "gpt-4.1": {"input_per_m": 2.0, "output_per_m": 8.0},
    "gpt-5": {"input_per_m": 10.0, "output_per_m": 30.0},
    "gpt-5.2": {"input_per_m": 5.0, "output_per_m": 20.0},
}
COST_PER_WEB_SEARCH = 0.025


@dataclass
class CostReport:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    web_search_calls: int = 0
    usd: float = 0.0

    def __add__(self, other: "CostReport") -> "CostReport":
        return CostReport(
            model=f"{self.model}+{other.model}" if self.model and other.model else (self.model or other.model),
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            web_search_calls=self.web_search_calls + other.web_search_calls,
            usd=self.usd + other.usd,
        )


@dataclass
class ParseResult:
    parsed: BaseModel | None
    refused: bool
    refusal_text: str
    low_confidence: bool
    cost: CostReport
    raw: Any = None


def _cost_for(model: str, input_tokens: int, output_tokens: int, web_search_calls: int) -> float:
    rates = COSTS.get(model, {"input_per_m": 0.0, "output_per_m": 0.0})
    return (
        input_tokens * rates["input_per_m"] / 1_000_000
        + output_tokens * rates["output_per_m"] / 1_000_000
        + web_search_calls * COST_PER_WEB_SEARCH
    )


def _walk_min_confidence(obj: Any) -> float | None:
    """Walk a Pydantic model and return the minimum ``confidence`` value found."""
    if isinstance(obj, BaseModel):
        out = None
        for name in obj.__class__.model_fields:
            v = getattr(obj, name)
            if name == "confidence" and isinstance(v, (int, float)):
                out = v if out is None else min(out, v)
            sub = _walk_min_confidence(v)
            if sub is not None:
                out = sub if out is None else min(out, sub)
        return out
    if isinstance(obj, (list, tuple)):
        candidates = [c for c in (_walk_min_confidence(x) for x in obj) if c is not None]
        return min(candidates) if candidates else None
    if isinstance(obj, dict):
        candidates = [c for c in (_walk_min_confidence(x) for x in obj.values()) if c is not None]
        return min(candidates) if candidates else None
    return None


# Module-level imports done lazily so tests can patch ``openai`` cleanly.
try:  # pragma: no cover
    import openai  # type: ignore
    _OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    openai = None  # type: ignore
    _OPENAI_AVAILABLE = False


class LLMClient:
    def __init__(
        self,
        tier1: str = "gpt-4.1-mini",
        tier2: str = "gpt-5",
        fallbacks: list[str] | None = None,
        low_confidence_threshold: float = 0.4,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tier1 = tier1
        self.tier2 = tier2
        self.fallbacks = list(fallbacks or [tier1, tier2, "gpt-4.1"])
        self.low_confidence_threshold = low_confidence_threshold
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:
            if not _OPENAI_AVAILABLE:
                raise RuntimeError("openai package not available")
            self._client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.available_model = self._probe()

    # ------------------------------------------------------------------
    # Startup probe
    # ------------------------------------------------------------------

    def _probe(self) -> str:
        attempted: list[tuple[str, str]] = []
        for m in self.fallbacks:
            try:
                self._client.models.retrieve(m)
                return m
            except Exception as exc:  # noqa: BLE001 — probe must tolerate any failure
                attempted.append((m, type(exc).__name__))
        raise RuntimeError(
            "No fallback model reachable; tried: "
            + ", ".join(f"{m} ({status})" for m, status in attempted)
        )

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse(
        self,
        messages: list[dict],
        text_format: Type[BaseModel],
        *,
        tools: list[dict] | None = None,
        tier: Literal["tier1", "tier2"] = "tier1",
        max_retries: int = 3,
        temperature: float = 0.0,
    ) -> ParseResult:
        model = self.tier1 if tier == "tier1" else self.tier2
        cumulative = CostReport(model=model)
        for attempt in range(max_retries + 1):
            try:
                response = self._client.responses.parse(
                    model=model,
                    input=messages,
                    text_format=text_format,
                    tools=tools or [],
                    temperature=temperature,
                )
            except Exception as exc:
                if _is_auth(exc):
                    raise
                if _is_transient(exc) and attempt < max_retries:
                    self._sleep(_backoff(attempt))
                    continue
                raise
            return self._unpack(response, text_format, cumulative)
        # Unreachable: loop either returns or raises.
        raise RuntimeError("parse exhausted retries without raising")

    def _unpack(self, response: Any, text_format: Type[BaseModel], cost_acc: CostReport) -> ParseResult:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
        web_search_calls = _count_web_search(response)
        usd = _cost_for(cost_acc.model, input_tokens, output_tokens, web_search_calls)
        cost = CostReport(
            model=cost_acc.model,
            input_tokens=cost_acc.input_tokens + input_tokens,
            output_tokens=cost_acc.output_tokens + output_tokens,
            web_search_calls=cost_acc.web_search_calls + web_search_calls,
            usd=cost_acc.usd + usd,
        )

        refusal_text = _extract_refusal(response)
        if refusal_text:
            return ParseResult(
                parsed=None,
                refused=True,
                refusal_text=refusal_text,
                low_confidence=False,
                cost=cost,
                raw=response,
            )

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            return ParseResult(
                parsed=None,
                refused=False,
                refusal_text="",
                low_confidence=False,
                cost=cost,
                raw=response,
            )

        if not isinstance(parsed, text_format):
            try:
                parsed = text_format.model_validate(parsed)
            except Exception:
                return ParseResult(
                    parsed=None,
                    refused=False,
                    refusal_text="",
                    low_confidence=False,
                    cost=cost,
                    raw=response,
                )

        conf = _walk_min_confidence(parsed)
        low = conf is not None and conf < self.low_confidence_threshold
        return ParseResult(
            parsed=parsed,
            refused=False,
            refusal_text="",
            low_confidence=low,
            cost=cost,
            raw=response,
        )

    # ------------------------------------------------------------------
    # Cascade
    # ------------------------------------------------------------------

    def cascade(
        self,
        messages: list[dict],
        text_format: Type[BaseModel],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.0,
    ) -> ParseResult:
        r1 = self.parse(messages, text_format, tools=tools, tier="tier1", temperature=temperature)
        if r1.refused:
            return r1
        if r1.parsed is None or r1.low_confidence:
            r2 = self.parse(messages, text_format, tools=tools, tier="tier2", temperature=temperature)
            combined_cost = r1.cost + r2.cost
            if r2.refused:
                return ParseResult(
                    parsed=None, refused=True, refusal_text=r2.refusal_text,
                    low_confidence=False, cost=combined_cost, raw=r2.raw,
                )
            if r1.parsed is None:
                return ParseResult(
                    parsed=r2.parsed, refused=False, refusal_text="",
                    low_confidence=r2.low_confidence, cost=combined_cost, raw=r2.raw,
                )
            # both parsed; pick higher confidence
            c1 = _walk_min_confidence(r1.parsed) or 0.0
            c2 = _walk_min_confidence(r2.parsed) or 0.0
            chosen = r2 if c2 >= c1 else r1
            return ParseResult(
                parsed=chosen.parsed, refused=False, refusal_text="",
                low_confidence=chosen.low_confidence, cost=combined_cost, raw=chosen.raw,
            )
        return r1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backoff(attempt: int) -> float:
    base = min(32.0, 2 ** attempt)
    return base * random.uniform(0.5, 1.5)


def _is_auth(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in ("AuthenticationError", "PermissionDeniedError")


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in (
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "ConnectionError",
        "TimeoutError",
    ):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    return False


def _count_web_search(response: Any) -> int:
    output = getattr(response, "output", None) or []
    count = 0
    for item in output:
        kind = getattr(item, "type", None)
        if isinstance(item, dict):
            kind = item.get("type")
        if kind == "web_search_call":
            count += 1
    return count


def _extract_refusal(response: Any) -> str:
    output = getattr(response, "output", None) or []
    for item in output:
        refusal = getattr(item, "refusal", None)
        if isinstance(item, dict):
            refusal = item.get("refusal")
        if refusal:
            if isinstance(refusal, dict):
                return refusal.get("text", "") or refusal.get("reason", "") or "refused"
            if isinstance(refusal, str):
                return refusal
            text = getattr(refusal, "text", None)
            if text:
                return text
            return "refused"
    return ""
