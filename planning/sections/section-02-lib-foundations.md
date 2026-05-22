I have all the context I need. Now I'll generate the section content for section-02-lib-foundations.

# section-02-lib-foundations

This section implements the no-network, no-LLM portion of the cross-cutting library layer that every later milestone depends on. The five modules in this section are:

- `scripts/lib/brief.py` — Pydantic model + validation for `brief.yaml`
- `scripts/lib/csv_schema.py` — Pydantic row models + CSV read/write helpers (the M0 strict-mode gate)
- `scripts/lib/progress.py` — thread-safe, atomic, file-backed progress store
- `scripts/lib/rate_limit.py` — `RateLimiter` (token bucket) and `HourlyLimiter` (sliding window)
- `scripts/lib/dns_check.py` — MX / null-MX / A-record helpers with LRU cache
- `tests/conftest.py` — shared fixtures used by tests in this and every later section

This section deliberately excludes `lib/observability.py`, `lib/dedup.py`, `lib/llm.py`, `lib/gmail.py`, and the verifier modules — those belong to sections 03 and 04 (and 08). Section 05 adds the brief-hash helpers (`write_brief_hash` / `check_brief_hash`) on top of `progress.py`.

---

## 1. Background and invariants

Read these once. They apply to every module below.

### 1.1 Engine vs. campaign

The repo is split into two layers.

- **Engine** (`scripts/lib/`, `config/`, `templates/`, `tests/`) — stable code. Built once.
- **Campaign** (`campaigns/<YYYY-MM>_<slug>/`) — disposable, one folder per outreach run.

The interface between layers is `brief.yaml`. Engine code reads from a loaded `Brief`. **Nothing in the engine layer is allowed to hardcode segment-specific values** — not the segment definition, not the role priorities, not the value prop, not the rate limits. Everything segment-shaped is in the brief.

### 1.2 Pydantic-v2 + OpenAI strict-mode rules

Apply to every Pydantic model in the codebase (including those in `csv_schema.py` and `brief.py`):

- `model_config = ConfigDict(extra="forbid")` on every model.
- `Optional[X]` fields must have an explicit `default=None`. Plain `Optional[X]` without a default is Pydantic-OK but breaks OpenAI strict mode (which requires every property to appear in `required`, with `Optional` expressed as nullable type).
- A single test in `tests/lib/test_csv_schema.py` runs every model through OpenAI's strict-mode schema validator (see §3.6 below). **This test gates M0.** If any model fails this test, fix the model — don't disable the test.

### 1.3 Concurrency model

- `ProgressStore` is thread-safe via an internal `RLock`. Recommended pattern for later sections that use `ThreadPoolExecutor`: workers push results to a `queue.Queue`; the main thread is the sole writer of CSVs and the sole caller of `progress.mark()`. The `RLock` inside `ProgressStore` is defense-in-depth.
- Reads from CSV files are non-locking. Writes via `write_csv_row` are atomic (`.tmp` + `os.replace`).
- `data/`-directory locking (cross-process `fcntl.flock`) belongs to `lib/dedup.py` in section 03 and is NOT in scope here.

### 1.4 Exit codes (project-wide)

- 0 — success.
- 1 — refused operation (user error, re-invocable correctly).
- 2 — stage failure (pre-flight failed, halt condition, FAILED finish).
- 3 — **brief validation error**. `BriefValidationError` is converted to a structured JSON line on stderr by the script's main wrapper (see §2.2 below) and the process exits 3.

### 1.5 v1 scope guardrails

Do NOT add anything in this section that would support deferred features: no `List-Unsubscribe` schema fields, no warmup config, no Brave/Tavily verifier hooks, no reply-detection state, no follow-up state, no geo-filtering, no pattern-only email tier in any model. If you see these in another document, ignore them.

---

## 2. `scripts/lib/brief.py` — the brief contract

### 2.1 Purpose

Single source of truth for the shape of `brief.yaml`. Every stage script loads a brief through this module. Validation is strict — missing or wrong fields fail at brief-load time, not three stages later at send time.

### 2.2 Public interface

