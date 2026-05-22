Now I have all the information I need. Let me generate the section content.

# section-12-bounces-and-polish

## Scope

This is the final section, closing **Milestone M4**. It is the thin Stage 6 of the pipeline plus the polish work that turns the v1 codebase from "all stages function" to "shippable and documented."

Concretely, this section delivers:

1. `scripts/poll_bounces.py` — the standalone bounce-poller.
2. `lib/gmail.list_bounces` — the Gmail-readonly query + body parsing helper (the `send()` half of `lib/gmail.py` already exists from section 04; only `list_bounces` is new here).
3. `scripts/setup_campaign.py` — a tiny helper that creates the `campaigns/<slug>/` folder with the right subdirs and copies in the brief template (used by Stage 0 in `CLAUDE.md`).
4. Polish work:
   - `CLAUDE.md` v2 — incorporate lessons from real runs; cross-reference playbooks at each stage transition; add a "Common questions" section.
   - Fill in all `playbooks/*.md` stubs that haven't already been filled by earlier sections. Each playbook should have: Purpose, When Claude reads this, Strategy, Common failure modes, Examples.
   - `README.md` v2 — include a worked-example "5-minute campaign" walkthrough.
   - `tests/manual/smoke_test_m4.md` — documented end-to-end smoke test that produces a real test campaign, sends to known-bad addresses, runs `poll_bounces`, verifies suppression updates.
5. Tests: `tests/test_poll_bounces.py` (the only automated test suite added in M4; the rest is documentation/polish).

## Dependencies

This section depends on **section-11-send-emails** being complete (which itself transitively requires sections 01–10). Inputs from earlier sections that this section consumes without re-deriving:

- `lib/gmail.authorize()` with scope-superset detection (from section 04).
- `GmailClient`, `SendResult`, `QuotaExceeded`, `BounceRecord` model declarations (from section 04 — `BounceRecord` was declared but `list_bounces()` was deferred to this section).
- `lib/dedup.Deduper` with `append_suppressed()` and `fcntl.flock`-based append semantics (from section 03).
- `lib/csv_schema.SuppressionRow` with `reason: Literal["hard_bounce","manual_optout","reply_optout"]` (from section 02).
- `lib/observability.{CampaignObserver,StageObserver}` (from section 03).
- `data/.poll.pid` lockfile convention (from section 03 / cross-cutting invariants).

## Background and Context (read-once invariants relevant here)

A reader implementing only this section needs the following invariants from `claude-plan.md §10` in mind. They are NOT re-derived here — they are documented project-wide rules:

**Concurrency rules for `data/`** (from section 03):
- All writes to `data/master_contacts.csv` and `data/suppression.csv` use `fcntl.flock(fd, LOCK_EX)`. Reads use `LOCK_SH`.
- Appends are single-row, plain `open(path, "a")`. We do NOT rewrite the whole file on every append.
- Only one `poll_bounces.py` may run per machine at a time. Enforced via a `data/.poll.pid` lockfile (separate from the `data/.send.pid` used by `send_emails.py`).

**Schema rules for every Pydantic model in the codebase:**
- `model_config = ConfigDict(extra="forbid")`.
- `Optional[X]` fields have `default=None`.

**Error taxonomy:**
- Transient errors (429, 5xx, timeouts, `ConnectionError`): exp-backoff up to 3 attempts.
- Halt errors (401/403): stage calls `obs.finish(FAILED)` and exits 2.

**Exit codes:**
- 0: success.
- 1: refused operation.
- 2: stage failure (pre-flight failed, FAILED finish).
- 3: brief validation error.

**Re-auth note (critical for this section):** M3 (`send_emails.py`) authorized Gmail with the `gmail.send` scope only. M4 (`poll_bounces.py`) needs `gmail.send + gmail.readonly`. The `authorize()` helper from section 04 already implements scope-superset detection: if `token.json` is missing a requested scope, it deletes the token and runs `InstalledAppFlow.run_local_server()` fresh. The user will see exactly one Google consent screen on the first invocation of `poll_bounces.py`. README v2 documents this explicitly so the user isn't surprised by an unexpected browser pop-up.

