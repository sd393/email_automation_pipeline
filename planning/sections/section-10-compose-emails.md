Now I have all the information I need. Let me write the section content.

# Section 10 — Compose Emails (Stage 4)

This section implements `scripts/compose_emails.py`, the fourth pipeline stage. It reads `<campaign-dir>/emails.csv` plus a message template, computes a per-recipient first name (with formal ambiguity rules and a persistent on-disk cache), renders the template, runs non-blocking lints, and appends rows to `<campaign-dir>/outbox.csv`. It also fills in `playbooks/05-email-composition.md`.

It closes the first half of milestone M3. Section 11 (`send_emails.py`) consumes `outbox.csv`.

## 1. Dependencies (do not re-implement; reference only)

Section 10 builds on existing pieces from earlier sections. Treat these as already-implemented black boxes — read the section that owns each one if a signature is unclear, but do not re-derive or re-test them here.

- `lib/brief.py` (section 02): `load(path) -> Brief`, `BriefValidationError`, `MessageSection` with `template: str` path and `personalize_first_name: bool`.
- `lib/csv_schema.py` (section 02): `EmailRow`, `OutboxRow`, `read_csv(path, model)`, `write_csv_row(path, row)`. `OutboxRow` shape:
  ```python
  class OutboxRow(BaseModel):
      to_email: str
      to_name: str
      subject: str
      body_html: str
      body_plain: str
      first_name_used: str
  ```
  `EmailRow` shape (input):
  ```python
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
  ```
- `lib/progress.py` (section 02): `ProgressStore(path)` with `load()`, `mark(key, status, **extras)`, `is_done(key)`, `is_retriable(key)`, `keys()`. Thread-safe via internal `RLock`. Atomic `.tmp`-rename writes. Brief-hash helpers `write_brief_hash(campaign_dir)` and `check_brief_hash(campaign_dir)` from section 05 — `check_brief_hash` returns `True` if the hash on disk matches or is absent (it writes on absence), `False` if it mismatches.
- `lib/observability.py` (section 03): `CampaignObserver(campaign_dir)`, `StageObserver(campaign_obs, stage="compose", cadence_items=50, cadence_seconds=120)`. Methods: `stage_start()`, `event(message, level="info"|"warn")`, `tick(counters)`, `finish(status, summary)`. Warnings (lints) go through `event(level="warn")`; failures go through `finish(status="FAILED")`.
- `lib/llm.py` (section 04): `LLMClient.parse(messages, text_format, *, tier="tier1", max_retries=3, temperature=0.0) -> ParseResult`. We call this only for ambiguous first names. Always pass `temperature=0`.

The exit-code contract, brief-hash invariant, error taxonomy, and observability split are inherited from §10 of `claude-plan.md`; do not re-derive them here. In short for this section:
- Exit 0 on success.
- Exit 2 on pre-flight failure (missing input, brief-hash mismatch, missing template).
- Lints are `event(level="warn")` — never block a row from being written.
- No section 10 work should ever call Gmail or DNS.

## 2. Files to create or modify

Create:
- `scripts/compose_emails.py` — the stage CLI/entrypoint.
- `scripts/lib/first_name.py` — the first-name extraction logic with ambiguity rules and persistent cache (importable so tests can hit it directly).
- `scripts/lib/template_render.py` — the `{{slot}}` substituter (tiny helper, no Jinja).
- `playbooks/05-email-composition.md` — fill in the existing stub.
- `tests/test_compose_emails.py` — full test suite for this stage.
- `tests/lib/test_first_name.py` — unit tests for the ambiguity rules (so the rule logic can be tested without spinning up the whole pipeline).
- `tests/lib/test_template_render.py` — unit tests for the substituter.

Do NOT modify any file outside `scripts/`, `playbooks/`, and `tests/`. In particular, do not touch the brief schema, the CSV row models, or any earlier stage's script. If you find yourself needing to, stop and re-read section 02/03/04.

