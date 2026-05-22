# Outreach Bot — Implementation Plan

This plan describes how to build a reusable cold-outreach automation tool in `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/`. The original design lives in `planning/outreach-bot-design-and-plan.md`; this plan supersedes it where the two disagree (see `claude-interview.md` for resolved questions).

The plan is organized into:
1. **Big picture** — what we're building and the single most important design idea.
2. **Architectural primitives** — the cross-cutting libraries every stage depends on.
3. **Milestones M0–M4** — concrete, ordered work blocks. Each is independently shippable.
4. **Acceptance + risks** — how we know we're done; what could go wrong.

The plan deliberately stays at the design level: directory layout, types, function signatures, test lists, and acceptance criteria. It does **not** contain full implementations — those are written in the build phase.

---

## 1. Big picture

### 1.1 What we're building

A Python CLI tool, driven by Claude Code, that runs a cold-outreach campaign end to end. The user describes a target in one sentence ("contact medium-sized retailers about AI shopping agents"); Claude Code interviews to fill a `brief.yaml`; then the pipeline:

1. Sources ~N domains in the segment (Stage 1).
2. Finds high-leverage people at each domain (Stage 2).
3. Verifies candidate email addresses (Stage 3).
4. Composes per-recipient messages from a template (Stage 4).
5. Sends a 10-email test batch, pauses for the user's go/no-go, then sends the rest under a daily cap (Stage 5).
6. A standalone bounce-poller adds hard bounces to a suppression list (Stage 6 — thin in v1).

The user gets ambient progress (a live `status.md`, an append-only `activity.log`, and milestone lines posted to the chat) and is only required to intervene once: after the test batch.

### 1.2 The single most important design idea: engine vs. campaign

The repo is split into two layers, intentionally.

**Engine** — `CLAUDE.md`, `playbooks/`, `scripts/`, `scripts/lib/`, `config/`, `templates/`, `data/`, `tests/`. This is the stable code-and-knowledge layer. You build it once and rarely touch it.

**Campaign** — one folder per run under `campaigns/<YYYY-MM>_<slug>/`. Contains the campaign's brief, per-stage CSV outputs, progress files, and observability files. Disposable.

The interface between layers is `brief.yaml`. Every script in the engine reads from a loaded brief; **nothing in the engine layer is allowed to hardcode segment-specific values** — not the segment definition, not the role priorities, not the value prop, not the rate limits. Lifting all of that out of the prior-art scripts is ~80% of what turns the user's one-off into a general tool.

### 1.3 What's in v1, what's deferred

This plan implements v1 only. Items explicitly deferred to v2+ (with reasons, per the interview):

- Reply detection and auto follow-up bumps — user reads inbox manually for now.
- Custom-opening-line LLM personalization — replaced by narrower first-name fill.
- `List-Unsubscribe` / `List-Unsubscribe-Post` headers, postal address, CAN-SPAM scaffolding — user opted out.
- Automatic Gmail warmup ramp — user opted out.
- LLM response cache — user opted out.
- Non-OpenAI search backends (Brave, Tavily, Serper) — kept on OpenAI hosted `web_search`.
- Pattern-only email tier — dropped entirely (we never emit pattern-only rows).
- HTTPS one-click unsubscribe endpoint — out of v1.
- Geographic recipient filtering (DE/AT/FR exclusion etc.) — out of v1.
- Campaign report generation — manual SQL-on-CSV for now.

### 1.4 Tech stack

- **Python 3.12** (uv-managed venv).
- **OpenAI Python SDK** (`openai` ≥ 1.50). Structured Outputs (`responses.parse` with `text_format=PydanticModel`, `strict=true`). Hosted `web_search` tool.
- **Pydantic v2** for all CSV row schemas, LLM response schemas, brief schema.
- **`google-api-python-client`** + **`google-auth-oauthlib`** for Gmail.
- **`dnspython`** for MX/A lookups.
- **`pyyaml`** for brief loading.
- **`pytest`** + **`aiosmtpd`** (or `smtplib` mock) for tests.
- Pure-Python stdlib elsewhere (`smtplib`, `socket`, `csv`, `email.message`, `base64`).

No web framework. No database. The CSV files are the persistence layer.

---

## 2. Architectural primitives

These libraries live in `scripts/lib/` and are dependencies of every stage. They're the bulk of M0.

### 2.1 `lib/brief.py` — the spec contract

Pydantic model for the brief file (see `claude-spec.md §4` for the schema). Single source of truth for what a brief looks like.

Public interface:
```python
class Brief(BaseModel):
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
    """Load + validate a brief.yaml. Raises BriefValidationError on failure."""
```

Nested sections each get their own Pydantic model with field-level validators (e.g., `slug` must be kebab-case; `target_domain_count > 0`; `send_rate_per_day ≤ 2000` for safety; `template` path must exist; `from_gmail` must look like an email).

Validation is strict — missing required fields fail at load time, not at send time. The error message names the field and what's wrong; example: `"BriefValidationError: who_to_contact.priority_roles is empty; provide at least one role."`.

### 2.2 `lib/progress.py` — resume machinery

Lifted from the prior art (`scrape-retailers.py` and `find-emails-bulk.py`) and generalized. **Thread-safe**: every `ProgressStore` owns a `threading.RLock`, held across the read-modify-write of every `mark()`.

```python
class ProgressStore:
    """File-backed progress tracker. One per stage per campaign.
    Thread-safe via an internal RLock; safe to share across workers."""

    def __init__(self, path: Path): ...
    def load(self) -> None: """Read existing progress.json if present."""
    def mark(self, key: str, status: str, **extras) -> None:
        """Record outcome for a key. Read-modify-write under the lock,
        then atomic .tmp-rename write."""
    def is_done(self, key: str) -> bool: """True if key has terminal status."""
    def is_retriable(self, key: str) -> bool:
        """True if key has a non-terminal status (e.g., worker_exc) and should
        be retried on --resume."""
    def keys(self) -> Iterator[str]: """All keys already processed."""
```

Internal contract:
- One file per stage at `campaigns/<slug>/progress/<stage>.json`.
- File is a JSON dict keyed by string (URL, domain, candidate email, outbox row id depending on stage).
- Every value is `{"status": "<enum>", ...extras}`.
- All writes hold the instance's `RLock`; write goes through `_atomic_write()`: `path.with_suffix(".tmp").write_text(...)` → `os.replace(tmp, path)`. No partial files; no lost updates across threads.
- "Terminal" statuses depend on stage; see each stage's enum below. Each stage script declares which statuses are terminal (e.g., `ok`, `excluded`) vs retriable (e.g., `worker_exc`, `discovery_fail`).

**Concurrency model for stages that use `ThreadPoolExecutor` (M2):** the recommended pattern is single-writer: workers compute results and push to a `queue.Queue`; the main thread is the sole consumer that calls `progress.mark()` and `csv.write_csv_row()`. This avoids needing a lock per CSV file and matches the prior art's pattern. The `RLock` inside `ProgressStore` is a defense-in-depth — even if a future caller violates the single-writer convention, the store stays consistent.

### 2.3 `lib/observability.py` — live progress

Cross-cutting reporting layer. **Split into two cooperating classes:**

- **`CampaignObserver`** — singleton per campaign. Owns the campaign-level header in `status.md` (which stage is current, completed stages, total spend across stages). Reads + writes a small `campaigns/<slug>/observer_state.json` so cross-stage state survives process boundaries.
- **`StageObserver`** — one per stage invocation. Owns the stage-specific section of `status.md` and the `activity.log` lines for this stage. Holds a reference to its parent `CampaignObserver` so total cost can be rolled up.

```python
class CampaignObserver:
    def __init__(self, campaign_dir: Path): ...
    def stage_complete(self, stage: str, summary: dict) -> None:
        """Record stage completion + summary; rewrites status.md preserving prior stages."""
    def total_cost(self) -> float: """Sum of all stage costs to date."""

class StageObserver:
    def __init__(self, campaign_obs: CampaignObserver, stage: str,
                 cadence_items: int = 50, cadence_seconds: int = 120): ...

    def stage_start(self) -> None:
        """Mark stage RUNNING in status.md, log a 'stage X starting' event."""

    def event(self, message: str, level: Literal["info","warn"] = "info") -> None:
        """Append a timestamped line to activity.log. Always emits.
        NOTE: 'error' is NOT a level here — transient errors use 'warn',
        terminal failures use finish(status='FAILED'). This avoids the
        ambiguity of 'one error → FAILED stage'."""

    def tick(self, counters: dict[str, int | float | str]) -> None:
        """Update counters. If cadence threshold crossed, emit a milestone:
           - Append [stage] ... line to activity.log
           - Print [stage] ... line to stdout
           - Rewrite stage section of status.md
        """

    def finish(self, status: Literal["COMPLETED","FAILED"], summary: dict) -> None:
        """Terminal call. Updates campaign-level state.
        On FAILED, prints traceback location to stdout and exits with status code."""
```