## Tests First (TDD)

Write these before any implementation. They live in `tests/test_poll_bounces.py` and in `tests/lib/test_gmail.py` (for the new `list_bounces` portion).

### `tests/lib/test_gmail.py` (additions for `list_bounces`)

Stub the test list — fill the bodies during implementation. Use mocked Gmail HTTP responses (the existing `lib/gmail.py` tests from section 04 already establish the mocking pattern).

```python
# tests/lib/test_gmail.py — additions for list_bounces

def test_list_bounces_parses_final_recipient():
    """Mocked API returns matching messages → returns BounceRecord list with parsed
    Final-Recipient. Verify each record has original_recipient, gmail_message_id,
    bounce_date populated."""

def test_list_bounces_empty_inbox():
    """Mocked API returns 0 matches → returns []."""

def test_list_bounces_malformed_body_skipped():
    """One message lacks a Final-Recipient header → that record skipped, warning
    logged via the standard logger (not raised). Other records returned normally."""

def test_list_bounces_since_message_id_filter():
    """Caller passes since_message_id='abc'; mocked API receives the appropriate
    query filter; only messages newer than that id are returned."""
```

### `tests/test_poll_bounces.py`

```python
# tests/test_poll_bounces.py

def test_three_bounces_appended_to_suppression(tmp_path, mock_gmail):
    """mock_gmail.list_bounces returns 3 BounceRecords → 3 rows appended to
    data/suppression.csv with reason='hard_bounce' and source=gmail_message_id."""

def test_idempotent_dedup_against_existing_suppression(tmp_path, mock_gmail):
    """1 of the 3 returned bounces already exists in data/suppression.csv →
    only 2 new rows appended (idempotent)."""

def test_empty_bounce_list_updates_state_only(tmp_path, mock_gmail):
    """list_bounces returns [] → no changes to suppression.csv. state file
    data/poll_bounces_state.json updated to latest message ID seen (or unchanged
    if mock returns no head id)."""

def test_missing_state_file_starts_from_scratch(tmp_path, mock_gmail):
    """data/poll_bounces_state.json absent → poll runs without since_message_id;
    all bounces in inbox processed."""

def test_malformed_body_record_skipped(tmp_path, mock_gmail):
    """One bounce lacks Final-Recipient (handled inside list_bounces, but tested
    end-to-end here too) → skipped, warning logged, no row appended for that one."""

def test_concurrent_invocation_blocks_on_poll_pid_lock(tmp_path):
    """Two poll_bounces.py invocations: second one fails fast with a clean message
    naming data/.poll.pid and exits 1. First completes normally."""

# Re-auth flow (review issue #7)

def test_reauth_when_token_only_has_send_scope(tmp_path, mock_oauth_flow):
    """Pre-populate token.json with scopes=['gmail.send']. Invoke poll_bounces →
    authorize() detects missing 'gmail.readonly', deletes token.json, runs
    InstalledAppFlow.run_local_server() (mocked). New token.json has both scopes.
    Documented message 'Gmail token has scopes [...]; required [...]. Re-authorizing.'
    is printed to stdout."""

def test_no_reauth_when_token_has_both_scopes(tmp_path, mock_oauth_flow):
    """Pre-populate token.json with both scopes already → no re-flow triggered,
    InstalledAppFlow.run_local_server() is NOT called."""
```

### Manual smoke test

Not in pytest. Documented at `tests/manual/smoke_test_m4.md`:

1. Create a campaign with `target_domain_count=3` where 2 of the 3 domains are guaranteed-to-bounce fakes (e.g., `nosuchcompany12345.example.org`).
2. Run M1–M3 normally (`source_domains.py`, `discover_contacts.py`, `verify_emails.py`, `compose_emails.py`).
3. Send via `send_emails.py` and then `send_emails.py --confirm-test` so all addresses go out.
4. Wait ~5 minutes for Gmail to receive bounce notifications.
5. Run `python scripts/poll_bounces.py`.
6. Verify the 2 fake-domain recipients appear in `data/suppression.csv` with `reason=hard_bounce`.

