Now I have all the information needed. Let me write the section content for `section-04-lib-llm-and-gmail`.

# Section 04 — `lib/llm.py` and `lib/gmail.py`

## Purpose

Implement the OpenAI LLM wrapper and Gmail OAuth/send client. These are two of the cross-cutting libraries that live in `scripts/lib/` and are dependencies of nearly every downstream stage (M1 sourcing, M2 discovery, M3 composition + send, M4 bounce-poll).

This section is parallel-friendly with section-03-lib-observability (different concerns, no shared fixtures).

## Dependencies

- **Section 01** must be complete: `pyproject.toml` declares `openai>=1.50`, `google-api-python-client`, `google-auth-oauthlib`, `pydantic>=2`. `config/secrets.example.env` documents `OPENAI_API_KEY` and Gmail-credential file paths.
- **Section 02** must be complete: this section imports `Brief` types and Pydantic-row helpers from `lib/brief.py` and `lib/csv_schema.py` indirectly via test fixtures, and uses the schema rules locked there (every `Optional[X]` has `default=None`, every model sets `extra="forbid"`).

## Files to create

- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/lib/llm.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/lib/gmail.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/lib/test_llm.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/lib/test_gmail.py`

The two libraries live in the same section because they're both "external-IO wrappers with tier/scope cascades and refusal-vs-error distinctions" — they share the same testing pattern (full HTTP mock) and they're each independently small.

---

## Cross-cutting invariants (apply to everything below)

These come from the v1 plan's §10. The implementer should not re-derive them.

**Schema rules (every Pydantic model in this codebase):**
- `model_config = ConfigDict(extra="forbid")`.
- `Optional[X]` fields use `default=None`.
- A test in `tests/lib/test_csv_schema.py` (section 02) gates M0 by running every model through OpenAI's strict-mode validator — schemas this section defines (`FirstNameResult`, etc., if any are declared here; mostly they live in downstream sections) must also pass.

**Error taxonomy:**
- Transient (retried): 429, 5xx, timeouts, `ConnectionError`. Exp-backoff up to 3 attempts.
- Halt (whole-stage): authentication errors (401/403). The wrapper itself does NOT halt the stage; it raises so the caller's main loop can `obs.finish(FAILED)` and exit 2.

**Cost constants** (used by `lib/llm.py`):
```
COSTS = {
    "gpt-4.1-mini": {"input_per_m": 0.15, "output_per_m": 0.60},
    "gpt-5":        {"input_per_m": 10.0, "output_per_m": 30.0},
    "gpt-5.2":      {"input_per_m": 5.0,  "output_per_m": 20.0},
}
COST_PER_WEB_SEARCH = 0.025
```

---

## Part A — `lib/llm.py`

### Design intent

The thinnest possible wrapper around `openai.OpenAI` that gives us:
1. Tiered model cascade (`tier1` cheap default, `tier2` escalation when results are bad).
2. Hosted `web_search` tool support.
3. Structured outputs via `responses.parse(text_format=PydanticModel, strict=True)`.
4. Retry-on-429 with exp backoff + jitter.
5. Cost tracking per call.
6. **Crucial distinction:** model **refusal** (do NOT retry, do NOT escalate, surface the refusal text) vs. **empty output** (do retry / escalate to tier2) vs. **low confidence** (parsed result present but a `confidence` field below threshold — also a candidate for tier2).

This refusal-vs-empty split is review issue #5 from the plan. Callers (M1 `source_domains.py`, M2 `discover_contacts.py`, M3 `compose_emails.py`) all rely on it to decide whether to mark a row `search_fail`/`discovery_fail` vs. try again.

### Public interface

```python
# scripts/lib/llm.py

from dataclasses import dataclass
from typing import Literal, Type
from pydantic import BaseModel

@dataclass
class CostReport:
    model: str
    input_tokens: int
    output_tokens: int
    web_search_calls: int
    usd: float

@dataclass
class ParseResult:
    parsed: BaseModel | None
    refused: bool             # True only if model safety-refused
    refusal_text: str         # filled when refused=True; "" otherwise
    low_confidence: bool      # True if any field named 'confidence' < threshold
    cost: CostReport

