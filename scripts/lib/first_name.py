"""First-name extraction with formal-ambiguity rules + persistent on-disk cache.

The LLM is only consulted when the deterministic ambiguity rules trigger and
the cache misses. Cache writes are atomic so a kill+resume never loses work.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


TITLE_PATTERN = re.compile(r"^(Dr|Mr|Mrs|Ms|Prof|Sir|Lord|Lady)\.?\s+", re.IGNORECASE)
SUFFIX_TOKENS = frozenset({"Jr.", "Sr.", "II", "III", "IV", "Jr", "Sr"})
NOT_A_NAME_TOKENS = frozenset({"the", "mr", "ms", "mrs", "dr", "prof", "sir", "dame", "lord", "lady", "rev"})
MIDDLE_INITIAL = re.compile(r"^[A-Z]\.$")


class FirstNameResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str


def strip_title(name: str) -> str:
    return TITLE_PATTERN.sub("", name.strip(), count=1)


def naive_first(name_stripped: str) -> str:
    tokens = name_stripped.strip().split()
    return tokens[0] if tokens else name_stripped


def is_ambiguous(name_stripped: str) -> bool:
    tokens = name_stripped.split()
    if not tokens:
        return False
    first = tokens[0]
    # Rule 1: hyphen in first token
    if "-" in first:
        return True
    # Rule 2: any suffix token
    if any(t in SUFFIX_TOKENS for t in tokens):
        return True
    # Rule 3: codepoint > 0x024F in first token
    if any(ord(ch) > 0x024F for ch in first):
        return True
    # Rule 4: first token is in NOT_A_NAME_TOKENS
    if first.lower() in NOT_A_NAME_TOKENS:
        return True
    # Rule 5: three+ tokens, both first two short (<=8), neither is middle initial
    if len(tokens) >= 3:
        t1, t2 = tokens[0], tokens[1]
        if len(t1) <= 8 and len(t2) <= 8:
            if not MIDDLE_INITIAL.match(t1) and not MIDDLE_INITIAL.match(t2):
                return True
    return False


class FirstNameCache:
    """JSON-backed dict keyed by post-title-strip name string."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state: dict[str, str] = {}

    def load(self) -> None:
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._state = {}

    def get(self, key: str) -> str | None:
        return self._state.get(key)

    def set(self, key: str, value: str) -> None:
        self._state[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def extract(
    name: str,
    *,
    personalize: bool,
    llm_client: Any,
    cache: FirstNameCache,
) -> tuple[str, int, float]:
    """Return (first_name, llm_calls_made, cost_usd)."""
    stripped = strip_title(name)
    if not personalize:
        return (naive_first(stripped), 0, 0.0)

    cached = cache.get(stripped)
    if cached is not None:
        return (cached, 0, 0.0)

    if not is_ambiguous(stripped):
        result = naive_first(stripped)
        cache.set(stripped, result)
        return (result, 0, 0.0)

    # Ambiguous + not cached → ask the LLM.
    cost = 0.0
    try:
        parse_result = llm_client.parse(
            messages=[
                {"role": "system", "content": (
                    "You parse personal names. Given a full name, output the form the "
                    "person would prefer in a salutation in English. Examples: "
                    "'Marie-Claire Dupont' -> 'Marie-Claire'; "
                    "'李伟' -> 'Wei' (transliterate); "
                    "'Robert Smith Jr.' -> 'Robert'. "
                    "If ambiguous between a compound and a simple first name, prefer the "
                    "shorter form. Return only the first name."
                )},
                {"role": "user", "content": stripped},
            ],
            text_format=FirstNameResult,
            tier="tier1",
            temperature=0.0,
        )
        cost = getattr(parse_result.cost, "usd", 0.0) if parse_result.cost else 0.0
    except Exception:
        result = naive_first(stripped)
        cache.set(stripped, result)
        return (result, 1, cost)

    if parse_result.refused or parse_result.parsed is None:
        result = naive_first(stripped)
        cache.set(stripped, result)
        return (result, 1, cost)
    result = parse_result.parsed.first_name
    cache.set(stripped, result)
    return (result, 1, cost)