## 3. CLI and stage shape

```
python scripts/compose_emails.py --campaign-dir <dir> [--resume]
```

Behavior:
1. Parse args.
2. Load brief via `lib/brief.load(<campaign-dir>/brief.yaml)`. On `BriefValidationError`, print structured JSON to stderr and exit 3 (the wrapper for this is the same pattern as other stage scripts; if a shared helper exists in `lib/brief.py` use it, otherwise duplicate ~5 lines).
3. **Pre-flight**, in this order, fail-fast on each:
   a. `progress/brief_hash.txt` check via `lib/progress.check_brief_hash(campaign_dir)`. On mismatch: print `"Brief changed since previous stage. Revert brief.yaml or start a fresh campaign."` and exit 2.
   b. `<campaign-dir>/emails.csv` exists and has ≥ 1 data row. Otherwise: print `"No verified emails. Run verify_emails.py first."` and exit 2.
   c. Template file at `brief.message.template` exists and is readable. Otherwise: print `"Template not found: <path>"` and exit 2.
4. Construct observers: `CampaignObserver(campaign_dir)` then `StageObserver(camp_obs, "compose", cadence_items=50, cadence_seconds=120)`. Call `stage_obs.stage_start()`.
5. Open `ProgressStore(campaign_dir/"progress"/"compose_emails.json")` and call `.load()`. Open `FirstNameCache(campaign_dir/"progress"/"first_name_cache.json")` (see §5) and call `.load()`.
6. Iterate over `read_csv(emails_csv, EmailRow)`. The progress key is the recipient email (lowercased, the same key the verifier used). Skip rows where `progress.is_done(key)` and NOT `progress.is_retriable(key)`.
7. For each row: compute first name, render template, run lints, append `OutboxRow` to `<campaign-dir>/outbox.csv` via `write_csv_row(...)`, mark progress with status `"composed"`. Tick observer with counters `{"composed": n, "llm_calls": k, "cost": c}`.
8. On any unhandled exception in the main loop: `stage_obs.finish("FAILED", {...})`, then re-raise so the user sees the traceback.
9. On clean completion: `stage_obs.finish("COMPLETED", {"composed": total_n, "llm_calls": total_k, "cost": total_cost})`. Exit 0.

This stage is sequential (no `ThreadPoolExecutor`) — composition is cheap, the optional LLM call is only ~1–5% of rows, and the simpler single-threaded shape eliminates a class of cache-locking concerns.

## 4. Template rendering

Templates are markdown files. The user's first template lives at `templates/ai-agent-integration.md` (created in section 01 as a stub, possibly filled in by the user). The substituter supports exactly the slots `first_name`, `name`, `company`, `role`, `value_prop`, `from_name`. Implementation contract:

```python
# scripts/lib/template_render.py

ALLOWED_SLOTS = {"first_name", "name", "company", "role", "value_prop", "from_name"}

class TemplateError(Exception):
    """Raised when a template references an unknown slot or a row is missing a value."""

def find_slots(template_text: str) -> set[str]:
    """Return the set of {{slot}} names referenced in the template."""

def render(template_text: str, values: dict[str, str]) -> str:
    """Substitute {{slot}} with values[slot]. Raise TemplateError if any slot in
    the template is not in ALLOWED_SLOTS, or if any referenced slot is missing
    from values. Extra keys in `values` are ignored (no error)."""
```

Implementation note: a simple `re.sub(r"\{\{\s*(\w+)\s*\}\}", ...)` is enough. No Jinja dependency. Allow optional whitespace inside the braces.

In `compose_emails.py`, after reading the template once at startup, call `find_slots(template_text)` and verify the set is a subset of `ALLOWED_SLOTS`. If not, exit 2 with `"Template references unknown slot: <name>"`. Per-row, build `values = {"first_name": ..., "name": row.name, "company": row.company, "role": row.role, "value_prop": brief.message.value_prop, "from_name": brief.sending.from_name}` and call `render(...)`.