class LLMClient:
    def __init__(
        self,
        tier1: str = "gpt-4.1-mini",
        tier2: str = "gpt-5",
        fallbacks: list[str] = ["gpt-5.2", "gpt-5", "gpt-4.1"],
        low_confidence_threshold: float = 0.4,
    ):
        """Probe `fallbacks` at init by making a cheap models.retrieve() call
        (or equivalent). Use the first reachable as the 'available model';
        `tier1`/`tier2` are the active cascade tiers.

        Raises RuntimeError if NO fallback is reachable. The error message
        names every model tried and the HTTP status received for each."""

    def parse(
        self,
        messages: list[dict],
        text_format: Type[BaseModel],
        *,
        tools: list[dict] = None,
        tier: Literal["tier1", "tier2"] = "tier1",
        max_retries: int = 3,
        temperature: float = 0.0,
    ) -> ParseResult:
        """Call openai.responses.parse with structured outputs.

        Behavior:
          - On 429: exp-backoff 1s, 2s, 4s, ..., max 32s, with jitter on each
            attempt. Up to `max_retries` retries.
          - On 5xx / Timeout / ConnectionError: same backoff schedule.
          - On 401 / 403: re-raise immediately (caller halts the stage).
          - On model refusal (resp.output[0].refusal is set): return
            ParseResult(parsed=None, refused=True, refusal_text=..., cost=...).
            Caller MUST NOT retry/escalate.
          - On empty output_parsed (output present but no parse): return
            ParseResult(parsed=None, refused=False, ...). Caller MAY retry/escalate.
          - On low confidence (any field literally named 'confidence', at any
            nesting level, with float value < threshold): return
            ParseResult(parsed=<instance>, low_confidence=True, ...).
            Caller MAY escalate; the parsed instance is still valid.
          - temperature is passed through verbatim. Default 0.0."""

    def cascade(
        self,
        messages: list[dict],
        text_format: Type[BaseModel],
        *,
        tools: list[dict] = None,
        temperature: float = 0.0,
    ) -> ParseResult:
        """Try tier1 first.
          - tier1 refused=True → DO NOT escalate; propagate refusal.
          - tier1 parsed=None, refused=False (empty output) → escalate to tier2.
          - tier1 low_confidence=True → also try tier2, prefer the higher-
            confidence result. (If tier2 itself returns low_confidence, return
            whichever has the higher numeric confidence; ties → tier2.)
          - tier1 parsed OK and not low_confidence → return tier1 result.
        Cost accumulates across both calls when tier2 is invoked."""
