I now have all the context needed to generate the section. Let me produce it.

# Section 07 — Discover Contacts (Stage 2, first half)

## Overview

This section implements **Stage 2 of the outreach pipeline**: `scripts/discover_contacts.py`. It reads the `domains.csv` produced by Stage 1 and, for each domain, uses the LLM (with hosted `web_search`) to find high-leverage candidate people at that company, writing the results to `contacts.csv`.

This is the first parallelized stage. Workers run inside a `ThreadPoolExecutor` and push results to a `queue.Queue`; the main thread is the sole writer of CSVs and progress. The stage also introduces the **exception taxonomy** and the **failure budget** that the rest of the M2 / M3 stages use.

This section also fills in `playbooks/03-contact-discovery.md`.

This section runs in parallel with section-08 (verifiers); the two share no files.

## Dependencies (reference only — do not re-implement)

This section depends on outputs from earlier sections. Use them; don't re-define them.

- **section-02** — `lib/brief.py` (loads `brief.yaml` into a `Brief` Pydantic model), `lib/progress.py` (the `ProgressStore` with `RLock` and atomic `.tmp`+rename writes), `lib/csv_schema.py` (the `DomainRow`, `ContactRow`, `read_csv`, `write_csv_row` helpers), `lib/dns_check.py` (LRU-cached MX/A lookups).
- **section-03** — `lib/observability.py` (the `CampaignObserver` / `StageObserver` split; `tick()`, `event()`, `finish()`; cadence rules).
- **section-04** — `lib/llm.py` (the `LLMClient`, `parse()`, `cascade()`, `ParseResult` with `refused` vs `parsed=None` distinction).
- **section-05** — `lib/progress.write_brief_hash` / `check_brief_hash` invariant; the standard pre-flight pattern.
- **section-06** — produces `<campaign-dir>/domains.csv`. Each row is a `DomainRow` (columns: `company_name`, `domain`, `domain_inferred`, `category`, `source_url`, `notes`). Section-07 only reads this file; it does not modify it.

All Pydantic models in this section must follow the codebase invariants from section-02: `model_config = ConfigDict(extra="forbid")`, and every `Optional[X]` field has `default=None` (required for OpenAI strict mode).

## Files to create / modify

1. **Create** `scripts/discover_contacts.py` — the stage script.
2. **Create** `tests/test_discover_contacts.py` — the test suite. Tests come FIRST.
3. **Create / fill in** `playbooks/03-contact-discovery.md` — the human-readable strategy doc.

(The `DiscoveryResponse` / `DiscoveryPerson` Pydantic models can live at the top of `scripts/discover_contacts.py`; they are stage-specific LLM-response schemas, not shared library types.)

## Tests FIRST — `tests/test_discover_contacts.py`

Write these test stubs before the script. Each is a one-line description; flesh out the body using mocks of `lib/llm.LLMClient`, `lib/dns_check`, and an in-memory `tmp_campaign_dir` fixture (from `tests/conftest.py`, set up in section-02).