```python
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

def load(path: Path) -> Brief:
    """Read YAML from path, validate, return Brief.
    Raises BriefValidationError (with field, message, brief_path) on failure.
    Raises FileNotFoundError with a clean message if path doesn't exist."""

class BriefValidationError(Exception):
    """Carries .field, .message, .brief_path so the main-wrapper exit-3 contract
    can emit:
      {"error":"BriefValidationError",
       "field":"<dotted.path>",
       "message":"<reason>",
       "brief_path":"<absolute path>"}
    """
    def __init__(self, field: str, message: str, brief_path: Path): ...
```

### 2.3 Nested sections (sketch — full field list from `claude-spec.md §4`)

Each section is its own Pydantic model with `extra="forbid"`. Field-level constraints summarized:

- `TargetSection`: `segment: str` (required, non-empty), `include: list[str]` (≥1 item), `exclude: list[str]` (may be empty), `geography: str` (required), `target_domain_count: int` (>0; no default — interview Q1.1 said brief must specify).
- `WhoToContactSection`: `priority_roles: list[str]` (≥1), `deprioritize: list[str]`, `value_prop: str` (non-empty), `contacts_per_company: int` (≥1, ≤12 — `claude-spec.md §4` cap).
- `MessageSection`: `template: Path` (must exist on disk at load time), `personalize_first_name: bool` (default False), `from_name: str`, `from_gmail: str` (must look like an email — regex `^[^@\s]+@[^@\s]+\.[^@\s]+$`), `reply_to: Optional[str] = None`.
- `VerifierSection`: `chain: list[Literal["smtp_probe","web_citation","api_provider"]]` (≥1), `rate_per_sec: float` (default 0.5), `per_hour_cap: int` (default 50), `burst: int` (default 10), `greylist_retry: bool` (default True).
- `SendingSection`: `send_test_count: int` (default 10, ≥1), `send_rate_per_day: int` (≤2000 — safety cap), `throttle_seconds: float` (>0).
- `SafetySection`: `scope: Literal["this_campaign","all_campaigns"]` (default `all_campaigns`), miscellaneous safety toggles.

### 2.4 Validators (field-level)

- `slug` — must be kebab-case: `^[a-z0-9]+(-[a-z0-9]+)*$`. "Foo Bar" rejected.
- `send_rate_per_day` — `≤2000`. 5000 rejected.
- `priority_roles` — `len ≥ 1`.
- `contacts_per_company` — `1 ≤ x ≤ 12`.
- `template` — `Path(value).is_file()` must be true at load time. If not, raise with the offending path in the message.
- `from_gmail` — regex above.

Convert Pydantic `ValidationError` into `BriefValidationError`: pick the first error in the list, use its `loc` joined with `.` as `field`, its `msg` as `message`, pass through `brief_path`.

### 2.5 Tests (write FIRST, in `tests/lib/test_brief.py`)

Stub each test with a clear name and one-line docstring. Implementations are tiny once `load()` exists.

```python
# tests/lib/test_brief.py
# Test: load() with a complete valid brief.yaml returns a populated Brief.
# Test: missing required field target.segment → BriefValidationError naming "target.segment".
# Test: empty priority_roles list → validation error.
# Test: send_rate_per_day = 5000 → validation error (safety cap).
# Test: slug = "Foo Bar" → validation error.
# Test: unknown extra top-level field in YAML → validation error (extra="forbid").
# Test: template path that doesn't exist → validation error naming the path.
# Test: from_gmail that doesn't look like an email → validation error.
# Test: BriefValidationError has structured attributes (field, message, brief_path).
# Test: load() of a non-existent path → FileNotFoundError with a clean message.
# Test: contacts_per_company > 12 → validation error.
```

Fixture support — use the `sample_brief_yaml` fixture from `tests/conftest.py` (§7 below) and mutate copies for the failure cases.

---

## 3. `scripts/lib/csv_schema.py` — row models + I/O

### 3.1 Purpose

Canonical Pydantic row models for every CSV the pipeline reads or writes, plus thin read/write helpers. These models are also imported by later sections as the row types for `ThreadPoolExecutor` queues and as the JSON schemas passed to OpenAI structured outputs.

### 3.2 Row models

```python
class DomainRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: str
    domain: str           # lowercase, no scheme, no www, no path
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
    confidence: float     # 0.0 - 1.0, from LLM

class EmailRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    email: str
    company: str
    domain: str
    role: str
    category: str
    confidence: Literal["verified-smtp","verified-web","verified-api"]
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
    status: Literal["sent","quota_exceeded","skipped_suppressed","error"]
    error_message: Optional[str] = None

class SuppressionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    reason: Literal["hard_bounce","manual_optout","reply_optout"]
    source: str           # gmail_message_id or "manual"
    added_at: datetime

class MasterContactRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    name: str
    domain: str
    role: str
    first_seen_campaign: str
    first_seen_at: datetime
```