```

### Implementation notes

- Use `openai.OpenAI()` constructed from `OPENAI_API_KEY` env var. Do NOT accept the key as a parameter — keys are env-only per the security rules.
- The `cost` field on `ParseResult` is computed from `response.usage.input_tokens`, `response.usage.output_tokens`, and a count of tool calls of type `web_search`. Formula: `(input_tokens * input_per_m / 1_000_000) + (output_tokens * output_per_m / 1_000_000) + (web_search_calls * COST_PER_WEB_SEARCH)`.
- Backoff with jitter: `sleep_for = min(32.0, 2 ** attempt) * random.uniform(0.5, 1.5)`.
- Low-confidence detection walks the parsed model with a recursive helper. For v1, "confidence" is detected by attribute name; nested submodels are searched. Lists of submodels: take the min confidence among elements.
- On `cascade`, when tier2 is invoked, the returned `ParseResult.cost` is the **sum** of tier1 + tier2 costs. Use a `+` operator on `CostReport` (or build one explicitly).

### Test stubs — `tests/lib/test_llm.py`

```python
# Test: parse() with mocked OpenAI client returning structured output → ParseResult.parsed
#       is the expected Pydantic instance; cost non-zero.
# Test: parse() on 429 (first call), success (second) → retries; ParseResult.parsed set, cost
#       reflects both attempts' input/output tokens.
# Test: parse() with refusal in resp.output[0].refusal → refused=True, parsed=None,
#       refusal_text populated.
# Test: parse() with empty output_parsed and no refusal → refused=False, parsed=None.
# Test: parse() with parsed result whose confidence < threshold → low_confidence=True, parsed set.
# Test: cascade() — tier1 returns parsed=None refused=False → tier2 called; cost accumulates.
# Test: cascade() — tier1 returns refused=True → tier2 NOT called; ParseResult propagates refusal.
# Test: cascade() — tier1 low_confidence + tier2 high_confidence → tier2 result preferred.
# Test: model probe at startup — first fallback unreachable, second reachable → uses second.
# Test: all fallbacks unreachable → RuntimeError at __init__ naming every tried model.
# Test: cost calculation — token counts × per-model rates + web_search_calls × $0.025 matches
#       known-good values for canned response.
# Test: temperature=0 passed through to the API call.
```

Use `pytest-mock` (`mocker`) to patch `openai.OpenAI` at the module level. Construct fake response objects matching the shape of `openai.types.responses.Response` (or whatever your installed SDK version uses for `responses.parse`). Keep fakes minimal: only the fields the implementation actually reads.

For the 429 retry test, the mock should raise `openai.RateLimitError` on the first call and return success on the second. Patch `time.sleep` or use a `mocker.patch("scripts.lib.llm.time.sleep")` so the test doesn't actually wait.

---

## Part B — `lib/gmail.py`

### Design intent

OAuth + send wrapper around the Gmail API. Three responsibilities:
1. `authorize()` — run OAuth flow if no token; refresh if expired; **detect scope-superset mismatch and force re-flow** when a new scope is requested (review issue #7).
2. `GmailClient.send()` — build correct MIME (HTML + plain alternative), base64-url encode, POST to `messages.send`. Map 429 / quota-exceeded to a typed exception.
3. `GmailClient.list_bounces()` — **stub signature only in this section; full implementation is deferred to section 12.** Define the dataclass/Pydantic models (`BounceRecord`) and raise `NotImplementedError` from the method body, so M3 send code can import the symbol without a circular dependency on M4.

### Scopes used by this tool

- `https://www.googleapis.com/auth/gmail.send` — used by Stage 5 (`send_emails.py`).
- `https://www.googleapis.com/auth/gmail.readonly` — used by Stage 6 (`poll_bounces.py`).

Scope-superset rule: if the existing `token.json` has scopes `[X]` and the caller requests scopes `[X, Y]`, Google's OAuth refresh will fail at API-use time with a confusing permission-denied. We pre-empt this in `authorize()` by inspecting `creds.scopes` and forcing a fresh `InstalledAppFlow.run_local_server()` when the requested set is not a subset.

### Public interface