```python
# tests/test_discover_contacts.py
# Mock lib/llm.LLMClient.cascade() to return canned ParseResult objects.
# Mock lib/dns_check.has_mail() per-test as needed.
# Use tmp_campaign_dir fixture for filesystem isolation.

# --- Happy path ---
# Test: 3 domains in domains.csv, mocked LLM returns DiscoveryResponse with 3 people each
#       → contacts.csv has 9 rows; every row is a valid ContactRow.

# --- LLM behavior ---
# Test: tier1 ParseResult(refused=True) → cascade attempted at tier2; tier2 also refused
#       → mark domain status='discovery_fail' in progress; no rows written for that domain.
# Test: tier1 ParseResult(parsed=DiscoveryResponse(people=[])) → empty people; cascade tried;
#       still empty → mark status='no_people'.
# Test: DiscoveryResponse(corrected_domain="huckberry.com", people=[...]) where input
#       domain was "huckberry.co" → ContactRow.domain = "huckberry.com" for every written row.

# --- DNS ---
# Test: dns_check.has_mail(domain) returns False for one domain → skip, mark status='dns_fail'.

# --- Concurrency (review issue #1 queue-based writer) ---
# Test: worker exception in one thread (raise random RuntimeError inside the LLM mock for
#       domain X) → marked status='worker_exc' with exception_type + truncated message in
#       progress; other workers continue and produce their rows.
# Test: worker_exc is retriable — re-run with --resume, this time the mock for domain X
#       succeeds → row(s) appear in contacts.csv on the resumed run.
# Test: queue-based write — 50 mocked domains processed concurrently with --workers 5;
#       each domain returns a deterministic person → contacts.csv has exactly 50 rows,
#       no dupes, no row loss, no interleaved/garbled CSV writes.

# --- Exception taxonomy (review issue #11) ---
# Test: openai.RateLimitError (429) raised once, then succeed → retried with exp backoff
#       (mocked sleep), eventually rows are written.
# Test: openai.AuthenticationError (401) raised → stage halts: obs.finish(status='FAILED')
#       called, process exits with code 2; no further domains processed; contacts.csv may
#       be partial but progress.json is consistent.
# Test: openai.PermissionDeniedError (403) → same halt behavior as 401.
# Test: requests.Timeout (or dns.exception.Timeout) raised in a worker → retried via
#       exp backoff inside the worker; if still failing after 3 attempts → marked worker_exc.
# Test: a non-retryable exception (e.g. ValueError) in a worker → marked worker_exc directly,
#       no retries.

# --- Failure budget ---
# Test: 25 of 100 domains hit worker_exc (>20%) → stage halts with diagnostic message
#       ("Failure rate 25% (25 of 100 domains). Check OpenAI quota / API key. Re-run with
#       --resume to continue from row 100."); exit code 2.
# Test: 3 of 10 domains fail (30% rate, but n_processed=10 < 20 threshold) → stage continues,
#       no halt.

# --- Per-company cap ---
# Test: brief.who_to_contact.contacts_per_company=3; mocked LLM returns 7 people for one
#       domain → only the first 3 are written to contacts.csv.

# --- Resume ---
# Test: kill at row 50/200 (simulate by raising KeyboardInterrupt after 50 marks), then
#       re-invoke with --resume → final contacts.csv identical to a non-killed run
#       (same row count, same content modulo ordering).
# Test: --resume with no prior progress file → behaves like a fresh run.

# --- Pre-flight ---
# Test: missing domains.csv → exit 2 with stderr containing
#       "No domains. Run source_domains.py first."
# Test: domains.csv exists but is header-only (0 data rows) → same exit 2 message.
# Test: brief-hash mismatch (progress/brief_hash.txt differs from sha256 of brief.yaml)
#       → exit 2 with documented brief-changed message.
# Test: no prior brief_hash.txt → it gets written at pre-flight; subsequent run with the
#       same brief succeeds; modifying brief.yaml then re-running fails.

# --- Observability ---
# Test: with cadence_items=20, processing 60 domains → exactly 3 milestone lines emitted
#       to stdout AND to activity.log; status.md ends in COMPLETED state.
# Test: every milestone line in activity.log is ISO-timestamped and tagged '[discover]'.

# --- email_if_known passthrough ---
# Test: LLM returns DiscoveryPerson with email_if_known set → ContactRow.email_if_known
#       has the same value (no validation/filtering here — that happens in verify_emails).
# Test: LLM returns email_if_known=None → ContactRow.email_if_known is None (preserved).
```

The test file does NOT need to fully spec the mock objects or the file fixtures — `conftest.py` from section-02 already provides `tmp_campaign_dir` and `sample_brief`. The test file should `monkeypatch` `lib.llm.LLMClient.cascade` and `lib.dns_check.has_mail` as needed.

## Implementation — `scripts/discover_contacts.py`

### CLI

```
python scripts/discover_contacts.py --campaign-dir <dir> [--resume] [--workers 5]
```

Reads `<campaign-dir>/brief.yaml` and `<campaign-dir>/domains.csv`. Writes `<campaign-dir>/contacts.csv` and `<campaign-dir>/progress/discover_contacts.json`. Default worker count is 5.

### Pre-flight (in this order — fail fast)

1. Load the brief via `lib.brief.load(<campaign-dir>/brief.yaml)`. On `BriefValidationError`, print the structured JSON to stderr and exit 3 (per the v1 exit-code contract). 
2. **Brief-hash check.** Read `progress/brief_hash.txt` if present. Compute `sha256(brief.yaml bytes)`. If the file exists and the hash differs, exit 2 with: `"Brief changed since previous stage. Revert brief or start a fresh campaign."` If no prior hash, write it now via `lib.progress.write_brief_hash()`.
3. **Input-file check.** `domains.csv` must exist AND contain ≥1 data row (excluding the header). If not, exit 2 with: `"No domains. Run source_domains.py first."`
4. Instantiate the `CampaignObserver` for the campaign and a `StageObserver(stage="discover", cadence_items=20, cadence_seconds=120)`. Call `obs.stage_start()`.
5. Instantiate `ProgressStore(<campaign-dir>/progress/discover_contacts.json)` and `obs.load()`.