### Subject vs body
After rendering:
- If the first non-blank line of the rendered text starts with `Subject:` (case-insensitive, optional whitespace), strip that prefix; the remainder of that line is the subject; the rest (after one blank line consumed) is the body. Otherwise the first line is the subject and the rest is the body.
- `body_plain` = the body text as-is.
- `body_html` = paragraphs wrapped in `<p>...</p>`, blank lines preserved as paragraph breaks. No bold/italic/link conversion in v1. A trivial implementation: split on `\n\n+`, wrap each chunk in `<p>` after HTML-escaping (`html.escape`), join with `\n`.

## 5. First-name extraction

This is the core review-issue-#6 logic. Live in `scripts/lib/first_name.py` so it can be unit-tested in isolation.

```python
# scripts/lib/first_name.py

TITLE_PATTERN = re.compile(r"^(Dr|Mr|Mrs|Ms|Prof|Sir|Lord|Lady)\.?\s+", re.IGNORECASE)
SUFFIX_TOKENS = {"Jr.", "Sr.", "II", "III", "IV", "Jr", "Sr"}
NOT_A_NAME_TOKENS = {"the","mr","ms","mrs","dr","prof","sir","dame","lord","lady","rev"}
MIDDLE_INITIAL = re.compile(r"^[A-Z]\.$")

class FirstNameResult(BaseModel):
    """Pydantic schema returned by the LLM canonicalization call.
    Strict-mode compliant: extra='forbid', no Optional fields without defaults."""
    model_config = ConfigDict(extra="forbid")
    first_name: str

def strip_title(name: str) -> str:
    """Remove a single leading title prefix if present."""

def naive_first(name_stripped: str) -> str:
    """Return the first whitespace-split token. Used when name is unambiguous."""

def is_ambiguous(name_stripped: str) -> bool:
    """Return True if the formal ambiguity rules trigger. See rule list below."""

def extract(name: str, *, personalize: bool, llm_client, cache) -> tuple[str, int, float]:
    """Compute the first-name string for `name`. Returns (first_name, llm_calls_made, cost_usd).

    Flow:
      1. stripped = strip_title(name).
      2. If not personalize: return (naive_first(stripped), 0, 0.0).
      3. If cache hit on `name` (original, post-strip): return (cached, 0, 0.0).
      4. If not is_ambiguous(stripped): result = naive_first(stripped); cache it; return.
      5. Else: call llm_client.parse(..., text_format=FirstNameResult, temperature=0.0);
         on refusal or parsed=None: fall back to naive_first; on success: use parsed.first_name.
         Cache the result either way. Return (result, 1, cost.usd)."""
```

### Ambiguity rules (formal spec — implement exactly)

`is_ambiguous(stripped)` returns `True` if ANY of the following hold; otherwise `False`. Order doesn't matter logically, but evaluate cheap checks first:

1. The first whitespace-split token contains a hyphen. Triggers on `"Marie-Claire Dupont"`.
2. Any token in the stripped string is in `SUFFIX_TOKENS` (`Jr.`, `Sr.`, `II`, `III`, `IV`, and the dotless `Jr`/`Sr` for robustness). Triggers on `"Robert Smith Jr."`.
3. The first whitespace-split token contains any character with codepoint > `0x024F` (non-Latin-Extended). Triggers on `"李伟"`.
4. The first whitespace-split token, lowercased, is in `NOT_A_NAME_TOKENS`. Defensive — guards against title-strip misses or malformed rows.
5. The stripped string has ≥ 3 whitespace-split tokens AND the first two tokens are each ≤ 8 characters AND neither of the first two tokens matches the `MIDDLE_INITIAL` pattern (a single uppercase letter followed by a period). Triggers on `"Mary Jane Smith"`; does NOT trigger on `"Robert J. Smith"` (the `"J."` matches the middle-initial pattern, so rule 5 is short-circuited).