```python
# scripts/lib/gmail.py

from pathlib import Path
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from google.oauth2.credentials import Credentials

class SendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gmail_message_id: str
    thread_id: str

class QuotaExceeded(Exception):
    """Raised on 429 or 'Daily user sending limit exceeded' from Gmail.
    Caller (send_emails.py) maps this to the pessimistic counter
    decrement-and-retry path."""

class BounceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_recipient: str
    gmail_message_id: str
    bounce_date: datetime

def authorize(
    credentials_path: Path,
    token_path: Path,
    scopes: list[str],
) -> Credentials:
    """Run OAuth flow if no token; refresh if expired. Return valid creds.

    Behavior, in order:
      1. If token_path exists, load it. Inspect creds.scopes.
      2. If requested `scopes` is NOT a subset of existing creds.scopes:
         - Print to stdout: 'Gmail token has scopes [<existing>]; required
           [<requested>]. Re-authorizing.'
         - Delete token_path.
         - Fall through to step 3.
      3. If no token or token deleted in step 2:
         - Run google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
             credentials_path, scopes=<union of existing and requested>
           ).run_local_server(port=0). This opens a browser.
         - Save the new creds to token_path.
      4. If creds exist and are expired but have a refresh_token: call
         creds.refresh(google.auth.transport.requests.Request()), save back.
      5. Return creds.

    This function is invoked once at the start of every script that touches Gmail."""

class GmailClient:
    def __init__(self, creds: Credentials):
        """Build googleapiclient.discovery.build('gmail', 'v1', credentials=creds)."""

    def send(
        self,
        to: str,
        *,
        subject: str,
        body_html: str,
        body_plain: str,
        from_address: str,
        from_name: str,
        reply_to: str,
        headers: dict[str, str] | None = None,
    ) -> SendResult:
        """Build a multipart/alternative MIME message with:
          - Plain part = body_plain.
          - HTML part = body_html.
          - From: "<from_name>" <from_address>
          - To: to
          - Subject: subject
          - Reply-To: reply_to
          - Any extra headers from `headers` dict (e.g., List-Unsubscribe is OUT
            of v1; this dict exists for future-compatibility only).

        Base64-url encode via base64.urlsafe_b64encode(msg.as_bytes()).decode()
        (NOT plain b64encode — the URL-safe variant is required by Gmail API).

        POST to users().messages().send(userId='me', body={'raw': <encoded>}).

        Return SendResult(gmail_message_id, thread_id) from the API response.

        Raise QuotaExceeded on:
          - HTTP 429.
          - Any 4xx whose message contains 'Daily user sending limit exceeded'.

        Re-raise other API errors (5xx) verbatim; the caller's retry logic
        (send_emails.py) handles them.

        If the API response's resolved 'From' header differs from
        `from_address` (Gmail send-as rewriting), log a warning via
        observability (but do not fail). The caller is expected to inject
        the observer; for v1, the GmailClient can take an optional observer
        in __init__ OR just print to stderr — either works."""

    def list_bounces(
        self,
        since_message_id: str | None = None,
    ) -> list[BounceRecord]:
        """Query Gmail for bounce messages and return BounceRecord list.

        DEFERRED TO SECTION 12. This method MUST be defined in v1's lib/gmail.py
        (so M3 imports don't break) but the body raises NotImplementedError.

        Eventual query (for §12 implementer reference, do NOT implement here):
          q='from:mailer-daemon subject:"Delivery Status Notification (Failure)"'
          For each match: fetch full message, parse 'Final-Recipient: rfc822;<email>'
          from the text/plain body, build BounceRecord."""
        raise NotImplementedError("list_bounces is implemented in section 12 (M4)")
```

### Implementation notes

- MIME construction uses stdlib: `email.message.EmailMessage` or `email.mime.multipart.MIMEMultipart` + `MIMEText`. The plan calls for `multipart/alternative` so a text-only client falls back to `body_plain`.
- `from_name` containing characters needing quoting (e.g., commas) must be RFC-2822 escaped. Use `email.utils.formataddr((from_name, from_address))`.
- The Gmail API client builder (`googleapiclient.discovery.build`) does HTTP itself; do NOT call `requests` directly. Errors come back as `googleapiclient.errors.HttpError`; inspect `err.resp.status` and `err.error_details` / `err.content` to decide `QuotaExceeded` vs. raise-through.
- Send-as rewriting: Gmail Workspace can rewrite `From:` to the user's primary address when the requested `from_address` is configured as a send-as alias the account doesn't actually own. The API response includes the chosen `From` indirectly via the message's `labelIds` + a follow-up `messages.get()`. Simpler: just check `response['payload']['headers']` if available, else accept that v1 may not catch every rewrite. The warning is a nice-to-have, not a correctness gate.
- This module must be runnable as a CLI for the one-time authorize step (README v1 says `python scripts/lib/gmail.py authorize`). Add an `if __name__ == "__main__":` block that parses one positional arg (`authorize`), reads paths from `config/secrets.env`-style env vars (`GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`) with sensible defaults, and calls `authorize()` with `scopes=["https://www.googleapis.com/auth/gmail.send"]`.

### Test stubs — `tests/lib/test_gmail.py`