Semantics:
- `event(level="warn")` is a transient signal (rate-limit retry, greylist retry, etc.). It does NOT fail the stage.
- `finish(status="FAILED", ...)` is the only way a stage transitions to FAILED. The script's main loop catches its own unhandled exceptions and calls `finish(FAILED)` in a `finally` block before re-raising.
- `CampaignObserver.stage_complete()` is what `finish(COMPLETED)` calls internally. After completion, the stage's section in `status.md` is replaced with a one-line "COMPLETED" summary; the next stage's section appears below.

`status.md` template (rewritten from in-memory state on every milestone):
```
# <slug> — <STATUS> (stage N of 5: <stage name>)

Domains sourced:   1,491 / 1,500  ✅
Contacts found:    612 companies processed (41%)
Emails verified:   1,134 verified
Cost so far:       $18.40
Last event:        2026-05-21 14:03  verified aforch@huckberry.com
ETA this stage:    ~22 min
```

`activity.log` format:
```
2026-05-21T14:03:21.105Z  [verify]  INFO   verified aforch@huckberry.com
2026-05-21T14:03:22.901Z  [verify]  INFO   milestone: 612/1491 (41.0%) verified=1134 catchall=148 cost=$18.40 elapsed=22m
2026-05-21T14:03:23.412Z  [verify]  WARN   greylist retry scheduled for foo@bar.com (90s)
```

Cadence rule: emit a milestone when either (a) `current_count - last_emit_count ≥ cadence_items`, OR (b) `time.monotonic() - last_emit_time ≥ cadence_seconds`. Stage scripts can override the per-stage defaults.

### 2.4 `lib/dedup.py` — cross-campaign suppression + dedup

```python
class Deduper:
    def __init__(self, scope: Literal["this_campaign","all_campaigns"]): ...
    def load_global(self) -> None:
        """Load data/master_contacts.csv + data/suppression.csv (under shared file lock)."""
    def is_suppressed(self, email_or_domain: str) -> bool: ...
    def is_known(self, email_or_domain: str) -> bool:
        """True if seen in any prior campaign (only checked if scope=all_campaigns)."""
    def append_contact(self, email: str, domain: str, name: str, role: str,
                       campaign_slug: str) -> None:
        """Append a single row to data/master_contacts.csv. Acquires fcntl.flock
        (exclusive) on the file. Plain open(path, 'a') append; the lock serializes
        concurrent appenders. Returns without rewriting the file."""
    def append_suppressed(self, email: str, reason: str, source: str) -> None:
        """Append a single row to data/suppression.csv. Same lock model."""
    def reload(self) -> None:
        """Re-read both files under shared lock (used by long-running send loops
        that want to pick up bounces added by a concurrent poll_bounces.py)."""
```

**Concurrency rules for `data/`:**
- All writes to `data/master_contacts.csv` and `data/suppression.csv` use `fcntl.flock(fd, LOCK_EX)` for the duration of the append. Reads use `LOCK_SH`.
- Appends are single-row, plain `open(path, "a")`. We do NOT rewrite the whole file on every send (the prior plan said "atomic via .tmp+rename" which was wrong for an append-only growing file).
- **Documented constraint:** only one `send_emails.py` may run per machine at a time. The script writes a `data/.send.pid` lockfile on startup (via `fcntl.flock` on a sentinel file) and exits with a clear message if another instance holds it. `poll_bounces.py` uses a separate `data/.poll.pid` lockfile.
- Two `send_emails.py` running on DIFFERENT campaigns is still one process at a time — the constraint is per-machine, not per-campaign, because both processes are writing to the shared `master_contacts.csv` / `suppression.csv` and want to pick up each other's appends.

Suppression is **always** global — even with `scope=this_campaign`, suppression is shared. The scope flag only affects dedup against `master_contacts.csv`.

### 2.5 `lib/dns_check.py` — MX validation

```python
def mx_records(domain: str, timeout: float = 5.0) -> list[str]:
    """Return MX hostnames sorted by preference. Empty list if none."""

def has_mail(domain: str) -> bool:
    """True if the domain can plausibly receive mail (MX OR fallback A; not null MX)."""

def is_null_mx(domain: str) -> bool:
    """True if MX is RFC 7505 null (priority 0, target '.')."""
```

In-memory LRU cache (size 1024) to avoid re-resolving the same domain within a stage.

### 2.6 `lib/llm.py` — OpenAI wrapper