## Implementation

### 1. `lib/gmail.list_bounces` (extend existing `lib/gmail.py`)

Add to the existing `GmailClient` class created in section 04. The `BounceRecord` model is already declared there; only the method body is new.

```python
class GmailClient:
    # ... existing send() etc. unchanged ...

    def list_bounces(self, since_message_id: str | None = None) -> list[BounceRecord]:
        """Find all bounce notifications in the authorized mailbox.

        Query string:
            from:mailer-daemon subject:"Delivery Status Notification (Failure)"

        For each matching message:
          1. Fetch the full message (format='full') via users.messages.get.
          2. Walk MIME parts; locate the text/plain body.
          3. Search the body for a line matching:
                Final-Recipient: rfc822;<email>
             (case-insensitive on the header label; the value after ';' is the
             recipient. Strip whitespace.)
          4. If found → emit BounceRecord(original_recipient, gmail_message_id,
             bounce_date). If not found → log a warning and skip that record
             (do not raise).

        since_message_id: when set, the query is appended with
        `after:<internalDate>` derived from a get() on that message id, so only
        newer messages are returned. When None, no filter — all bounces in inbox.

        Returns: list of BounceRecord, ordered newest-first (matches Gmail's
        default messages.list order)."""
```

Implementation notes:
- Use `users.messages.list(userId='me', q=<query>, pageToken=...)` paginated.
- Parse `internalDate` (ms since epoch) into a `datetime` for `bounce_date`.
- The body may be base64url-encoded inside `payload.parts[].body.data`; decode with `base64.urlsafe_b64decode(data + '===')` (padding-safe).
- For multipart bounces, the `text/plain` part with `Content-Type: message/delivery-status` is the canonical location of the `Final-Recipient` header; fall back to scanning the whole body if not found in a delivery-status part.
- A malformed body where the regex finds nothing should log a warning (`logger.warning("bounce message <id> has no Final-Recipient; skipping")`) and continue, not raise. This matches the test contract.

### 2. `scripts/poll_bounces.py`

```python
# scripts/poll_bounces.py
"""Poll Gmail for bounce notifications and append fresh recipients to
data/suppression.csv. Standalone — does not require a campaign-dir."""

# CLI:
#   python scripts/poll_bounces.py [--since-message-id <id>]
```

Control flow:

1. **Lockfile.** Acquire `data/.poll.pid` via `fcntl.flock(LOCK_EX | LOCK_NB)`. On failure print `"Another poll_bounces.py is running. Holder PID: <pid>. Exiting."` and exit 1.
2. **Authorize.** Call `lib.gmail.authorize(credentials_path, token_path, scopes=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"])`. The scope-superset check in `authorize()` will trigger a fresh OAuth flow if the existing `token.json` is `gmail.send`-only. Capture stdout-printed re-auth message — that's the user-visible signal that a browser is about to open.
3. **Load state.** Read `data/poll_bounces_state.json` if it exists:
   ```json
   {"last_processed_message_id": "abc123", "last_polled_at": "2026-05-21T14:00:00Z"}
   ```
   If absent, treat as first run.
4. **Construct `GmailClient(creds)`** and call `gmail.list_bounces(since_message_id=last_processed_message_id)`. CLI flag `--since-message-id` overrides the state-file value (useful for one-off reprocessing).
5. **Load Deduper.** Instantiate `Deduper(scope="all_campaigns")` and call `load_global()` — we need `is_suppressed()` to short-circuit dupes.
6. **Iterate bounces.** For each `BounceRecord`:
   - `email = record.original_recipient.lower().strip()`.
   - If `deduper.is_suppressed(email)` → log "already suppressed: <email>" at INFO, skip.
   - Else → call `deduper.append_suppressed(email, reason="hard_bounce", source=record.gmail_message_id)`. This is the existing append helper from section 03; it uses `fcntl.flock(LOCK_EX)` and appends a single `SuppressionRow` with `added_at=datetime.now(timezone.utc)`.