```python
# authorize() tests:
# Test: token.json present, scopes match → returns creds without browser prompt
#       (mock InstalledAppFlow to assert it's NOT called).
# Test: token.json present, requested scopes are a strict superset of existing → forces re-flow;
#       documented "Re-authorizing" message printed; InstalledAppFlow.run_local_server called.
# Test: token.json present but expired with refresh_token → creds.refresh() called, no browser.
# Test: token.json absent → InstalledAppFlow.run_local_server() called (mocked); resulting
#       creds saved to token_path.

# send() tests:
# Test: mocked Gmail HTTP API; send() builds correct MIME structure.
#       Verify: raw field is base64.urlsafe_b64encode(msg.as_bytes()).decode()
#       (NOT plain b64encode). Decode raw → headers include To, From, Subject, Reply-To;
#       multipart/alternative with both body_html and body_plain.
# Test: 429 from API (HttpError with resp.status=429) → raises QuotaExceeded.
# Test: 4xx with message "Daily user sending limit exceeded" → raises QuotaExceeded.
# Test: 5xx → raises HttpError verbatim (caller will retry).
# Test: 200 response where echoed From differs from requested from_address → warning logged
#       to activity.log (or stderr, depending on observer wiring).
# Test: from_name containing a comma is RFC-2822 escaped in the resulting MIME header.

# list_bounces() tests:
# Test: calling list_bounces in v1 raises NotImplementedError (section 12 will implement).
#       Note: the full bounce-parsing tests are in section 12; only the stub is verified here.
```

### Mocking patterns to use

- For `authorize()`: patch `googleapiclient.discovery.build`, `google.oauth2.credentials.Credentials.from_authorized_user_file`, and `google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file` at the module path where `lib/gmail.py` imports them. Use a `tmp_path` fixture for `token_path` / `credentials_path`.
- For `send()`: patch `googleapiclient.discovery.build` and have the returned mock service expose a chain `service.users().messages().send().execute()` that returns a canned dict or raises `googleapiclient.errors.HttpError`. Construct `HttpError` instances with a fake `httplib2.Response` whose `status` is set as needed.
- For MIME verification: decode `raw` from base64-url, parse with `email.message_from_bytes`, then assert on header values and on `msg.get_payload()` returning a list with two parts (text + html).

---

## Implementation order (TDD)

1. Write `tests/lib/test_llm.py` first (all stubs above). Run — they fail with import errors.
2. Implement `scripts/lib/llm.py` until all tests in step 1 pass.
3. Write `tests/lib/test_gmail.py`. Run — fail.
4. Implement `scripts/lib/gmail.py` until all tests in step 3 pass.
5. Confirm `uv run pytest tests/lib/test_llm.py tests/lib/test_gmail.py` is green.

Steps 1–2 and 3–4 are independent; a sufficiently caffeinated implementer can do them in parallel, but they share nothing testing-wise.

## Acceptance gates for this section

- `uv run pytest tests/lib/test_llm.py tests/lib/test_gmail.py` exits 0.
- `python scripts/lib/gmail.py authorize` opens a browser, completes Google OAuth with the `gmail.send` scope, writes `config/token.json`. Re-running without scope changes does NOT re-prompt.
- The OpenAI strict-mode schema test in `tests/lib/test_csv_schema.py` (defined in section 02) passes for `SendResult`, `BounceRecord`, and any LLM response schemas declared in this section. If section-02's test file enumerates models by import, add `lib/gmail.py:SendResult` and `lib/gmail.py:BounceRecord` to its model list.
- No secrets in code: `lib/llm.py` reads `OPENAI_API_KEY` from env only; `lib/gmail.py` reads paths from env (`GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`) with sensible defaults.
- The scope-superset detection in `authorize()` is exercised by a unit test that constructs a token with `["gmail.send"]` and calls `authorize(scopes=["gmail.send","gmail.readonly"])` — the test asserts `InstalledAppFlow.run_local_server` is invoked.

## What this section deliberately does NOT do

- No bounce-parsing logic — that's section 12.
- No campaign-specific prompts (those live in section 06 for sourcing, section 07 for discovery, section 10 for composition).
- No daily-send-counter logic — that lives in `scripts/send_emails.py` (section 11). This section's `GmailClient.send()` just maps the quota-exceeded error to a typed exception; the counter logic is the caller's job.
- No LLM response cache (out of v1 scope).
- No alternative providers (Anthropic, etc.) — OpenAI only in v1, though the wrapper is structured to allow a second provider later.
- No retry logic in `GmailClient.send()` — the caller (M3 `send_emails.py`) owns retry semantics because they're entangled with the pessimistic counter. This module just raises `QuotaExceeded`.