If none of rules 1–5 fire, the name is unambiguous and we use the naive first token.

### Persistent cache

```python
# scripts/lib/first_name.py (continued)

class FirstNameCache:
    """JSON-backed dict keyed by the original (post-title-strip) name string,
    valued by the canonical first name to use. Atomic .tmp-rename on every set.
    NOT shared across campaigns — lives under <campaign-dir>/progress/.

    The cache key is the post-title-strip `name` so that 'Dr. Marie-Claire Dupont'
    and 'Marie-Claire Dupont' hit the same entry; this matches the spec that
    'same name → cached result, no LLM call'."""

    def __init__(self, path: Path): ...
    def load(self) -> None:
        """Read the JSON file if present; empty dict otherwise."""
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None:
        """Write through to disk atomically (.tmp + os.replace)."""
```

Resilience: the cache must survive kill+resume. The "load" is called once at startup; subsequent `set()` calls persist immediately so that an interrupted run never loses cache entries. This is critical for cost: the LLM call is the most expensive thing in this stage and we cannot afford to re-issue it for already-seen names.

### LLM prompt (sketch — fill in during implementation)

When the ambiguity rules fire, call `llm_client.parse(messages, text_format=FirstNameResult, tier="tier1", temperature=0.0)`. The system prompt should be ~one paragraph: "You are a name parser. Given a person's full name, output the form they would prefer in a salutation in English. Examples: 'Marie-Claire Dupont' → 'Marie-Claire'. '李伟' → 'Wei' (transliterate). 'Mary Jane Smith' → 'Mary Jane' if she goes by both, else 'Mary'. 'Robert Smith Jr.' → 'Robert'." The full prompt text is up to the implementer; just keep the schema strict.

## 6. Lints

After rendering, run all lints per row. Each lint that fires calls `stage_obs.event(f"lint: <message> (to={row.email})", level="warn")`. The row is still written to `outbox.csv` regardless. Lints:

- **Subject all-caps**: `subject == subject.upper()` AND subject contains at least one letter. Message: `"subject is all caps"`.
- **URL shortener in body**: any of `bit.ly`, `t.co`, `tinyurl.com`, `bit.do` appearing as a substring in `body_plain` (case-insensitive). Message: `"body contains URL shortener"`.
- **Zero newlines in body**: `"\n" not in body_plain.strip()`. Message: `"body has no paragraph breaks"`.
- **Body too long**: `len(body_plain.split()) > 500`. Message: `"body is > 500 words"`.

Lints are warnings only. There is no failure path through lints.

## 7. Tests

Write all tests in `tests/test_compose_emails.py`, `tests/lib/test_first_name.py`, and `tests/lib/test_template_render.py` BEFORE implementing. Keep tests stub-level — `pytest`-collectible function signatures with a docstring stating the assertion, fleshed out only enough to actually exercise the code path. Use the shared fixtures from `tests/conftest.py` (section 02) — `sample_brief`, `tmp_campaign_dir`. Mock the LLM with a fake `LLMClient` (return a `ParseResult` with a `FirstNameResult` parsed instance and a small cost stub). Do NOT hit the network in any test.

### `tests/lib/test_template_render.py`

```python
# Test: render with all slots present → exact substitution.
# Test: render with extra unused values dict keys → ignored, no error.
# Test: render with a {{unknown_slot}} not in ALLOWED_SLOTS → raises TemplateError.
# Test: render with a known slot referenced but missing from values → raises TemplateError naming the slot.
# Test: find_slots correctly extracts repeated and whitespace-padded slot names.
# Test: HTML-escaping in body_html branch — verify a row.name = "A&B" appears as "A&amp;B" in body_html
#   (only relevant if html.escape is used inside render; if it lives in compose_emails.py, test it there).
```

### `tests/lib/test_first_name.py`