Every model: `extra="forbid"`, every `Optional[X]` carries `default=None`.

### 3.3 Read/write helpers

```python
def read_csv(path: Path, model: Type[BaseModel]) -> list[BaseModel]:
    """Read all rows. Construct each via model(**row_dict); ValidationError propagates."""

def write_csv_row(path: Path, row: BaseModel) -> None:
    """Append row. If file doesn't exist, write header first.
    Atomic via .tmp + os.replace (not strictly required for append, but the
    header-creation case must be atomic to avoid header-only files on crash)."""

def rewrite_csv(path: Path, rows: list[BaseModel]) -> None:
    """Full rewrite (used rarely; e.g., during dedup commit in section 03).
    Atomic via .tmp + os.replace."""
```

Column order = field declaration order on the model. Use `model.model_fields` (Pydantic v2) to get the canonical list. `datetime` fields serialize as ISO-8601; deserialize with `datetime.fromisoformat`.

### 3.4 OpenAI strict-mode validator (the M0 gate)

In `tests/lib/test_csv_schema.py`, for every row model AND every LLM-response model declared anywhere in this section, generate the JSON schema that OpenAI receives and assert:

- Top-level schema has `additionalProperties: false`.
- Every property listed in `properties` also appears in `required` (OpenAI strict-mode rule — even `Optional` fields must be `required` and expressed as nullable types).
- Recursively true for every nested object schema.

Helper for getting the schema: either `openai.lib._tools.pydantic_function_tool(model)` if available in the installed `openai` version, or build it manually via `model.model_json_schema()` and post-process. Either way, the test must reflect what OpenAI's `responses.parse` actually sees.

Failure of any model in this test blocks M0 completion.

### 3.5 Tests (write FIRST, in `tests/lib/test_csv_schema.py`)

```python
# tests/lib/test_csv_schema.py
# For each model (DomainRow, ContactRow, EmailRow, OutboxRow, SentLogRow,
# SuppressionRow, MasterContactRow):
#   Test: write_csv_row then read_csv round-trips identically.
#   Test: appending to existing CSV → header not duplicated.
#   Test: invalid row (missing required field) → ValidationError at construct time.
#   Test: extra="forbid" — unknown field rejected at construct time.
#   Test: Optional[X] field with default=None — missing in CSV → None, not error.
# Test: OpenAI strict-mode compliance — every model's JSON schema has
#       additionalProperties:false and every property in required. THIS GATES M0.
```

Parametrize the round-trip tests over all seven models; one parametrized function covers all of them.

---

## 4. `scripts/lib/progress.py` — resume machinery

### 4.1 Purpose

File-backed key→status store. One file per stage per campaign at `campaigns/<slug>/progress/<stage>.json`. Drives `--resume` by recording which keys have terminal vs retriable statuses.

### 4.2 Public interface

```python
class ProgressStore:
    """File-backed progress tracker. One per stage per campaign.
    Thread-safe via an internal RLock; safe to share across workers."""

    def __init__(self, path: Path):
        """Path to progress JSON file (e.g., campaigns/x/progress/source_domains.json).
        Does NOT auto-load; caller must call load()."""

    def load(self) -> None:
        """Read existing JSON if present. If a sibling .tmp exists, ignore it
        (crash mid-write). If file missing, start empty."""

    def mark(self, key: str, status: str, **extras) -> None:
        """Record outcome for a key. Read-modify-write under self._lock,
        then atomic .tmp + os.replace. extras stored as additional fields
        in the JSON value (e.g., count=12, error='timeout')."""

    def is_done(self, key: str) -> bool:
        """True iff key was marked with a terminal status. Terminal statuses
        are declared by the caller via terminal_statuses (see __init__ kwarg
        on the version below) — defaults to {'ok'}.

        Implementation note: ProgressStore stores statuses opaquely; the
        terminal/retriable distinction is configured per-stage via the
        constructor. The caller passes the set."""

    def is_retriable(self, key: str) -> bool:
        """True iff key is recorded but with a non-terminal status."""

    def keys(self) -> Iterator[str]:
        """All keys already processed, in insertion order."""
```