### Per-domain worker logic (runs inside `ThreadPoolExecutor`)

For each row in `domains.csv` that is not already terminal in progress (`ok` / `no_people` / `dns_fail` / `discovery_fail` are terminal; `worker_exc` is RETRIABLE on `--resume`):

1. **DNS recheck.** `dns_check.has_mail(domain)` — cheap, LRU-cached. If `False`, return `("dns_fail", [])`.
2. **Build the discovery prompt** by template-substituting brief sections into `DISCOVERY_SYSTEM_PROMPT` (defined below). Variables substituted: `value_prop`, `priority_roles` (rendered as a bullet list), `deprioritize` (bullet list or "none"), `contacts_per_company`.
3. **Call the LLM:**
   ```python
   result = llm.cascade(
       messages=[
           {"role": "system", "content": system_prompt},
           {"role": "user", "content": f"Company: {company_name}\nDomain: {domain}"},
       ],
       text_format=DiscoveryResponse,
       tools=[{"type": "web_search"}],
       temperature=0.0,
   )
   ```
4. Interpret the `ParseResult`:
   - `result.refused == True` (both tiers) → return `("discovery_fail", [])`.
   - `result.parsed is None` AND `result.refused == False` (both tiers returned nothing) → return `("discovery_fail", [])`.
   - `result.parsed.people == []` (after cascade) → return `("no_people", [])`.
   - Otherwise: cap people at `brief.who_to_contact.contacts_per_company`; convert each `DiscoveryPerson` to a `ContactRow`; if `result.parsed.corrected_domain` is set, use it for `ContactRow.domain`; return `("ok", [contact_rows])`.

The worker function MUST NOT touch `contacts.csv` or `progress.json` directly — it returns its result via the `queue.Queue` to the main thread.

### Main-thread writer loop

The main thread:
- Submits all not-yet-done domains to the executor.
- Pops results from the queue (or uses `as_completed`).
- For each result: writes any `ContactRow`s via `csv_schema.write_csv_row(contacts_csv_path, row)`, calls `progress.mark(domain, status, n_people=len(rows), cost=...)`, calls `obs.tick({"domains_done": ..., "contacts_found": ..., "cost_usd": ...})`.
- On any worker exception caught by the executor: marks `worker_exc` with `{"exception_type": type(e).__name__, "message": str(e)[:200]}`.

This single-writer pattern matches `lib/progress.py`'s recommended concurrency model from section-02 and avoids needing a lock per CSV file.

### Exception taxonomy (formal, applied inside each worker)

- **Retried (transient, exp-backoff 1s, 2s, 4s, up to 3 attempts inside the worker):** `openai.RateLimitError` (429), `openai.APIError` (5xx subclasses), `requests.Timeout`, `dns.exception.Timeout`, `ConnectionError`. If still failing after 3 attempts → re-raise as a worker exception → main thread marks `worker_exc`.
- **Terminal-skip (per-item):** any other exception → main thread marks `worker_exc` with the exception type + truncated message. These are RETRIED on `--resume`.
- **Halt (whole-stage):** `openai.AuthenticationError` (401), `openai.PermissionDeniedError` (403). The worker re-raises immediately; the main thread catches these specifically, calls `obs.finish(status="FAILED", summary={...})`, and exits 2.

### Failure budget

After each domain marked, the main thread checks:

```
n_failures = count of progress entries with status in {"worker_exc", "discovery_fail"}
n_processed = count of progress entries with any terminal status
if n_failures / max(n_processed, 1) > 0.20 and n_processed > 20:
    halt
```

Halt message format: `"Failure rate {pct}% ({n_failures} of {n_processed} domains). Check OpenAI quota / API key. Re-run with --resume to continue from row {n_processed}."` Then `obs.finish(status="FAILED", ...)` and exit 2.

### Pydantic schemas (declared inline in `discover_contacts.py`)

These follow the strict-mode rules from section-02 (`extra="forbid"`, `Optional[X] = None`):

```python
class DiscoveryPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str
    leverage_rationale: str
    email_if_known: Optional[str] = None
    email_source_url: Optional[str] = None
    confidence: float                # 0.0 - 1.0

class DiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrected_domain: Optional[str] = None
    people: list[DiscoveryPerson]
```