```python
# Naive path (personalize=False):
# Test: "Dr. Robert Smith" → "Robert" (title stripped, no LLM).
# Test: "Jane Doe" → "Jane" (no LLM).
# Test: "Andy" → "Andy" (single token, no LLM).
# Test: "Marie-Claire Dupont" + personalize=False → "Marie-Claire" via naive split (LLM never called).

# Ambiguity-trigger rules (personalize=True, no cache hit, LLM mocked):
# Test: "Marie-Claire Dupont" → is_ambiguous=True (hyphen rule) → LLM called.
# Test: "Mary Jane Smith" → is_ambiguous=True (three-token + short rule) → LLM called.
# Test: "Robert J. Smith" → is_ambiguous=False (middle-initial short-circuits rule 5) → LLM NOT called,
#       naive_first returns "Robert".
# Test: "李伟" → is_ambiguous=True (codepoint > 0x024F) → LLM called.
# Test: "Robert Smith Jr." → is_ambiguous=True (suffix token) → LLM called.
# Test: "Robert Smith II" → ambiguous (suffix II) → LLM called.

# Defensive rule:
# Test: "the Foo Bar" → is_ambiguous=True (NOT_A_NAME_TOKEN first) → LLM called.

# Personalize=False short-circuit:
# Test: any ambiguous name with personalize=False → LLM never called, even for "Marie-Claire".

# Cache:
# Test: same name twice with personalize=True + ambiguous → LLM called exactly once.
#   Counter on the fake LLMClient verifies this.
# Test: cache survives reload — write entries to disk, construct new FirstNameCache on same path,
#   call .load(), verify .get() returns prior values.
# Test: cache atomicity — simulate kill mid-write (no .tmp rename) → load() falls back to the old file.

# Temperature contract:
# Test: when LLM is invoked, the mock receives temperature=0.0 in its call kwargs.

# Refusal fallback:
# Test: mock LLM returns ParseResult(parsed=None, refused=True, ...) → extract() returns the naive
#   first token; result is cached so future calls don't re-invoke the LLM.
```

### `tests/test_compose_emails.py`

```python
# Happy path:
# Test: 3 EmailRows + template with all slots → 3 OutboxRows written with correct substitutions,
#   progress.json has 3 keys with status="composed", exit 0.

# Subject extraction:
# Test: template starts with "Subject: Hi {{first_name}}\n\nBody..." → OutboxRow.subject="Hi <name>",
#   body excludes the Subject line.
# Test: template without Subject prefix → first line of rendered text becomes subject.

# First-name integration (most rule coverage is in test_first_name.py; here we test integration):
# Test: EmailRow with name="Dr. Robert Smith" → OutboxRow.first_name_used="Robert".
# Test: EmailRow with name="Marie-Claire Dupont", brief.message.personalize_first_name=True,
#   mocked LLM returns "Marie-Claire" → OutboxRow.first_name_used="Marie-Claire".
# Test: same brief but personalize_first_name=False → first_name_used="Marie-Claire" via naive split,
#   LLM never called.

# Persistent cache integration:
# Test: two EmailRows with the same `name` value (different emails/companies) → LLM called once,
#   both OutboxRows have the same first_name_used.
# Test: kill at row 3/5 + resume → cache file present from first run; second run does NOT call the
#   LLM for any previously-seen name; final outbox.csv has 5 rows; progress.json has 5 composed keys.

# Lints (each row still written):
# Test: subject "OFFER INSIDE!!!" (from template) → activity.log contains a WARN line referencing
#   "all caps"; outbox.csv still has the row.
# Test: body containing "bit.ly/foo" → WARN line referencing "URL shortener"; row written.
# Test: body with 0 newlines → WARN; row written.
# Test: body with 600 words → WARN; row written.

# Template errors:
# Test: brief.message.template points to a non-existent file → exit 2, message mentions the path.
# Test: template references {{nonexistent_slot}} → exit 2, message names the slot.
# Test: template references valid slot but row missing a corresponding field (cannot happen with
#   EmailRow as-defined, but verify the TemplateError path with a synthetic test) → exit 2.

# Pre-flight:
# Test: missing emails.csv → exit 2 with message "Run verify_emails.py first."
# Test: empty emails.csv (header only) → exit 2.
# Test: brief-hash mismatch → exit 2 with mismatch message; nothing written to outbox.csv.

# Resume:
# Test: kill mid-run at row 100/200 (raise an exception in a test hook), then re-run with --resume:
#   - outbox.csv contains exactly 200 rows.
#   - No row appears twice (dedup by to_email).
#   - progress.json has 200 composed keys.
#   - LLM call counter on mock did not exceed the call count from a non-killed run + 0 (cache covers
#     the names already processed; new LLM calls only for never-seen ambiguous names).

# Observability:
# Test: after 50 rows, status.md contains the cadence milestone; activity.log has at least one
#   "milestone:" line for the compose stage.
# Test: finish(COMPLETED) called exactly once; summary dict contains "composed", "llm_calls", "cost".
```