Constructor signature, full:

```python
def __init__(self,
             path: Path,
             terminal_statuses: set[str] = frozenset({"ok"}),
             retriable_statuses: set[str] = frozenset({"worker_exc"})): ...
```

### 4.3 Atomic write contract

Inside `mark()`, while holding `self._lock`:

1. Compute the new JSON dict from current in-memory state.
2. `tmp = self.path.with_suffix(self.path.suffix + ".tmp")` (so `source_domains.json.tmp`).
3. `tmp.write_text(json.dumps(state, indent=2))`.
4. `os.replace(tmp, self.path)`.

Never write partial files. Never lose updates across threads. `load()` explicitly ignores any pre-existing `.tmp` file (it's debris from a prior crash).

### 4.4 What is NOT in this section

The brief-hash helper functions (`write_brief_hash(path, brief_bytes)` and `check_brief_hash(path, brief_bytes) -> bool`) belong to **section 05** alongside the no-op stage that first exercises the invariant. Leave a TODO comment in `progress.py` noting where they will live; don't implement them here.

### 4.5 Tests (write FIRST, in `tests/lib/test_progress.py`)

```python
# tests/lib/test_progress.py
# Test: new ProgressStore on non-existent path → empty after load();
#       writes file on first mark().
# Test: mark("k1","ok") then is_done("k1") is true; is_done("k2") is false.
# Test: reload from disk preserves state.
# Test: terminal vs retriable status — is_retriable("worker_exc") true,
#       is_retriable("ok") false.
# Test: lost-update under concurrency (review issue #1):
#       100 threads each call mark(f"k{i}", "ok") → final progress.json
#       has exactly 100 keys.
# Test: concurrent mark() on the SAME key from two threads → final state
#       is one of the two writes, never half-written or absent.
# Test: crash simulation — write .tmp without rename → on next load(),
#       .tmp ignored, old file used.
# Test: keys() returns all processed keys in insertion order.
# Test: extras passed to mark() are preserved in the JSON value.
```

The 100-thread test uses `concurrent.futures.ThreadPoolExecutor(max_workers=100)` plus a barrier so all threads hit `mark()` concurrently. This proves the `RLock` plus atomic-write actually prevents lost updates — the #1 reviewer issue from the planning phase.

The brief-hash test from `claude-plan-tdd.md §2.2` is **deferred to section 05**.

---

## 5. `scripts/lib/rate_limit.py` — throttles

### 5.1 Purpose

Two reusable blocking limiters used by later stages. `RateLimiter` (token-bucket per-second) is used by the SMTP probe and the Gmail sender. `HourlyLimiter` (sliding-window per-hour) caps SMTP probing to stay under Spamhaus thresholds.

### 5.2 Public interface

```python
class RateLimiter:
    """Token-bucket. Blocking acquire(). Burst-tolerant.
    Uses time.monotonic() for clock; replaceable in tests via a clock kwarg."""
    def __init__(self, rate_per_sec: float, burst: int = 1,
                 clock: Callable[[], float] = time.monotonic): ...
    def acquire(self) -> None: ...

class HourlyLimiter:
    """Sliding-window hourly cap. Blocks until under cap.
    Uses time.monotonic() for clock; replaceable in tests via clock kwarg.
    Keeps timestamps of every acquire() in a deque; on acquire, prunes
    entries older than 3600s, then either records-and-returns or sleeps
    until the oldest entry ages out."""
    def __init__(self, per_hour: int, burst: int = 1,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep): ...
    def acquire(self) -> None: ...
```

The `clock` and `sleep` kwargs are how tests inject a mocked monotonic clock — calling `sleep()` advances a counter that the mocked `clock()` returns. Real production code uses defaults.

### 5.3 Default values (research-aligned, used by brief defaults)

The brief's `verifier` section defaults to these values:

- `rate_per_sec = 0.5`
- `per_hour_cap = 50`
- `burst = 10`

These belong in `brief.py`'s `VerifierSection` defaults, not hardcoded here.

### 5.4 Tests (write FIRST, in `tests/lib/test_rate_limit.py`)

```python
# tests/lib/test_rate_limit.py
# Test: RateLimiter(2.0) — 4 acquires take ~2.0s ±0.1s (uses mocked monotonic).
# Test: HourlyLimiter(per_hour=3, burst=1) — first 3 immediate; 4th blocks ~1200s
#       (mocked clock).
# Test: Sustained-rate (review issue #12) — HourlyLimiter(30/hr, burst=5):
#       60 acquires take ≥ ~110 minutes with mocked clock.
# Test: Mixed limiter — RateLimiter(0.5) + HourlyLimiter(50, burst=10) — first 10
#       unblocked, then converges to ~50/hr.
# Test: RateLimiter wakes up correctly after a long pause (clock skip simulation).
```

The "mocked clock" pattern: write a tiny `FakeClock` helper in `conftest.py` that tracks "now" as a float; the test's `sleep()` callable advances "now"; assertions are made on the total elapsed value, not real wall-clock time.

---

## 6. `scripts/lib/dns_check.py` — MX validation

### 6.1 Purpose

DNS helpers used by `source_domains.py`, `discover_contacts.py`, and `verifiers/smtp_probe.py` to decide whether a domain can plausibly receive mail.

### 6.2 Public interface

```python
def mx_records(domain: str, timeout: float = 5.0) -> list[str]:
    """Return MX hostnames sorted by preference (lowest preference first).
    Empty list on NoAnswer or NXDOMAIN. Raises dns.exception.Timeout (caller
    decides what to do)."""

def has_mail(domain: str) -> bool:
    """True if the domain can plausibly receive mail:
       - Has MX records, AND those records are not RFC 7505 null MX, OR
       - Has no MX but has an A record (RFC 5321 fallback).
    Catches Timeout internally and returns False."""

def is_null_mx(domain: str) -> bool:
    """True if MX is RFC 7505 null (single MX with priority 0, target '.').
    False if no MX or multiple MX."""
```

### 6.3 LRU cache

Wrap `mx_records`, `has_mail`, and `is_null_mx` with `functools.lru_cache(maxsize=1024)` (or implement an explicit small cache class — `lru_cache` is fine for a process-lifetime cache). The cache is keyed by domain. This avoids re-resolving the same domain dozens of times within a single stage. Domain inputs are lowercased before lookup (the caller is expected to normalize, but be defensive).

### 6.4 Tests (write FIRST, in `tests/lib/test_dns_check.py`)

```python
# tests/lib/test_dns_check.py
# Test: mx_records — mock dns.resolver.resolve to return canned MX → returns
#       sorted hostnames.
# Test: mx_records — mock NoAnswer → returns [].
# Test: mx_records — mock NXDOMAIN → returns [].
# Test: mx_records — Timeout → raises (caller handles).
# Test: is_null_mx — mock priority=0, target='.' → True.
# Test: has_mail — MX present → True.
# Test: has_mail — no MX but A record present → True.
# Test: has_mail — no MX, no A → False.
# Test: has_mail — null MX → False.
# Test: LRU cache hits — second call for same domain doesn't re-resolve
#       (assert resolver mock called exactly once across two calls).
```

Mock `dns.resolver.resolve` at the module level using `pytest-mock`'s `mocker.patch("scripts.lib.dns_check.dns.resolver.resolve", ...)`. For each test, build a fake answer object that quacks like `dns.resolver.Answer` (has the MX records with `.preference` and `.exchange` attributes).

---

## 7. `tests/conftest.py` — shared fixtures

Lives at `tests/conftest.py` (not under `tests/lib/`) so every test directory can pick it up automatically.

Fixtures to define in this section (others are added in later sections):

```python
@pytest.fixture
def tmp_campaign_dir(tmp_path: Path) -> Path:
    """Returns an empty tmp directory shaped like a campaign:
       <tmp>/brief.yaml (absent — caller writes it)
       <tmp>/progress/   (created)
    Caller fills in brief.yaml as needed."""

@pytest.fixture
def sample_brief_yaml() -> str:
    """Returns a string containing a complete, valid brief.yaml.
    Tests that need an invalid brief mutate (yaml.safe_load → mutate → yaml.safe_dump)
    and write the result to tmp_campaign_dir / 'brief.yaml'."""

@pytest.fixture
def sample_brief(tmp_campaign_dir, sample_brief_yaml) -> Brief:
    """Writes sample_brief_yaml to tmp_campaign_dir/brief.yaml and returns
    the loaded Brief. Reused everywhere a valid Brief is needed."""

@pytest.fixture
def fake_clock():
    """Tiny mutable clock for rate-limit tests:
       class FakeClock:
           t: float = 0.0
           def now(self) -> float: return self.t
           def sleep(self, s: float) -> None: self.t += s
    Used by both rate_limit and observability tests."""
```

The `sample_brief_yaml` fixture must produce a YAML that:
- Has all required sections fully populated with valid values.
- Has `template:` pointing to an actual file the fixture also creates in `tmp_campaign_dir`.
- Uses `slug: "test-campaign"`, `target_domain_count: 20`, `send_rate_per_day: 100`, `send_test_count: 5`, `contacts_per_company: 3`, `scope: this_campaign`.
- Uses `from_gmail: "test@example.com"`.

Add a stub fixture for `fake_dns_resolver` (to be fleshed out in `dns_check` tests). Add stubs for `fake_llm_client` and `fake_gmail_client` but mark them with `pytest.fixture` and `pytest.skip("filled in section 04")` so future sections can plug them in without conflict.

---

## 8. File layout produced by this section

```
scripts/lib/__init__.py             # empty file, makes lib a package
scripts/lib/brief.py
scripts/lib/csv_schema.py
scripts/lib/progress.py
scripts/lib/rate_limit.py
scripts/lib/dns_check.py
tests/__init__.py                   # empty
tests/conftest.py                   # shared fixtures
tests/lib/__init__.py               # empty
tests/lib/test_brief.py
tests/lib/test_csv_schema.py
tests/lib/test_progress.py
tests/lib/test_rate_limit.py
tests/lib/test_dns_check.py
```

`scripts/__init__.py` is created in section 01.

---

## 9. Implementation order (TDD)

For each module, write tests first, then implementation. Recommended order within this section:

1. **`brief.py`** — depends on no other lib module. Write `tests/lib/test_brief.py`, then implement. Add the YAML fixture to `conftest.py` here.
2. **`csv_schema.py`** — depends on nothing. Write `tests/lib/test_csv_schema.py` including the strict-mode validator test, then implement. **Do not move on until the strict-mode test passes** — it's the M0 gate.
3. **`progress.py`** — depends on nothing. Write `tests/lib/test_progress.py` including the 100-thread concurrency test, then implement.
4. **`rate_limit.py`** — depends on nothing. Write `tests/lib/test_rate_limit.py` using the `fake_clock` fixture, then implement.
5. **`dns_check.py`** — depends on `dnspython`. Write `tests/lib/test_dns_check.py` with mocked `dns.resolver`, then implement.

Run `uv run pytest tests/lib/` after each module — all tests for that module should be green before moving to the next.

---

## 10. Acceptance criteria for this section

- `uv run pytest tests/lib/test_brief.py tests/lib/test_csv_schema.py tests/lib/test_progress.py tests/lib/test_rate_limit.py tests/lib/test_dns_check.py` — all green.
- The OpenAI strict-mode test in `test_csv_schema.py` is green for every row model.
- The 100-thread `ProgressStore` concurrency test is green (review issue #1 closed).
- `tests/conftest.py` exposes `tmp_campaign_dir`, `sample_brief_yaml`, `sample_brief`, and `fake_clock` fixtures that later sections can import.
- No imports from `lib/observability`, `lib/dedup`, `lib/llm`, `lib/gmail`, or `lib/verifiers/` exist anywhere in this section — those modules don't exist yet.
- No network calls (DNS, HTTP, OpenAI, Gmail) happen during `uv run pytest tests/lib/` — all dependencies are mocked or use file I/O only.

---

## 11. Things to deliberately NOT do in this section

- Do not implement `lib/observability.py`, `lib/dedup.py`, `lib/llm.py`, `lib/gmail.py`, or any verifier — those are sections 03, 04, and 08.
- Do not implement `write_brief_hash` / `check_brief_hash` — section 05.
- Do not implement `fcntl.flock`-based locking on `data/` files — section 03 (`lib/dedup.py`).
- Do not add CLI entry points or `main()` functions to any lib module — libs are imported, not executed.
- Do not add a `lib/__init__.py` re-export of public names — keep imports explicit (`from scripts.lib.brief import Brief, load`).
- Do not add support for `List-Unsubscribe`, postal address, warmup config, reply detection, follow-up bumps, geo filtering, or pattern-only email fields to any schema — out of v1 scope.