7. **Update state file.** After processing, write `data/poll_bounces_state.json` atomically (`.tmp` + `os.replace`) with `last_processed_message_id` = the newest seen message id (the first record returned by `list_bounces`, since results are newest-first). If `list_bounces` returned `[]`, leave the state file as-is. This is important so an empty poll doesn't lose your high-water mark.
8. **Observability.** Use a `StageObserver(stage="poll_bounces", ...)` rooted at a `CampaignObserver` pointing at a dedicated `campaigns/_global/` dir (or skip the campaign-observer integration entirely for poll_bounces — it's a global standalone, not part of any one campaign). Emit a summary line to `activity.log` and stdout: `"poll_bounces: examined <N> bounces, added <M> new suppressions, skipped <K> already-suppressed."`
9. **Exit codes.**
   - 0: ran cleanly, state updated.
   - 1: lock held by another process.
   - 2: Gmail auth failure or unexpected error (let it propagate after logging).

Error handling:
- Wrap the whole body in `try/except` with `obs.finish("FAILED", ...)` on any unhandled exception, then re-raise.
- Transient Gmail API errors (429, 5xx) are retried inside `list_bounces` per the existing `lib/gmail.py` retry pattern (3 attempts exp-backoff).
- A 401/403 from Gmail means the OAuth token is invalid — print the documented remediation message ("Re-run `python scripts/lib/gmail.py authorize`") and exit 2.

### 3. `scripts/setup_campaign.py`

A tiny helper, callable directly or via Claude Code at Stage 0.

```python
# scripts/setup_campaign.py
"""Initialize a new campaign directory with the expected layout."""

# CLI:
#   python scripts/setup_campaign.py --slug 2026-05_medium-retailers
```

Behavior:
1. Validate slug is kebab-case (matches the regex used in `lib/brief.py`'s `slug` validator).
2. Compute `campaign_dir = Path("campaigns") / slug`. Refuse if it already exists (exit 1 with "Campaign already exists: <path>").
3. `mkdir -p`:
   - `campaigns/<slug>/`
   - `campaigns/<slug>/progress/`
4. Copy `templates/_brief_template.yaml` → `campaigns/<slug>/brief.yaml`.
5. Touch empty `campaigns/<slug>/activity.log` and an initial `campaigns/<slug>/status.md` with a "NOT_STARTED" header.
6. Print the path and the next step: `"Created campaigns/<slug>/. Edit brief.yaml, then run scripts/source_domains.py --campaign-dir campaigns/<slug>"`.

No tests required for this helper (it's three filesystem operations); it gets exercised by the manual smoke test.

### 4. `CLAUDE.md` v2 (polish)

Rewrite the orchestrator in `CLAUDE.md` to incorporate lessons from the first real run. Required content:

- Stage 0 interview script, unchanged from v1 but with one explicit example transcript showing how to fill `brief.yaml` from a one-line ask.
- For each stage (1–5 plus 6), include a "Before this stage, read `playbooks/0X-<name>.md`" instruction. This makes the orchestrator delegate strategy questions to the playbooks rather than re-deriving them.
- A new "Common questions" section answering: "What if the user changes their mind about the segment mid-campaign?" (answer: brief-hash invariant refuses to run; revert brief or start fresh) — "What if port 25 is blocked?" — "What if Gmail OAuth expires?" — "How do I add a new template?" — "Where do I see costs?"
- A reference to `scripts/status.py --json` as the canonical way Claude Code checks pipeline state between actions.

### 5. Playbook fill-ins

By the time this section runs, sections 06–11 have already filled in playbooks 02–06. This section's job is to:

- Audit every `playbooks/*.md` for the five required headings (Purpose, When Claude reads this, Strategy, Common failure modes, Examples).
- Fill any missing sections in `playbooks/01-stage-zero-interview.md` (the Stage 0 interview playbook, which has no preceding section that filled it).
- Add `playbooks/07-bounce-polling.md` with content specific to this section:
  - Purpose: explain why bounce-polling is a separate cadence from sending (it's a cron-style job, not part of the linear pipeline).
  - Strategy: recommended cadence (after each test batch; weekly during bulk send; never during a send window because both write to `data/suppression.csv`).
  - Common failure modes: re-auth surprise on first run; clock-skew issues with `since_message_id` if the user manually deletes bounce emails; rate-limited Gmail searches on very large inboxes.
  - Examples: a transcript showing `poll_bounces.py` adding 3 hard bounces and how the user verifies them via `tail data/suppression.csv`.

### 6. `README.md` v2

Replace the section-01 v1 README with a complete user-facing document. Required sections:

1. **What this is** — one-paragraph elevator pitch.
2. **Prerequisites** — Python 3.12+, OpenAI API key (with budget), Workspace Gmail (not consumer Gmail), Dartmouth VPN or equivalent (port 25 access), 30 minutes for first-run OAuth.
3. **Install** — `uv sync`, `cp config/secrets.example.env config/secrets.env`, fill keys.
4. **First-run OAuth** — `python scripts/lib/gmail.py authorize` opens a browser. Document the "Testing" vs "Production" mode caveat (refresh tokens expire every 7 days in Testing mode).
5. **5-minute campaign walkthrough** (worked example) — a complete transcript:
   ```
   $ python scripts/setup_campaign.py --slug 2026-05_demo
   Created campaigns/2026-05_demo/. Edit brief.yaml, then run ...

   $ vim campaigns/2026-05_demo/brief.yaml
   # (paste in a sample brief targeting 5 domains)

   $ python scripts/source_domains.py --campaign-dir campaigns/2026-05_demo
   # (live status.md updates; ~30s)

   $ python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_demo
   # ...

   # Phase A test batch
   $ python scripts/send_emails.py --campaign-dir campaigns/2026-05_demo
   ════════════════════════════════════
   Test batch complete. Sent 5 emails ...

   # User checks Gmail Sent folder

   # Phase B bulk
   $ python scripts/send_emails.py --campaign-dir campaigns/2026-05_demo --confirm-test
   ```
6. **Bounce polling** — cron-style usage of `poll_bounces.py`; the re-auth notice on first run.
7. **Troubleshooting** — port 25 blocked, OAuth expired, brief invalid (exit 3 → fix the field named in the JSON error), Gmail daily-cap rollover.
8. **Layout** — one-paragraph description of `engine vs. campaign` so new contributors find their bearings.

### 7. `tests/manual/smoke_test_m4.md`

A markdown document, not Python. Contents are the manual smoke test described above under "Manual smoke test." Should be detailed enough that a fresh user can follow it without referring to any other file.

## Acceptance criteria for this section (and v1 as a whole)

- `pytest tests/test_poll_bounces.py tests/lib/test_gmail.py` is green.
- The full pytest suite (~80–120 tests across all sections) is green.
- A real `poll_bounces.py` run on a Gmail inbox with zero prior bounces: no changes to `data/suppression.csv`, `data/poll_bounces_state.json` updated with the current head message ID.
- After a real test send to known-invalid addresses (per the manual smoke test), `poll_bounces.py` adds them to `data/suppression.csv` with `reason=hard_bounce`.
- All `playbooks/*.md` have substantive content under all five required headings — no stubs remain.
- A fresh-clone user can follow `README.md` v2 end-to-end and produce a successful test-batch send.
- Re-running `poll_bounces.py` twice in a row with no new bounces is a clean no-op (idempotency).

## Files created or modified by this section

- `scripts/poll_bounces.py` (new)
- `scripts/setup_campaign.py` (new)
- `scripts/lib/gmail.py` (modified — adds `list_bounces` body)
- `CLAUDE.md` (modified — v2 rewrite)
- `README.md` (modified — v2 rewrite)
- `playbooks/01-stage-zero-interview.md` (modified — fill in)
- `playbooks/07-bounce-polling.md` (new)
- Any `playbooks/0X-*.md` still missing one of the five required headings (audit pass)
- `tests/test_poll_bounces.py` (new)
- `tests/lib/test_gmail.py` (modified — add `list_bounces` tests)
- `tests/manual/smoke_test_m4.md` (new)