Tests using mocked LLM should construct a fake that records `(messages, text_format, temperature)` per call and returns a configurable `ParseResult`. Cost stubs of `0.0001` per call are fine — what matters is the call count and the temperature, not the dollar value.

## 8. Playbook content

`playbooks/05-email-composition.md` should be filled in with these sections (no need to write more than 1–2 paragraphs each):

- **Purpose** — what this stage does and what it produces.
- **When Claude reads this** — at the start of stage 4, before invoking `compose_emails.py`.
- **Template authoring** — slot syntax (`{{first_name}}`, etc.), the Subject-line convention, that markdown is plain text in v1 (no headers/bold/links beyond `<p>` wrapping in body_html).
- **First-name philosophy** — naive split first; LLM only on ambiguous names; persistent cache means costs are bounded.
- **Lint warnings** — what each lint catches and why it's a warning not a block.
- **Common failure modes** — missing template file, brief-hash mismatch, slot typo.

Keep it under ~200 lines. The user (and Claude Code) reads this; verbose prose hurts more than terse-but-correct prose.

## 9. Acceptance checklist (verify before declaring done)

- `pytest tests/test_compose_emails.py tests/lib/test_first_name.py tests/lib/test_template_render.py` is green.
- Running `python scripts/compose_emails.py --campaign-dir <a-real-campaign-with-emails.csv>` produces an `outbox.csv` whose row count equals the input `emails.csv` row count.
- Re-running the same command with `--resume` after a `Ctrl-C` mid-run produces the same final `outbox.csv` (byte-for-byte modulo timestamps, which `OutboxRow` doesn't carry).
- Switching `brief.message.personalize_first_name` from `false` to `true` and re-running on the same emails.csv (with cache file deleted) increases `llm_calls` in the finish-summary by exactly the count of ambiguous names.
- `progress/first_name_cache.json` exists and is valid JSON after any run that invoked the LLM at least once.
- `status.md` shows COMPLETED for the compose stage at the end.
- No new dependency added to `pyproject.toml` (only stdlib + existing libs).

## 10. Out-of-scope reminders

Do not, in this section, do any of the following — they are explicitly deferred per `claude-spec.md §1.3`:

- Custom-opening-line personalization (the whole "LLM writes a unique first line" pattern).
- `List-Unsubscribe` headers, postal address, CAN-SPAM footer.
- LLM response cache for any call other than the first-name canonicalization cache described above.
- HTML formatting richer than `<p>`-wrapping (no markdown-to-HTML conversion).
- Per-recipient subject variation beyond template slots.

If a test seems to require any of these, you've drifted from the spec — re-read this section.

Relevant absolute paths:

- Plan: `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/claude-plan.md`
- TDD plan: `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/claude-plan-tdd.md`
- Section index: `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/sections/index.md`
- Target section file (written by hook): `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/sections/section-10-compose-emails.md`