A worker converts each `DiscoveryPerson` to a `ContactRow` (from `lib/csv_schema.py`): copy fields one-to-one, set `company_name` and `domain` from the input row (with `corrected_domain` overriding when present).

### `DISCOVERY_SYSTEM_PROMPT` template

The prompt is a Python f-string-style template kept as a module-level constant. It substitutes (at runtime, not at import) these brief variables:

- `{value_prop}` — `brief.message.value_prop`.
- `{priority_roles}` — `brief.who_to_contact.priority_roles`, rendered as a bullet list.
- `{deprioritize}` — `brief.who_to_contact.deprioritize`, bullet list (or the literal `"(none)"` if empty).
- `{contacts_per_company}` — `brief.who_to_contact.contacts_per_company`.

The prompt content (paraphrased, you can write the actual prose during implementation): instruct the model to use `web_search` to find up to `{contacts_per_company}` high-leverage people at the company, prioritizing the roles listed, avoiding the deprioritized roles, and grounding each person with a `email_source_url` whenever an email is asserted. Require the model to set `corrected_domain` if the user-provided domain looks wrong (e.g., outdated `.co` when the live site is `.com`). For each person, require a one-sentence `leverage_rationale` explaining WHY this person is the right contact for a pitch about `{value_prop}`.

### Observability

- `cadence_items=20`, `cadence_seconds=120` (override the defaults from `StageObserver`).
- Counters to tick: `domains_done`, `contacts_found`, `cost_usd`, `n_failures`.
- Status line example: `"Contacts found: 612 candidates across 200 of 500 domains (cost: $4.20)"`.

### Exit codes (per the v1 contract from section-05)

- `0` — success (stage finished, contacts.csv written, observer finished COMPLETED).
- `2` — pre-flight failure, halt (auth error, failure budget), or `obs.finish(FAILED)`.
- `3` — brief validation error (structured JSON on stderr, parsed by Claude Code).

## `playbooks/03-contact-discovery.md`

Fill in the previously-stub playbook. Required sections:

- **Purpose.** What stage 2 does in one paragraph.
- **When Claude reads this.** Whenever it's about to invoke `discover_contacts.py`, or when interpreting its output / failure modes.
- **Strategy.** Why we use one LLM call per domain (cost headroom, simpler retry; per `claude-plan §9.2`); why we require `web_search` grounding; why we cap at `contacts_per_company`; why we skip pattern-only candidates in Stage 3 (so the discover stage is allowed to omit `email_if_known`, but never invent one).
- **Common failure modes.** Brief-hash mismatch → tell user to revert or fresh-campaign. Auth error → tell user to check `OPENAI_API_KEY`. Failure-budget halt → diagnostic interpretation. `dns_fail` cluster → likely brief-quality issue in Stage 1.
- **Examples.** A worked `--resume` example after a kill; what the activity.log looks like during a healthy run.

## Acceptance criteria for this section

- `pytest tests/test_discover_contacts.py` is green.
- Running `python scripts/discover_contacts.py --campaign-dir <test-dir>` on a small (3-domain) hand-crafted `domains.csv` with mocked LLM produces a 3+ row `contacts.csv` and a `progress/discover_contacts.json` whose every key has a terminal status.
- Killing the script mid-run (Ctrl-C around domain 5 of 10) and re-invoking with `--resume` yields the same final `contacts.csv` as a non-killed run on the same input (modulo row ordering).
- Pre-flight failures (missing domains.csv, brief-hash mismatch) print the documented messages and exit 2.
- The failure-budget halt triggers at exactly the documented threshold (>20% AND n_processed > 20).
- `playbooks/03-contact-discovery.md` contains all five required sections.

## Out-of-scope reminders (do NOT add these here)

Per `claude-plan §1.3` and `claude-spec §1.3` — these are explicitly v2+, do not creep them in:

- No reply detection, no auto follow-up bump, no LLM response cache.
- No Brave/Tavily/Serper search backends (OpenAI hosted `web_search` only).
- No pattern-only email tier (Stage 3 will hard-skip rows whose `email_if_known` is null; do not invent emails here either).
- No geographic recipient filtering.
- No HTTPS unsubscribe / `List-Unsubscribe` headers (Stage 5 concern anyway).

The relevant on-disk files an implementer will touch are at the absolute paths:
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/discover_contacts.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/test_discover_contacts.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/03-contact-discovery.md`