The thinnest possible wrapper around `openai.OpenAI` that gives us: tiered model cascade, web-search tool, structured outputs, retry-on-429, cost tracking. **Distinguishes refusal (don't retry) from empty output (retry/escalate).**

```python
@dataclass
class ParseResult:
    parsed: BaseModel | None
    refused: bool             # True only if model safety-refused
    refusal_text: str         # filled when refused=True
    low_confidence: bool      # True if any field named 'confidence' < threshold
    cost: CostReport

@dataclass
class CostReport:
    model: str
    input_tokens: int
    output_tokens: int
    web_search_calls: int
    usd: float

class LLMClient:
    def __init__(self,
                 tier1: str = "gpt-4.1-mini",
                 tier2: str = "gpt-5",
                 fallbacks: list[str] = ["gpt-5.2","gpt-5","gpt-4.1"],
                 low_confidence_threshold: float = 0.4):
        """Probe fallbacks at init; use first reachable as 'available model'.
        tier1/tier2 govern the cascade; fallbacks govern model-availability."""

    def parse(self,
              messages: list[dict],
              text_format: Type[BaseModel],
              *, tools: list[dict] = None,
              tier: Literal["tier1","tier2"] = "tier1",
              max_retries: int = 3,
              temperature: float = 0.0) -> ParseResult:
        """Call responses.parse with structured outputs.
           - On 429: exp-backoff 1s, 2s, 4s, ..., max 32s + jitter.
           - On model refusal (resp.output[0].refusal set): ParseResult(parsed=None,
             refused=True, ...). Caller does NOT retry/escalate.
           - On empty output_parsed (output present but no parse): ParseResult(parsed=None,
             refused=False, ...). Caller MAY retry/escalate.
           - On low confidence (any 'confidence' field < threshold): ParseResult(parsed=instance,
             low_confidence=True, ...). Caller MAY escalate."""

    def cascade(self,
                messages: list[dict],
                text_format: Type[BaseModel],
                *, tools: list[dict] = None,
                temperature: float = 0.0) -> ParseResult:
        """Try tier1. If tier1 result is .parsed=None AND .refused=False,
        try tier2 (model returned nothing). If tier1 is .low_confidence=True,
        also try tier2 and prefer the higher-confidence result. Refusal at tier1
        does NOT escalate. Cost accumulates."""
```

**Strict-mode constraints applied to every Pydantic schema in §2.8 and stage-specific schemas:**
- Every `Optional[X]` field MUST have `default=None`. Plain `Optional[X]` (no default) is a Pydantic-OK pattern but breaks OpenAI strict mode.
- `model_config = ConfigDict(extra="forbid")` on every model.
- A test in `tests/lib/test_csv_schema.py` runs every schema through `openai.lib._tools.pydantic_function_tool` (or the equivalent helper for `responses.parse`) and asserts the schema is accepted by OpenAI's strict-mode validator. **This test gates M0.**

Cost calculation reuses prior-art constants but is per-model:
```python
COSTS = {
    "gpt-4.1-mini": {"input_per_m": 0.15, "output_per_m": 0.60},
    "gpt-5": {"input_per_m": 10.0, "output_per_m": 30.0},
    "gpt-5.2": {"input_per_m": 5.0, "output_per_m": 20.0},
}
COST_PER_WEB_SEARCH = 0.025
```

### 2.7 `lib/gmail.py` — Gmail OAuth + send + bounce-poll

```python
def authorize(credentials_path: Path, token_path: Path,
              scopes: list[str]) -> Credentials:
    """Run OAuth flow if no token; refresh if expired. Returns valid creds.
       SCOPES used by this tool:
         https://www.googleapis.com/auth/gmail.send       (Stage 5)
         https://www.googleapis.com/auth/gmail.readonly   (Stage 6)

       Scope-superset check: if existing token.json has scopes [X] but caller
       requests scopes [X, Y], the OAuth refresh will FAIL silently with a
       permission-denied error on use. To prevent that, this function:
         1. Loads token.json if present and inspects creds.scopes.
         2. If requested_scopes is NOT a subset of existing scopes:
            - Prints: 'Gmail token has scopes [X]; required [X, Y]. Re-authorizing.'
            - Deletes token.json.
            - Runs InstalledAppFlow.run_local_server() to get a new token with
              the union of scopes.
       This is invoked once at the start of every script that touches Gmail."""

class GmailClient:
    def __init__(self, creds: Credentials): ...
    def send(self, to: str, *, subject: str, body_html: str, body_plain: str,
             from_address: str, from_name: str, reply_to: str,
             headers: dict[str,str] | None = None) -> SendResult:
        """Build MIME, base64url-encode, POST to messages.send.
           Returns SendResult with gmail_message_id + thread_id.
           Raises QuotaExceeded on 429 or 'Daily user sending limit exceeded'."""

    def list_bounces(self, since_message_id: str | None = None) -> list[BounceRecord]:
        """Query: from:mailer-daemon AND subject:'Delivery Status Notification (Failure)'
           Parse Final-Recipient: header from each bounce body.
           Return BounceRecord with original recipient, bounce date, gmail_message_id."""
```

`SendResult`, `QuotaExceeded`, `BounceRecord` are small Pydantic / dataclass models in this file.

### 2.8 `lib/csv_schema.py` — row models

One Pydantic model per CSV. These are the canonical column definitions for the whole pipeline. **All Optional fields use `default=None` (required for OpenAI strict mode). All models set `extra="forbid"`.**

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

All other Pydantic models in the codebase (LLM response schemas in §4, §5, §6) follow the same rules: `Optional` fields get `default=None`, models get `extra="forbid"`.

Read/write helpers:
```python
def read_csv(path: Path, model: Type[BaseModel]) -> list[BaseModel]: ...
def write_csv_row(path: Path, row: BaseModel) -> None:
    """Append row. If file doesn't exist, write header first. Atomic via .tmp+rename."""
def rewrite_csv(path: Path, rows: list[BaseModel]) -> None:
    """Full rewrite (used by dedup commit)."""
```

### 2.9 `lib/rate_limit.py` — throttles

```python
class RateLimiter:
    """Token-bucket. blocking acquire()."""
    def __init__(self, rate_per_sec: float): ...
    def acquire(self) -> None: ...

class HourlyLimiter:
    """Sliding-window hourly cap. acquire() blocks until under cap."""
    def __init__(self, per_hour: int, burst: int): ...
    def acquire(self) -> None: ...
```

Both are reused: `RateLimiter` for SMTP-probe rate, `HourlyLimiter` for SMTP-per-hour cap (defends Spamhaus thresholds), `RateLimiter` again for Gmail sends (via `throttle_seconds`).

**Default values, revised from research §B.2** (Spamhaus thresholds make the prior art's 3.0/sec too aggressive for sustained runs):
- `verifier.rate_per_sec`: 0.5 (allows brief bursts; the hourly cap is the real constraint).
- `verifier.per_hour_cap`: 50 (well under Spamhaus's ~100/hr flagging threshold for static IPs).
- `verifier.burst`: 10 (lets short campaigns finish quickly; long ones converge to the hourly rate).

**Estimated-time pre-flight in `verify_emails.py`:** at brief-load, the script computes `len(contacts) / per_hour_cap` and prints a warning if > 8 hours: `"Estimated verification time: ~45h. Consider enabling api_provider (config/verifiers.yaml) or splitting the campaign."`. This is informational, not blocking — the user can proceed if they want.

**Test additions in `tests/lib/test_rate_limit.py`** (in addition to existing tests):
- Sustained-rate test: 60 `acquire()` calls against `HourlyLimiter(per_hour=30, burst=5)` should take ≥ 1 hour using a mocked monotonic clock. Verifies the cap actually binds across time, not just instantaneous bursts.
- Mixed limiter: `RateLimiter(0.5) + HourlyLimiter(50, burst=10)` — first 10 are unblocked (burst), then settles to ~50/hr.

### 2.10 `lib/verifiers/base.py` — verifier interface

```python
class VerificationResult(BaseModel):
    status: Literal["accepted","catchall","rejected","unknown"]
    confidence: Literal["verified-smtp","verified-web","verified-api",""]
    source_url: str       # "https://verified-smtp/" or real URL or ""
    notes: str            # diagnostic info, e.g. "MX tarpit (O365)"

class Verifier(Protocol):
    name: str
    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult: ...
    def assert_available(self) -> None:
        """Pre-flight. Raises VerifierUnavailable with actionable remediation message."""
```

Concrete verifiers implement this protocol. See M2 below.

---

## 3. Milestone M0 — Skeleton + plumbing + observability (≈1 day)

### 3.1 Goal

Stand up the repo skeleton: directory layout, packaging, all cross-cutting libraries in `scripts/lib/`, `CLAUDE.md` v1, brief schema + example, and a no-op test stage that proves observability works end to end. After M0 the user can: (1) run a setup script that creates a campaign folder, (2) interview Claude Code to write `brief.yaml`, (3) invoke a no-op stage and watch `status.md` update live.

### 3.2 What gets built

**Repo plumbing:**
- `pyproject.toml` declaring deps and a `console_scripts` entry per stage (e.g., `outreach-source-domains = scripts.source_domains:main`). Use `uv` for venv.
- `.gitignore` excluding `config/secrets.env`, `config/token.json`, `data/`, `campaigns/`.
- `README.md` with: prerequisites (Python 3.12+, OpenAI API key, Workspace Gmail), `uv sync`, `cp config/secrets.example.env config/secrets.env`, `python scripts/lib/gmail.py authorize` (one-time OAuth), then "ask Claude Code to start a campaign."

**Cross-cutting libraries** — all of §2 above, implemented and unit-tested:
- `lib/brief.py`
- `lib/progress.py`
- `lib/observability.py`
- `lib/dedup.py`
- `lib/dns_check.py`
- `lib/llm.py`
- `lib/gmail.py` (authorize + send only; bounce-poll added in M4)
- `lib/csv_schema.py`
- `lib/rate_limit.py`
- `lib/verifiers/base.py`

**Brief template + example:** `templates/_brief_template.yaml` matching `claude-spec.md §4`.

**Playbooks v1 (stubs):** every file in `playbooks/` exists but with only one section: "Purpose" and "When Claude reads this." Detailed content fills in across M1–M4.

**Orchestrator:** `CLAUDE.md` v1 — Stage 0 interview script + global rules. Detailed Stage 1–5 instructions added incrementally.

**No-op stage:** a `scripts/noop_stage.py` that:
- Loads the brief.
- Instantiates an `Observer`.
- Loops over `target.target_domain_count` synthetic items, sleeping `0.05s` each, calling `obs.tick({"items": i, "cost": 0.0})`.
- Writes a `noop.csv` with one row per item.
- Honors `--resume` via `ProgressStore`.

The no-op stage exists only to prove the plumbing works. It gets deleted at the start of M1.

### 3.3 Test plan (TDD)

`tests/lib/test_brief.py`:
- Load a complete valid `brief.yaml` → all sections populated, no errors.
- Missing required field (`target.segment` absent) → `BriefValidationError` mentioning the field by name.
- Empty `priority_roles` list → validation error.
- `send_rate_per_day = 5000` → validation error (over safety cap).
- `slug = "Foo Bar"` (not kebab-case) → validation error.
- Unknown extra field in YAML → validation error (Pydantic `extra="forbid"`).

`tests/lib/test_progress.py`:
- New `ProgressStore` with non-existent path → empty state.
- After `mark("key1", "ok")` → `is_done("key1")` is true.
- Reload from disk → state preserved.
- Concurrent writes from two threads → no corruption (`.tmp` rename serializes).
- Crash simulation: write `.tmp` but don't rename → on next `load()`, `.tmp` ignored, old file used.

`tests/lib/test_observability.py`:
- Cadence by items: 50 ticks → exactly one milestone emitted.
- Cadence by time: 1 tick + 121s elapsed → milestone emitted.
- Status.md content matches template.
- Activity.log lines are timestamped + ordered.
- `event(level="error")` writes ERROR to activity.log but doesn't change tick state.

`tests/lib/test_dedup.py`:
- `is_suppressed` returns true for an email in `suppression.csv`.
- `is_known` returns true for an email in `master_contacts.csv` when scope=all_campaigns.
- `is_known` returns false for the same email when scope=this_campaign.
- `commit()` writes both files atomically.

`tests/lib/test_dns_check.py`:
- Mock `dns.resolver.resolve` to return canned MX records → `mx_records` returns sorted list.
- Mock to raise `NoAnswer` → `mx_records` returns `[]`.
- Null MX (priority 0, target ".") → `is_null_mx` returns true.
- Domain with A but no MX → `has_mail` returns true.
- Domain with null MX → `has_mail` returns false.

`tests/lib/test_llm.py`:
- Mock OpenAI client → `parse()` returns parsed Pydantic instance + non-zero cost.
- Mock 429 response on first call, success on second → retry, total `parse()` returns success.
- Mock refusal in response → `parse()` returns (None, cost).
- `cascade()` with tier1 returning None → escalates to tier2.

`tests/lib/test_gmail.py`:
- Mock the Gmail HTTP client. `send()` builds correct MIME (verify `raw` field is base64url-encoded, contains `To:`, `From:`, `Subject:`, body).
- `send()` on 429 → raises `QuotaExceeded`.
- `send()` on `'Daily user sending limit exceeded'` → raises `QuotaExceeded`.

`tests/lib/test_csv_schema.py`:
- `write_csv_row` then `read_csv` round-trips every row model.
- Appending to existing CSV → header not duplicated.
- Invalid row (missing required field) → `ValidationError` at write time, not read time.

`tests/lib/test_rate_limit.py`:
- `RateLimiter(2.0)`: 4 `acquire()` calls take ~2.0s ±0.1s.
- `HourlyLimiter(3, burst=1)`: first 3 calls immediate; 4th blocks.

`tests/test_noop_stage.py`:
- End-to-end: run with `target_domain_count=200`, kill at item 100, resume → final `noop.csv` has 200 rows, no duplicates.
- `status.md` ends with "COMPLETED".
- `activity.log` has ≥ 5 milestone lines (200 items / 50 cadence + finish events).

### 3.4 Acceptance criteria for M0

- `uv sync` installs cleanly.
- `pytest tests/lib/ tests/test_noop_stage.py` is green.
- `python scripts/lib/gmail.py authorize` opens a browser, completes OAuth, writes `config/token.json`. Subsequent calls don't re-prompt.
- `python scripts/noop_stage.py --campaign-dir campaigns/2026-05_noop --target-count 200` produces:
  - `campaigns/2026-05_noop/status.md` showing live progress while it runs.
  - `campaigns/2026-05_noop/activity.log` with ≥5 timestamped milestone lines.
  - `campaigns/2026-05_noop/noop.csv` with exactly 200 rows.
- Claude Code, given a one-line ask, can interview the user to write a valid `brief.yaml` (driven by `CLAUDE.md` v1).
- Killing the no-op mid-run and re-invoking with `--resume` produces the same final file as a non-killed run.

**Additional acceptance gates added per review:**
- Strict-mode schema test: every Pydantic model in `lib/csv_schema.py` (and any LLM-response schemas declared in M0) passes OpenAI's strict-mode validator.
- Concurrency test: 100 threads each call `progress.mark(unique_key, "ok", count=1)` — final progress.json has exactly 100 keys (no lost updates).
- Brief-hash invariant test: a stage that writes `progress/brief_hash.txt`, then a subsequent stage that detects a brief change and refuses to run with a clear message.

---

## 4. Milestone M1 — Domain sourcing (≈½ day)

### 4.1 Goal

Implement Stage 1. Reads brief → produces `domains.csv` with ~`target_domain_count` deduped, DNS-validated domains in the segment. The script is parameterized by the brief; it doesn't know what segment it's working on.

### 4.2 What gets built

**`scripts/source_domains.py`**

CLI:
```
python scripts/source_domains.py \
  --campaign-dir campaigns/2026-05_medium-retailers \
  [--resume]
```

Reads `<campaign-dir>/brief.yaml`. Writes `<campaign-dir>/domains.csv` and `<campaign-dir>/progress/source_domains.json`.

Internal structure:
1. **Curated source URLs (optional):** brief can name a list of "seed" URLs in `notes`. v1: not in the schema; left as a TODO. v1 falls back to step 2 only.
2. **Sub-category breakdown:** generate ~10–30 search queries from the brief by combining `target.segment` + `target.include` + `target.geography`. Example: `"top curated marketplaces US"`, `"hybrid retailer-brands US"`, `"premium home retailers Canada"`. The LLM (tier1, structured output) takes the brief sections and returns a list of `SearchQuery{query: str, sub_segment: str}`.
3. **Per-query extraction:** for each query, call `openai.responses.parse(model=tier1, input=[...], text_format=DomainExtractionResponse, tools=[{"type":"web_search"}])`. Schema:
   ```python
   class DomainExtractionResponse(BaseModel):
       retailers: list[DomainExtractionItem]

   class DomainExtractionItem(BaseModel):
       company_name: str
       domain: Optional[str]
       domain_inferred: bool
       is_excluded: bool          # LLM applies brief.target.exclude
       exclude_reason: Optional[str]
       category: str
       source_url: str            # MUST be non-null; required by schema
       notes: str
   ```
4. **Filter + dedup:** drop `is_excluded=true` rows. Lowercase domain, strip scheme/www/path. Check `Deduper.is_known()` (scope from brief) and `Deduper.is_suppressed()`. Drop dupes within the current run.
5. **DNS validate:** `dns_check.has_mail(domain)` → drop if false.
6. **Write row:** append to `domains.csv` via `csv_schema.write_csv_row`, mark progress, tick observer.
7. **Stop condition:** when `len(rows) ≥ target.target_domain_count` or query list exhausted, whichever first.

LLM prompts (generalized from prior art):
- **`SEARCH_QUERY_PROMPT`** (new): given brief sections, generate ~15 search queries that cover the segment + sub-segments. Strict output schema.
- **`DOMAIN_EXTRACTION_PROMPT`** (port + generalize): explicit definitions from brief substituted in. Embeds `target.include` and `target.exclude` as bullet points. **Always requires `source_url` in every result item** (hallucination guard from research).

**`playbooks/02-domain-sourcing.md`** — fill in. Explains: strategy hierarchy, why we rely on `web_search` tool over scraping, what to do when a segment is hyper-narrow (manual seed URLs as escape hatch).

### 4.3 Test plan

`tests/test_source_domains.py`:
- Happy path: brief with `target_domain_count=20`. Mock LLM to return canned `DomainExtractionResponse` with 5 retailers per query, 4 queries → 20 unique domains → `domains.csv` has 20 rows.
- Filter applied: mock LLM returns rows with `is_excluded=true` → those rows dropped from output.
- Dedup within run: mock LLM returns "huckberry.com" three times across queries → only one row in output.
- Dedup against master: pre-populate `data/master_contacts.csv` with "huckberry.com"; brief scope=all_campaigns → not in output.
- Dedup ignored: same as above but scope=this_campaign → "huckberry.com" included.
- Suppressed: pre-populate `data/suppression.csv` with "do-not-contact@huckberry.com" → no effect at domain level (suppression is per-email). But add `huckberry.com` to a suppression domain-blocklist file (if we add one) — TODO: design doc doesn't have domain-level suppression; v1 only has email-level.
- DNS skip: mock `dns_check.has_mail` to return false for "fake.example" → not in output.
- No MX (null MX): mock `dns_check.is_null_mx` to return true → not in output.
- LLM 429: first call fails with 429, second succeeds → retries, output unchanged.
- LLM refusal: mock returns refusal → tier2 cascade tried; if also refusal, mark progress as `search_fail`, continue.
- Resume: kill after 10 rows, resume → final output identical to non-killed run.
- Observability: after 50 rows, milestone emitted to stdout + activity.log; status.md content matches expected counters.
- Domain normalization: input "Https://Www.RetailerX.com/path" → output "retailerx.com".
- `target_domain_count` reached early: with 1500 target and queries producing 2000 rows, exactly 1500 in output.
- Query exhaustion: target=5000 but only 1200 unique domains found → output has 1200 rows, status.md shows `target unmet, queries exhausted`, exit code 0 (not an error).

### 4.4 Acceptance criteria

- Two different campaigns (e.g., medium retailers vs. boutique hotels) both produce sensible `domains.csv` from the same script, no code changes, just brief changes.
- Live `status.md` shows incremental progress.
- Re-running with `--resume` after `Ctrl-C` produces the same final file.
- `pytest tests/test_source_domains.py` is green.

---

## 5. Milestone M2 — Contacts + pluggable verification (≈1 day)

### 5.1 Goal

Implement Stages 2 and 3. Reads `domains.csv` → produces `contacts.csv` (candidates) → `emails.csv` (verified only). Verification is pluggable via the `Verifier` interface.

### 5.2 What gets built

**`scripts/discover_contacts.py`**

CLI:
```
python scripts/discover_contacts.py --campaign-dir <dir> [--resume] [--workers 5]
```

Pre-flight (in this order, fail fast):
1. Brief-hash check: read `progress/brief_hash.txt` if present; if hash differs from `hash(brief.yaml)`, exit 2 with "Brief changed since previous stage. Revert brief or start a fresh campaign." If no prior hash, write it now.
2. Input-file check: `domains.csv` exists and has ≥1 row (excluding header). If empty, exit 2 with "No domains. Run source_domains.py first."

Per domain (parallelized via `ThreadPoolExecutor`, default 5 workers; **workers return results to a queue, main thread is sole CSV/progress writer** — see §2.2 concurrency model):
1. DNS recheck (cheap; cached by `lib/dns_check`).
2. Build the discovery prompt by template-substituting brief sections into `DISCOVERY_SYSTEM_PROMPT`. Variables: `value_prop`, `priority_roles`, `deprioritize`, `contacts_per_company`.
3. Call `llm.cascade(messages, text_format=DiscoveryResponse, tools=[{"type":"web_search"}])`.
4. For each returned person, push a `ContactRow` onto the writer queue.
5. Main thread drains the queue, writes rows to `contacts.csv`, marks progress with status `n_people` (count of candidates) or `no_people` / `discovery_fail`.

**Exception taxonomy** (review issue #11):
- **Retried** (transient): `openai.RateLimitError` (429), `openai.APIError` (5xx), `requests.Timeout`, `dns.exception.Timeout`, `ConnectionError`. Retry with exp backoff (1s, 2s, 4s) up to 3 attempts.
- **Terminal-skip** (per-item, retried on `--resume`): `worker_exc` covers anything not in the retry set above. Recorded as `status=worker_exc` in progress.json with the exception type + truncated message. On `--resume`, `worker_exc` entries are retried (matches prior art).
- **Halt** (fail-fast, the whole stage): `openai.AuthenticationError` (401), `openai.PermissionDeniedError` (403). The stage calls `obs.finish(FAILED)` and exits 2.

**Failure budget**: stage tracks `n_failures / n_processed`. If > 20% AND `n_processed > 20` (i.e., not a noise from the first few), halt with "Failure rate 24% (48 of 200 domains). Check OpenAI quota / API key. Re-run with --resume to continue from row 200."

```python
class DiscoveryResponse(BaseModel):
    corrected_domain: Optional[str]
    people: list[DiscoveryPerson]

class DiscoveryPerson(BaseModel):
    name: str
    role: str
    leverage_rationale: str
    email_if_known: Optional[str]
    email_source_url: Optional[str]
    confidence: float
```

Cap people at `who_to_contact.contacts_per_company` (default 7).

**`scripts/verify_emails.py`**

CLI:
```
python scripts/verify_emails.py --campaign-dir <dir> [--resume] [--workers 5]
```

Pre-flight (in this order):
1. Brief-hash check (as in `discover_contacts.py`).
2. Input-file check: `contacts.csv` exists, ≥1 row.
3. For each verifier in `brief.verifier.chain`, call `verifier.assert_available()`. Any failure → print actionable error and exit 2.
4. Estimated-time check (review issue #12): compute `len(contacts) / verifier.per_hour_cap`; if > 8h, print informational warning.

Per candidate row in `contacts.csv` (parallel, rate-limited):
1. If `email_if_known` is null: **skip in v1** (we no longer emit pattern-only rows). This is a change from the prior art and the design doc.
2. If non-null: walk the verifier chain. First verifier returning `status=accepted` (or `status=catchall` with a primary-source citation, for `web_citation`) wins. Stop at first win.
3. Write `EmailRow` to `emails.csv` if any verifier accepted.
4. Stop accumulating per-company after `contacts_per_company` verified wins.

**`scripts/lib/verifiers/smtp_probe.py`**

```python
class SmtpProbeVerifier:
    name = "smtp_probe"

    TARPIT_MX_PATTERNS = [
        "*.mail.protection.outlook.com",
        "*.olc.protection.outlook.com",
        "*.pphosted.com",
        "*.ppe-hosted.com",
        "*.mimecast.com",
    ]

    def __init__(self, *, rate_per_sec: float, per_hour_cap: int,
                 greylist_retry: bool, timeout: float = 10.0): ...

    def assert_available(self) -> None:
        """Open TCP to gmail-smtp-in.l.google.com:25. Raise VerifierUnavailable
           with message: 'Port 25 blocked. Connect to Dartmouth VPN, or set
           verifier.chain to ["web_citation"] in the brief, or enable api_provider.'"""

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        """1. Look up MX for domain via dns_check.
           2. If MX hostname matches TARPIT_MX_PATTERNS → return status=catchall, notes='MX tarpit'.
           3. Else open SMTP, HELO, MAIL FROM, RCPT TO candidate, RSET, RCPT TO random, QUIT.
           4. Map response codes → status (see §B.2 of claude-research.md).
           5. If 4xx and greylist_retry=true: wait 90s, retry once. Still 4xx → status=unknown.
           6. Return VerificationResult."""
```

**`scripts/lib/verifiers/web_citation.py`**

```python
class WebCitationVerifier:
    name = "web_citation"

    AGGREGATOR_HOSTS = set([
        "contactout.com", "rocketreach.co", "rocketreach.com",
        "zoominfo.com", "apollo.io", "lusha.com", "hunter.io",
        "success.ai", "snov.io", "leadiq.com", "salesintel.com",
        "dropcontact.com", "getprospect.com", "kendo.tools",
        "signalhire.com", "swordfish.ai", "voilanorbert.com",
        "skrapp.io", "anymailfinder.com", "nymeria.io", "uplead.com",
    ])

    def __init__(self, *, fetch_timeout: float = 8.0): ...

    def assert_available(self) -> None:
        """Always available — no-op."""

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        """Multi-step grounding check (added per review issue #9):
           1. If citation_url is None → status=unknown.
           2. If host of citation_url is in AGGREGATOR_HOSTS (or subdomain) → status=unknown.
           3. HEAD request to citation_url (httpx, timeout=8s, follow_redirects=True).
              - If not 200 → status=unknown, notes='citation URL not reachable'.
           4. GET the URL (8s timeout, decompress as needed). Lowercase the response body.
           5. Search for the email's local-part AND domain as separate substrings:
              - Both present → status=accepted, confidence=verified-web, source_url=citation_url.
              - Only domain present → status=unknown, notes='local-part not on citation page'.
              - Neither → status=unknown."""
```

**Residual risk**: this still doesn't catch an LLM that hallucinates a URL pointing to a page that happens to contain the local-part + domain by coincidence (e.g., `aforch` on a directory page listing 100 people). For v1 this is acceptable; multi-source agreement is a v2 hardening.

**`scripts/lib/verifiers/api_provider.py`** (feature flag, off by default)

```python
class ApiProviderVerifier:
    name = "api_provider"

    def __init__(self, *, provider: Literal["zerobounce","neverbounce"], api_key: str): ...
    def assert_available(self) -> None:
        """Ping provider's /health endpoint. Raise on failure."""
    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        """Call provider API. Map response to VerificationResult."""
```

Config-driven enable in `config/verifiers.yaml`:
```yaml
smtp_probe:
  enabled: true
  rate_per_sec: 3.0
  per_hour_cap: 100
web_citation:
  enabled: true
api_provider:
  enabled: false
  provider: zerobounce
  # api_key loaded from secrets.env
```

**`playbooks/03-contact-discovery.md`** and **`playbooks/04-email-verification.md`** — fill in. Explain the strategy: aggressive web_search aggressively for breadth; primary-source citation requirement (no aggregator scraping); the cascade philosophy.

### 5.3 Test plan

`tests/test_discover_contacts.py`:
- Happy path: brief with 3 domains; mock LLM returns 3 people per domain; output has 9 ContactRows.
- LLM refusal: cascade to tier2; if also refusal, mark `discovery_fail`, skip.
- `corrected_domain` returned: `ContactRow.domain` uses corrected value.
- `email_if_known` from aggregator: written as-is (filtering happens in verify, not discover).
- DNS pre-check fails on a domain: skip, mark `dns_fail`.
- Worker exception in one thread: other workers continue, the bad domain marked `worker_exc`.
- Resume after kill at row 50/200: final output identical to non-killed run.
- Observability: milestone every 20 companies.

`tests/lib/verifiers/test_smtp_probe.py`:
- Mock socket: HELO ok, RCPT TO real → 250; RCPT TO random → 550 → `accepted`.
- Both 250 → `catchall`.
- 550 to candidate → `rejected`.
- 421 on connect → `unknown`.
- 4xx + greylist_retry=true → after 90s (mocked clock), retry; second call 250 → `accepted`.
- 4xx + greylist_retry=false → `unknown`.
- MX hostname matches `*.pphosted.com` → return `catchall` immediately, socket never opened.
- MX hostname matches `*.mail.protection.outlook.com` → same.
- Null MX → `rejected` (cannot receive mail).
- `assert_available` mocked socket success → no exception.
- `assert_available` mocked socket failure → raises `VerifierUnavailable` with the documented message.
- Rate limiter integration: 10 calls at rate=2.0/sec → takes ~5s ±0.5s.

`tests/lib/verifiers/test_web_citation.py`:
- citation_url = null → `unknown`.
- citation_url = "https://rocketreach.co/jane" → `unknown` (aggregator).
- citation_url = "https://www.huckberry.com/team" → `accepted`, confidence=verified-web.
- citation_url = "https://subdomain.contactout.com/profile" → `unknown` (subdomain match).
- citation_url = malformed URL → `unknown`.

`tests/lib/verifiers/test_api_provider.py`:
- Mock provider returns `valid` → `accepted`, confidence=verified-api.
- Mock provider returns `invalid` → `rejected`.
- Mock provider returns `unknown`/`catchall` → mapped accordingly.
- 401 from provider → `assert_available` raises `VerifierUnavailable`.

`tests/test_verify_emails.py`:
- Pipeline integration: 3 contacts, chain=[smtp_probe, web_citation].
  - Contact 1: smtp accepted → EmailRow with confidence=verified-smtp.
  - Contact 2: smtp catchall, citation primary-source → EmailRow with verified-web.
  - Contact 3: smtp rejected → not written.
- Pre-flight failure: mock `smtp_probe.assert_available` to raise → script exits 2 with documented message.
- Per-company cap (contacts_per_company=3): 5 contacts at same domain; first 3 verified → stop, no probes on contacts 4 and 5.
- `email_if_known` is null → skipped entirely (v1 dropped the pattern-only tier).
- Resume after kill: state in `progress/verify_emails.json` honored.
- Rate limiting: integration with `rate_limit.RateLimiter` per the brief's `verifier.rate_limit`.

### 5.4 Acceptance criteria

- `emails.csv` has verified-only rows after a real run on a small (e.g., 10-domain) test brief.
- Swapping the brief from `chain: [smtp_probe, web_citation]` to `chain: [web_citation, smtp_probe]` changes behavior without touching the script.
- Setting `api_provider.enabled: true` in `config/verifiers.yaml` makes the api_provider verifier available; setting it false keeps SMTP-only flow.
- Pre-flight failure produces a clear remediation message.
- `pytest tests/test_discover_contacts.py tests/test_verify_emails.py tests/lib/verifiers/` is green.

---

## 6. Milestone M3 — Composition + Gmail send + test-batch flow (≈1 day)

### 6.1 Goal

Implement Stages 4 and 5. Reads `emails.csv` + template → produces `outbox.csv` → sends first 10 real, pauses, then sends the rest under the daily cap on `--confirm-test`.

### 6.2 What gets built

**`scripts/compose_emails.py`**

CLI:
```
python scripts/compose_emails.py --campaign-dir <dir> [--resume]
```

Pre-flight: brief-hash check + input-file check (same pattern as M2 scripts).

Per `EmailRow`:
1. Compute `first_name`:
   - Strip leading titles (regex: `^(Dr|Mr|Mrs|Ms|Prof|Sir|Lord|Lady)\.?\s+`).
   - Take first whitespace-split token.
   - **Ambiguity rules** (review issue #6, formal spec): the naive token is treated as ambiguous if ANY of:
     - Result contains a hyphen ("Marie-Claire").
     - Original `name` (after title strip) is 3+ tokens AND the first two tokens are both ≤8 chars AND neither token contains a period ("Mary Jane Smith" → ambiguous; "Robert J. Smith" → not, the "J." marks middle initial).
     - Original `name` contains any of: "Jr.", "Sr.", "II", "III", "IV".
     - First token contains any non-Latin character (codepoint > 0x024F).
     - First token is a known initialism / not-a-name token (precomputed set: {"the","mr","ms","mrs","dr","prof","sir","dame","lord","lady","rev"}). Defensive.
   - If `brief.message.personalize_first_name=true` AND ambiguous, call `llm.parse(...)` with `temperature=0`, `text_format=FirstNameResult`:
     ```python
     class FirstNameResult(BaseModel):
         model_config = ConfigDict(extra="forbid")
         first_name: str   # the form to use as a salutation
     ```
   - **Persistent cache**: `progress/first_name_cache.json` keyed by full `name` field (post-title-strip). On resume, load before processing. Same name → cached result, no LLM call. This guarantees consistent personalization across kill+resume and across re-runs.
2. Render template. Templates are markdown files with `{{slot}}` placeholders. Slots supported in v1: `first_name`, `name`, `company`, `role`, `value_prop`, `from_name`. Engine uses a tiny custom replacer (no Jinja dep needed; ~10 lines).
3. Body has two forms:
   - `body_plain`: the rendered template as plain text.
   - `body_html`: lightly converted (paragraphs to `<p>`, blank lines preserved, no rich formatting). Both included in the eventual MIME message.
4. Subject line is the template's first non-blank line if it starts with `Subject:` (strip prefix); else the first line as-is.
5. Lints (warnings to `activity.log`, NOT blocking):
   - Subject all-caps.
   - Body contains URL shortener (`bit.ly`, `t.co`, `tinyurl.com`, `bit.do`).
   - Body has 0 newlines.
   - Body length > 500 words.
6. Append `OutboxRow` to `outbox.csv`. Mark progress.

**`scripts/send_emails.py`**

CLI:
```
python scripts/send_emails.py --campaign-dir <dir> [--resume] [--confirm-test]
```

Pre-flight: brief-hash check + input-file check + `data/.send.pid` lockfile (single-writer constraint, see §2.4).

Phase decision (review issue #4, formal spec):
- Read `progress/send_emails.json`. Define `n_sent` := count of keys with `status in ("sent", "skipped_suppressed")`. Errors do NOT count toward `n_sent`.
- Check `phase_a_complete` sentinel in progress.json (a top-level key, NOT keyed by row). If present and truthy → Phase B requires `--confirm-test`.
- If `phase_a_complete` is false/missing AND `n_sent < send_test_count`: **Phase A — Test Batch.**
- If `phase_a_complete` is false/missing AND `n_sent >= send_test_count`: **set `phase_a_complete=true`, write Phase A banner, exit 0.** Next invocation transitions to Phase B (requires `--confirm-test`).
- If `phase_a_complete` is true AND `--confirm-test` absent: refuse with "Test batch complete. Re-run with `--confirm-test` to send the bulk." Exit 1.
- If `phase_a_complete` is true AND `--confirm-test` present: **Phase B — Bulk.**

Error retry policy for Phase A: a row that errors 3 times consecutively (3 different invocations or 3 retries within one invocation) is marked `terminal_error` and does NOT block Phase A from completing. The script logs a warning, then continues filling the test batch from the next row in `outbox.csv`. This prevents a permanently-broken recipient from blocking the user forever.

Common loop body (both phases):
1. Load row from `outbox.csv`.
2. **Hard-gate suppression:** `dedup.is_suppressed(row.to_email)` → skip, mark `skipped_suppressed`. (Counts toward n_sent.)
3. **Hard-gate daily counter** (review issue #3, pessimistic accounting): read `data/send_counters.json`. Schema:
   ```json
   {"2026-05-21": {"smrjit@example.com": 47}, "2026-05-20": {"smrjit@example.com": 1500}}
   ```
   Date keys are **local-time ISO dates** computed via `datetime.now().date()` (no timezone library dep; uses system local tz). Stale dates older than 14 days are pruned on read.
   - If today's count for `from_gmail` ≥ `send_rate_per_day`: print rollover message and exit 0.
   - Else: **increment the counter FIRST** (under fcntl.flock on send_counters.json), then proceed to send.
4. `gmail.send(...)`.
5. On 429 or `QuotaExceeded`: exp backoff 1,2,4,8,16,32s + jitter. Three retries. If still failing, **DECREMENT** the counter (we didn't actually send), mark `error`, continue to next row.
6. On hard failure (4xx other than 429): decrement counter, mark `error`.
7. On success: append `SentLogRow` to `sent.log`. (Counter was already incremented in step 3.) Append `(email, domain, name, role, slug, now)` to `data/master_contacts.csv` via `dedup.append_contact()` (uses fcntl.flock). Mark progress `sent`.
8. Throttle: sleep `throttle_seconds * uniform(0.5, 1.5)`.
9. Tick observer.

The pessimistic accounting means: on a process kill between send and progress-mark, the counter is already incremented, so the next run sees one less slot. We over-throttle (lose at most one slot) instead of over-sending. Over-sending hits the Gmail daily-cap lockout; over-throttling just makes the user wait a few seconds.

Phase A end-of-loop:
- After exactly `send_test_count` successful sends, print to stdout (in addition to status.md):
  ```
  ════════════════════════════════════════════════════════════
  Test batch complete. Sent 10 emails from <from_gmail>.
  Check your Gmail Sent folder:
    https://mail.google.com/mail/u/<from_gmail>/#sent

  When you've verified that emails look right AND landed in inbox
  (not spam), re-run with --confirm-test to send the remaining
  <n> emails.
  ════════════════════════════════════════════════════════════
  ```
- Exit 0.

Phase B: just continues until `outbox.csv` exhausted or daily cap hit.

**Templates:** `templates/ai-agent-integration.md` (the user's actual first template) and `templates/_example.md` (a doc explaining the slot syntax).

**`playbooks/05-email-composition.md`** and **`playbooks/06-sending.md`** — fill in. Explain: template authoring, lint rules, the test-batch philosophy, throttle rationale, the daily-cap-rollover behavior.

### 6.3 Test plan

`tests/test_compose_emails.py`:
- Happy path: 3 EmailRows, template with all slots → 3 OutboxRows with correct substitutions.
- First-name extraction:
  - "Dr. Robert Smith" → first_name "Robert".
  - "Jane Doe" → "Jane".
  - "Marie-Claire Dupont" with personalize_first_name=true → LLM called (mocked) → returns "Marie-Claire".
  - "李伟" with personalize=true → LLM called → returns canonicalized form.
  - personalize=false → naive split always used.
- Cache: two rows with same `name` → LLM called once.
- Lints: subject "OFFER INSIDE!!!" → activity.log warning; row still written.
- Lints: body containing "bit.ly/foo" → warning.
- Resume after kill.
- Missing template file → fail-fast error mentioning the path.
- Slot referenced in template but missing from EmailRow → fail-fast error mentioning the slot name.

`tests/test_send_emails.py`:
- Phase A: 12 OutboxRows, `send_test_count=10` → exactly 10 sent, exit code 0, console banner printed.
- Phase A → Phase B: 12 rows, run Phase A (10 sent), re-run with `--confirm-test` → 2 more sent, exit 0.
- Phase B with daily cap hit mid-run: cap=15, 12 already sent today, 8 in outbox → 3 sent, 5 deferred, exit 0, status.md says "cap rolled over".
- Suppression hard-gate: 1 row's to_email in suppression.csv → marked `skipped_suppressed`, not sent.
- Quota exceeded mid-send: mock Gmail raises `QuotaExceeded` → 3 retries with backoff → if still failing, mark `error`, continue.
- Throttle jitter: 10 rows with throttle=1.0 → total time between 5s and 15s (uniform 0.5-1.5x).
- master_contacts.csv updated on every send.
- progress/send_emails.json consistent after kill+resume.
- Replay safety: re-running without --resume on a partially-sent campaign refuses ("would re-send already-sent recipients").
- `from_gmail` rewriting: mock Gmail returns response with different `From` than requested → log warning to activity.log.

### 6.4 Acceptance criteria

- A real (small) campaign run: 12 OutboxRows, send Phase A → user sees 10 emails in their Gmail Sent folder, status.md and activity.log accurate.
- Re-running with `--confirm-test` sends the remaining 2.
- A test unsubscribe (manually adding a row to `data/suppression.csv`) prevents that recipient from being sent to.
- Hitting the daily cap mid-run exits cleanly; the next day's invocation resumes without dupes.
- `pytest tests/test_compose_emails.py tests/test_send_emails.py` green.

---

## 7. Milestone M4 — Bounce tracking + polish (≈½ day)

### 7.1 Goal

Implement the v1 Stage 6 (bounce-only). Polish CLAUDE.md, fill in playbook gaps, smoke-test the entire pipeline on a real small campaign.

### 7.2 What gets built

**`scripts/poll_bounces.py`**

CLI:
```
python scripts/poll_bounces.py [--since-message-id <id>]
```

**Re-auth note (review issue #7):** M3 authorized with `gmail.send` only. M4 needs `gmail.send + gmail.readonly`. The `authorize()` helper detects the scope mismatch and forces a fresh OAuth flow — the user will see one Google consent screen on first M4 invocation. README documents this explicitly so the user isn't surprised.

1. Authorize Gmail via `lib/gmail.authorize(scopes=["gmail.send","gmail.readonly"])`. Acquires `data/.poll.pid` lockfile.
2. Read `data/poll_bounces_state.json` to get `last_processed_message_id`.
3. Call `gmail.list_bounces(since_message_id=last_processed)`.
4. For each bounce record:
   - Extract `final_recipient` from the bounce message body.
   - If already in `data/suppression.csv` → skip.
   - Else → append `SuppressionRow(email, reason="hard_bounce", source=gmail_message_id, added_at=now)`.
5. Update `data/poll_bounces_state.json` to the newest seen message ID.

**`gmail.list_bounces` implementation** (added to `lib/gmail.py`):
- Query: `from:mailer-daemon subject:"Delivery Status Notification (Failure)"`.
- For each match, fetch full message, parse the `text/plain` body for `Final-Recipient: rfc822;<email>` line.
- Return list of `BounceRecord(original_recipient, gmail_message_id, bounce_date)`.

**Polish list:**
- `CLAUDE.md` v2: incorporate lessons from the first real run. Add explicit reference to `playbooks/0X-*.md` files at each stage transition. Add a "Common questions" section.
- All `playbooks/*.md` filled in with: Purpose, When Claude reads this, Strategy, Common failure modes, Examples.
- `README.md` v2: include a worked-example "5-minute campaign" walkthrough.
- Add `scripts/setup_campaign.py` — a tiny helper that creates the `campaigns/<slug>/` folder with the right subdirs and copies in the brief template. Used by Stage 0.

### 7.3 Test plan

`tests/test_poll_bounces.py`:
- Mock `gmail.list_bounces` to return 3 BounceRecords → all 3 appended to suppression.csv.
- One of the 3 already in suppression.csv → only 2 appended (idempotent).
- Empty bounce list → state updated to current head, no changes to suppression.
- `poll_bounces_state.json` missing → starts from scratch (returns all bounces in inbox).
- Concurrent invocation: only one writer at a time (file lock or `.tmp`+rename).

End-to-end smoke test (manual, not in pytest):
- Create a campaign with `target_domain_count=3` and 2 of those domains being `example.org`-style fakes that will bounce.
- Run M1–M3 normally.
- Send Phase A (with `--confirm-test` so all 3 go out).
- Wait ~5 minutes for bounce notifications.
- Run `poll_bounces.py`.
- Verify the 2 fake-domain recipients appear in `data/suppression.csv`.

### 7.4 Acceptance criteria

- Polling a Gmail inbox with no prior bounces → no changes, state updated.
- After a test send to known-invalid addresses, `poll_bounces.py` adds them to suppression.
- All playbooks have substantive content (not stubs).
- `pytest` (full suite) green.
- README walkthrough works for a fresh-clone user (manually verified).

---

### Inter-stage orchestration (added per review issue #13)

The plan doesn't require a monolithic pipeline runner — Claude Code (driven by `CLAUDE.md`) can invoke each stage script in sequence. But two pieces of plumbing make that workable:

**`scripts/status.py`** (read-only inspector)

```
python scripts/status.py --campaign-dir <dir>
```

Output: a structured report (JSON when `--json`, human-readable otherwise) of:
- Brief slug and hash; whether hash matches the saved `progress/brief_hash.txt`.
- Per-stage status: NOT_STARTED | RUNNING | COMPLETED | FAILED | INCONSISTENT (input missing or partial).
- For each completed stage: row count of its output file, cost, duration.
- For the current/next stage: the command to run.

Claude Code calls `status.py --json` between every action to know exactly where it stands. This eliminates the "did M2 complete?" guesswork.

**`scripts/run_pipeline.py`** (optional sequential runner)

```
python scripts/run_pipeline.py --campaign-dir <dir>
```

Runs stages 1–4 in order, stopping fail-fast on any non-zero exit. Stops before Stage 5 to give the user the test-batch decision. This is a convenience wrapper for users who don't want to step through stages individually; Claude Code may or may not use it depending on the orchestration in `CLAUDE.md`.

**Brief-validation error contract** (used by Claude Code to recover from a bad brief it generated)

When `lib/brief.load(path)` raises `BriefValidationError`, the script's main wrapper catches it and:
1. Prints a structured JSON error to stderr:
   ```json
   {"error":"BriefValidationError","field":"target.segment","message":"required","brief_path":"campaigns/2026-05_foo/brief.yaml"}
   ```
2. Exits with code 3 (reserved for brief errors specifically; 2 is general fail, 1 is unspecified).
3. Claude Code's prompt in `CLAUDE.md` includes: "If a stage exits 3, parse the JSON error from stderr, ask the user about the specific field, edit `brief.yaml`, and re-run the stage."

## 8. Acceptance criteria summary (end of v1)

The user can:
1. Clone the repo, run `uv sync`, copy `secrets.example.env` to `secrets.env` + fill keys, run `python scripts/lib/gmail.py authorize`.
2. Open Claude Code in the repo, say "contact medium-sized retailers about AI shopping agents," and Claude:
   - Creates `campaigns/2026-05_medium-retailers/`.
   - Interviews the user to fill `brief.yaml`.
   - Runs `source_domains.py`, `discover_contacts.py`, `verify_emails.py`, `compose_emails.py` end to end without stopping. The user watches `status.md` live.
   - Runs `send_emails.py` for the test batch, stops, and asks the user to check their Gmail Sent folder.
   - On user OK, runs `send_emails.py --confirm-test` for the bulk.
3. Periodically runs `poll_bounces.py` to refresh the suppression list.
4. Starts a second, totally different campaign (e.g., "contact boutique hotel chains") with no code changes — just a new brief.

Quality bars:
- The full pytest suite is green (~80–120 tests).
- Cross-campaign dedup works: a domain emailed in campaign A is not re-emailed in campaign B (when scope=all_campaigns).
- Suppression is honored at every send.
- Resume-after-kill produces identical output to non-killed run for every stage.
- Pre-flight failures (port 25 blocked, OAuth expired, brief invalid) produce actionable error messages and clean exit codes.

---

## 9. Risks and open issues

### 9.1 Risks

1. **OpenAI hosted `web_search` quality drift.** If OpenAI tweaks the tool's behavior, Stage 1 quality could regress. Mitigation: the search-query LLM call is the first thing in the pipeline — easy to swap to Brave + LLM-extraction later.
2. **Workspace tenant gets flagged anyway.** Even with `send_rate_per_day=1500`, fresh-domain cold mail can get throttled. Mitigation: M3 includes the test-batch pause which functions as a deliverability canary; user can stop before bulk send. Warmup is deferred but documented as the obvious next feature if this happens.
3. **Dartmouth VPN dependency.** SMTP probing requires open port 25; the user's environment provides this via the Dartmouth VPN. If the user moves off campus, the pre-flight fails. Mitigation: clear remediation message in the abort; documented in `playbooks/04-email-verification.md` that `api_provider` is the upgrade path.
4. **Pydantic strict-mode schema gotchas with OpenAI `responses.parse`.** Every Optional field must be in `required` AND typed `Optional[X]`. Mitigation: covered in `claude-research.md §B.4`; tests force-exercise every model.
5. **Gmail OAuth in "Testing" mode expires refresh tokens every 7 days.** Mitigation: README documents this; user can move consent to "Production" if it becomes annoying.

### 9.2 Open issues (plan-writer's calls)

These were noted in `claude-spec.md §12` and resolved as follows:
- **brief format:** pure YAML (`brief.yaml`). The optional sibling `brief.md` is a free-text notes file Claude Code reads but doesn't validate.
- **default `target_domain_count`:** none. Brief validation fails if missing.
- **`discover_contacts.py` batching:** per-domain in v1. Cost headroom; simpler retry.
- **multi-model support (Anthropic):** OpenAI only in v1. `lib/llm.py` is structured to make adding another provider possible without rewriting callers.

### 9.3 Deliberately deferred to v2+ (see §1.3)

Recap, so plan reviewers don't flag them as omissions: warmup, compliance (`List-Unsubscribe`, postal address), Brave/Tavily search, LLM cache, pattern-only tier, reply detection, auto follow-up, campaign report, HTTPS unsubscribe, geo filtering, custom opening line.

---

## 10. v1 invariants (cross-cutting constraints — read this once)

A reader who jumps into any one milestone should be able to verify these invariants are honored without scrolling.

**Concurrency**
- `ProgressStore` is thread-safe via internal `RLock`. Recommended pattern: workers compute results, push to a `queue.Queue`, main thread is sole writer of CSVs and progress.
- All writes to `data/master_contacts.csv` and `data/suppression.csv` use `fcntl.flock(LOCK_EX)`. Reads use `LOCK_SH`. Plain `open(path, "a")` append; no full rewrites.
- Per-machine single-writer constraint: only one `send_emails.py` and one `poll_bounces.py` may run at a time. Enforced via `data/.send.pid` and `data/.poll.pid` lockfiles.

**Brief stability across stages**
- The first stage to use a brief writes `progress/brief_hash.txt = sha256(brief.yaml file bytes)`.
- Every subsequent stage checks this hash and refuses to run if it doesn't match. Exit 2 with a clear remediation message.

**Pessimistic counters**
- Daily send counter (`data/send_counters.json`) is incremented BEFORE the Gmail API call and decremented on hard failure. This caps over-send at 0 even across process crashes.
- Date keys use system local timezone (`datetime.now().date()`). No `pytz`/`zoneinfo` dependency.

**Schema rules (every Pydantic model in this codebase)**
- `model_config = ConfigDict(extra="forbid")`.
- `Optional[X]` fields have `default=None`.
- LLM-response schemas additionally require `source_url: str` (non-null) for every grounded fact.
- A test in `tests/lib/test_csv_schema.py` verifies OpenAI strict-mode acceptance for every model.

**Error taxonomy**
- Transient (retried): 429, 5xx, timeouts, ConnectionError. Exp-backoff up to 3 attempts.
- Terminal-skip (per-item): anything not in the retry set. Marked `worker_exc` in progress.json; retried on `--resume`.
- Halt (whole-stage): authentication errors (401/403). Stage calls `obs.finish(FAILED)` and exits 2.
- Failure budget: > 20% items failed AND > 20 items processed → halt with diagnostic message.

**Exit codes**
- 0: success.
- 1: refused operation (e.g., Phase B without --confirm-test). User can re-invoke correctly.
- 2: stage failure (pre-flight failed, halt condition, FAILED finish).
- 3: brief validation error (structured JSON on stderr; Claude Code parses to fix the brief).

**Observability split**
- `CampaignObserver` (singleton per campaign): owns `status.md` pipeline header, cross-stage cost roll-up. Reads + writes `observer_state.json`.
- `StageObserver` (one per stage invocation): owns stage section of `status.md`, all `activity.log` lines for the stage.
- Errors: `event(level="warn")` for transient, `finish(status="FAILED")` for terminal. There is no `event(level="error")` to avoid ambiguity.

**Rate limits (research-aligned defaults)**
- Verifier: 0.5/sec, 50/hour cap, burst 10. Brief warns if computed verification time > 8h.
- Sender: throttle is `throttle_seconds * uniform(0.5, 1.5)` per row.

## 11. How to use this plan

The build order is M0 → M1 → M2 → M3 → M4. Each milestone is independently shippable and the user can pause after any of them to use the partial system.

When implementing, refer back to:
- `planning/outreach-bot-design-and-plan.md` for the original design intent.
- `planning/claude-research.md` for concrete patterns from prior art + research findings to follow.
- `planning/claude-interview.md` for the resolved scope decisions.
- `planning/claude-spec.md` for the consolidated requirements.

When in doubt, the interview answers win. The plan deliberately keeps several features narrower than the original design doc proposed (no compliance scaffolding, no warmup, no pattern-only, no LLM cache) — those are user choices, not oversights.
