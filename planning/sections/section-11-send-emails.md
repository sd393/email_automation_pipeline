Now I have all the information needed. Let me generate the section content.

# Section 11 — Send Emails (Stage 5)

This section implements **Stage 5** of the outreach pipeline: `scripts/send_emails.py`. It closes Milestone M3. The script reads the rendered `outbox.csv`, runs a 10-email **Phase A** test batch, prints a stdout banner instructing the user to verify Gmail Sent + inbox placement, then on re-invocation with `--confirm-test` runs **Phase B** until `outbox.csv` is exhausted or the daily cap is hit. Along the way it enforces a per-machine single-writer lockfile, hard-gates against the global suppression list, maintains a pessimistic daily-send counter, throttles each send with jitter, and appends every successful send to `data/master_contacts.csv` for cross-campaign dedup.

This section also adds `playbooks/06-sending.md` (substantive content for what Claude Code reads at Stage 5).

## Dependencies (must already exist; do not re-implement)

- **Section 03** — `lib/observability.py` (`CampaignObserver`, `StageObserver`), `lib/dedup.py` (`Deduper.is_suppressed`, `Deduper.append_contact` — both `fcntl.flock`-protected).
- **Section 04** — `lib/gmail.py` (`authorize`, `GmailClient.send`, `QuotaExceeded`, `SendResult`).
- **Section 10** — `scripts/compose_emails.py` produces `outbox.csv` with `OutboxRow` schema (`to_email`, `to_name`, `subject`, `body_html`, `body_plain`, `first_name_used`).
- **Section 02** — `lib/brief.py` (`Brief`, `load`), `lib/csv_schema.py` (`OutboxRow`, `SentLogRow`, `write_csv_row`, `read_csv`), `lib/progress.py` (`ProgressStore`, `write_brief_hash`/`check_brief_hash`).

## v1 cross-cutting invariants (recap; apply here verbatim)

- **Schema rules.** Every Pydantic model uses `model_config = ConfigDict(extra="forbid")`; every `Optional[X]` field has `default=None`.
- **Concurrency.** `data/master_contacts.csv` and `data/suppression.csv` are append-only under `fcntl.flock(LOCK_EX)`. Per-machine single-writer constraint for `send_emails.py` is enforced via `data/.send.pid` (separate from `data/.poll.pid`).
- **Brief stability.** Stage refuses to run if `sha256(brief.yaml bytes)` does not match `progress/brief_hash.txt`. Exit 2 with remediation.
- **Pessimistic counters.** Daily counter is incremented **before** the Gmail API call and decremented on hard failure. Caps over-send at 0 even across process kills.
- **Date keys.** `datetime.now().date()` (system local tz). No `zoneinfo`/`pytz`. Stale dates > 14 days pruned on read.
- **Exit codes.** 0 success; 1 refused operation (Phase B without `--confirm-test`); 2 stage failure (pre-flight, halt, FAILED); 3 brief validation error (JSON-on-stderr).
- **Observability.** Use `CampaignObserver` + per-stage `StageObserver`. Transient issues use `event(level="warn")`. Terminal failure: `finish(status="FAILED", ...)`.
- **Out of v1 scope (do NOT add):** `List-Unsubscribe` headers, postal address, reply detection, follow-up bumps, warmup, LLM cache, geo filtering, HTTPS unsubscribe.

---

## 1. Files to create or modify

| Path | Action | Purpose |
|---|---|---|
| `scripts/send_emails.py` | create | The Stage 5 script. |
| `playbooks/06-sending.md` | replace stub with substantive content | What Claude Code reads at Stage 5. |
| `tests/test_send_emails.py` | create | Per-spec test suite below. |
| `data/send_counters.json` | runtime artifact (gitignored) | Daily per-`from_gmail` counter. |
| `data/.send.pid` | runtime artifact (gitignored) | Single-writer lockfile sentinel. |
| `campaigns/<slug>/sent.log` | runtime output | Append-only `SentLogRow` per send. |
| `campaigns/<slug>/progress/send_emails.json` | runtime output | Per-row progress + `phase_a_complete` sentinel. |

## 2. CLI

```
python scripts/send_emails.py --campaign-dir <dir> [--resume] [--confirm-test]
```

- `--campaign-dir` (required): the campaign root containing `brief.yaml`, `outbox.csv`, `progress/`.
- `--resume`: continue from `progress/send_emails.json`. Refusing a partially-sent campaign without this flag is part of the replay-safety contract (see §6.4).
- `--confirm-test`: explicit user gate that flips from Phase A → Phase B.

## 3. Pre-flight (in this exact order; fail fast on first failure)

1. **Brief-hash check** — call `progress.check_brief_hash(campaign_dir)`. Mismatch → exit 2: `"Brief changed since previous stage. Revert brief.yaml or start a fresh campaign."`
2. **Brief validation** — `brief.load(campaign_dir / "brief.yaml")`. `BriefValidationError` → wrapper emits structured JSON to stderr, exit 3 (see invariants).
3. **Input-file check** — `outbox.csv` exists and has ≥ 1 data row. Empty → exit 2: `"Run compose_emails.py first."`
4. **Replay-safety check** — if `progress/send_emails.json` exists with any row marked `sent` AND `--resume` was NOT passed: exit 2: `"Partially-sent campaign detected. Re-run with --resume (or delete progress/send_emails.json to start over)."`
5. **Single-writer lockfile** — open `data/.send.pid` for writing, call `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. If `BlockingIOError` raised → exit 2: `"Another send_emails.py is running (data/.send.pid). Wait for it to finish."` Keep the fd open for the lifetime of the process; rely on OS close-on-exit to release. Write the current PID into the file (informational only — the lock, not the PID, is authoritative).
6. **Gmail authorize** — `gmail.authorize(credentials_path, token_path, scopes=["https://www.googleapis.com/auth/gmail.send"])`. The helper handles scope-superset detection (see section 04).

## 4. Phase decision logic (formal spec from claude-plan.md §6.2)

Read `progress/send_emails.json`. Treat it as a JSON dict with row-keyed entries plus optional top-level non-row keys.

Define:
- `n_sent := |{key in progress : progress[key].status in ("sent", "skipped_suppressed")}|`. Per-row `error` does NOT count.
- `phase_a_complete := progress.get("phase_a_complete", False)` — a **top-level** key, NOT a row key. Use a sentinel key like `"__phase_a_complete__"` (or namespaced) so it cannot collide with a `to_email` key.

Branches:

| `phase_a_complete` | `n_sent` vs `send_test_count` | `--confirm-test` | Action |
|---|---|---|---|
| False / missing | `n_sent < send_test_count` | (don't care) | **Phase A — Test Batch.** |
| False / missing | `n_sent >= send_test_count` | (don't care) | Set sentinel `phase_a_complete=true`, write Phase A completion banner to stdout, persist progress, exit 0. (Next invocation transitions.) |
| True | — | absent | Refuse: `"Test batch complete. Re-run with --confirm-test to send the bulk."` Exit 1. |
| True | — | present | **Phase B — Bulk.** |

`send_test_count` comes from `brief.sending.send_test_count` (default 10 per spec).

## 5. Common loop body (Phases A and B share this)

Iterate over `outbox.csv` rows in order. For each row, key by `to_email` in `ProgressStore`.

1. Skip if `progress.is_done(to_email)` (terminal statuses for this stage: `sent`, `skipped_suppressed`, `terminal_error`). `error` is retriable on resume.
2. **Hard-gate suppression** — `deduper.is_suppressed(row.to_email)` → mark `skipped_suppressed`, append a `SentLogRow(status="skipped_suppressed")` to `sent.log`, advance counters (counts toward `n_sent`).
3. **Hard-gate daily counter** (pessimistic accounting; see §6):
   - Acquire `fcntl.flock(LOCK_EX)` on `data/send_counters.json`.
   - Load JSON. Prune any date keys older than 14 days (`today - timedelta(days=14)`).
   - `today_key = date.today().isoformat()` (system local tz).
   - `current = counters[today_key].get(brief.sending.from_gmail, 0)`.
   - If `current >= brief.sending.send_rate_per_day`: print rollover banner (`"Daily cap reached for <from_gmail>. Re-run tomorrow to continue."`), release lock, persist progress, exit 0.
   - Else **increment**: `counters[today_key][from_gmail] = current + 1`. Persist atomically (`.tmp` + rename). Release lock.
4. Build kwargs from the brief: `from_address=brief.sending.from_gmail`, `from_name=brief.sending.from_name`, `reply_to=brief.sending.reply_to`. Subject/body come from the `OutboxRow`.
5. Call `gmail_client.send(...)`.
   - **Success**: append `SentLogRow(status="sent", gmail_message_id=...)`. Append to `master_contacts.csv` via `deduper.append_contact(email, domain, name, role, campaign_slug)` (uses its own `fcntl.flock`). Mark progress `sent`. **Send-as warning**: if the returned `From` header differs from `brief.sending.from_gmail` (Gmail rewriting), call `obs.event(level="warn", message="from rewritten from X to Y")` but do NOT fail the row.
   - **`QuotaExceeded`** (or HTTP 429, or `"Daily user sending limit exceeded"`): retry with exponential backoff `[1s, 2s, 4s, 8s, 16s, 32s]` + uniform jitter (cap at 3 attempts per row). If all three fail → **decrement** the counter under flock (we never sent), mark `error`, continue to next row.
   - **Hard failure** (4xx other than 429): decrement counter under flock, mark `error`, continue.
6. **Throttle**: `time.sleep(brief.sending.throttle_seconds * random.uniform(0.5, 1.5))`. Skip the sleep on the very last row in the loop.
7. `obs.tick(counters={...})` so `status.md` updates.

### Why pessimistic?
Increment-before-send means if the process is killed between `gmail.send()` returning success and `progress.mark("sent")` being persisted, the counter is one ahead of what's recorded. The next run sees one less slot. We over-throttle (cost: a few seconds of waiting) rather than over-send (cost: 24-hour Gmail lockout). Decrement-on-hard-failure recovers slots only when we are certain the send did not happen.

## 6. Phase-specific behavior

### Phase A — Test Batch

- Loop body as above, but stop after exactly `send_test_count` rows reach a terminal status counted by `n_sent` (i.e., `sent` or `skipped_suppressed`).
- **Phase A error semantics** (per claude-plan.md §6.2): a row that errors 3 times in a row across retries/invocations is marked `terminal_error` (NOT counted toward `n_sent`). Phase A advances past it and pulls the next row from `outbox.csv`. This prevents a single broken recipient from blocking the test batch forever.
- **Phase A halt budget**: if more than half of the first `2 * send_test_count` attempted rows go to `terminal_error`, call `obs.finish(status="FAILED", ...)` with a diagnostic: `"Phase A failure rate too high (N of M rows terminal_error). Check Gmail auth / network. See activity.log."` Exit 2.
- After `send_test_count` real sends: persist `phase_a_complete=True` sentinel into the progress JSON, print the test-batch banner exactly as below to stdout (NOT just to status.md), exit 0.

Test-batch stdout banner (literal, including the box-drawing characters):

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

Substitute `<from_gmail>` from the brief and `<n>` as `count(outbox rows) - n_sent`.

### Phase B — Bulk

- Same loop body. No early stop. Continues until either:
  - `outbox.csv` is exhausted (call `obs.finish(status="COMPLETED", ...)`, exit 0), OR
  - `send_rate_per_day` is hit for `from_gmail` today (print rollover message, persist progress, exit 0; the next-day invocation resumes and continues).

## 7. Tests — TDD first (write before the implementation)

All tests live in `tests/test_send_emails.py`. Mock the Gmail client entirely; use `freezegun` (or a clock-injection seam) for date/throttle behavior; use `tmp_path` fixtures from `conftest.py` for the campaign dir.

```python
# tests/test_send_emails.py

# --- Phase A ---
# Test: 12 OutboxRows, send_test_count=10 → exactly 10 sent in Phase A, exit 0, banner printed to stdout.
# Test: After Phase A, progress/send_emails.json has phase_a_complete=true sentinel (top-level key).

# --- Phase A error semantics (review issue #4) ---
# Test: 12 OutboxRows; mock 3 of the first 10 to error 3 times each → those marked terminal_error;
#       Phase A advances to rows 11-13 to fill the test batch. Final n_sent == 10 real sends.
# Test: 12 OutboxRows; mock 11 to error permanently → Phase A halts with diagnostic, exit 2.

# --- Phase A → Phase B transition ---
# Test: 12 rows, run Phase A (10 sent), re-run WITHOUT --confirm-test → refuse, exit 1.
# Test: 12 rows, run Phase A, re-run WITH --confirm-test → 2 more sent, exit 0.

# --- Suppression hard-gate ---
# Test: 1 row's to_email is in data/suppression.csv → marked skipped_suppressed, gmail.send NOT called,
#       counts toward n_sent in Phase A.

# --- Pessimistic counter (review issue #3) ---
# Test: counter incremented BEFORE gmail.send; on hard failure (mock raises), counter decremented.
# Test: simulate kill between gmail-success and progress.mark("sent") → counter is 1-higher than
#       persisted sent rows; next run with --resume sees correct remaining cap and does NOT over-send.
# Test: counter date-keyed by datetime.now().date() (system local tz; use freezegun to inject).
# Test: stale dates (>14 days old) pruned on counter read.
# Test: hitting send_rate_per_day mid-run → exit 0 with "rolled over" message; next-day invocation
#       (clock advanced 1d) resumes cleanly, no duplicates.

# --- Quota / retry ---
# Test: gmail.send raises QuotaExceeded 3 times in a row → 3 retries with exp backoff (mocked sleep);
#       after the 3rd failure: counter decremented, row marked error, loop continues.
# Test: gmail.send raises QuotaExceeded once, succeeds on retry 2 → row marked sent, counter NOT
#       decremented (the eventual send used the slot we reserved).

# --- Throttle jitter ---
# Test: 10 rows with throttle_seconds=1.0; with a mocked clock summing sleep arguments, total sleep
#       lies in [5.0, 15.0] (uniform(0.5, 1.5) × 10). No sleep on the last row.

# --- master_contacts.csv ---
# Test: every successful send appends one row to data/master_contacts.csv via deduper.append_contact;
#       the file lock is acquired and released around each append.

# --- Concurrency / lockfile (review issue #2) ---
# Test: when data/.send.pid is already held by another process (simulate by holding the flock in a
#       second fd), invoking send_emails.py exits 2 with the documented message and does not
#       touch outbox.csv / sent.log / counter.

# --- Pre-flight ---
# Test: missing outbox.csv → exit 2 with "Run compose_emails.py first."
# Test: brief-hash mismatch → exit 2.
# Test: brief.yaml fails validation → exit 3 with structured JSON on stderr per error contract.

# --- Replay safety ---
# Test: progress shows some rows already 'sent', running without --resume → exit 2 with
#       "Partially-sent campaign detected. Re-run with --resume..."

# --- Send-as warning ---
# Test: gmail.send returns a SendResult whose 'from' field differs from brief.sending.from_gmail →
#       a warn-level line is appended to activity.log; the row is still marked sent.
```

### Test fixtures and harness notes

- Reuse `tmp_campaign_dir` and `sample_brief` from `tests/conftest.py` (created in section 02).
- Stub Gmail by patching `scripts.send_emails.GmailClient` (or wherever it is imported) with a `Mock` that implements `send(...)`. Provide canned return values + side effects (`raise QuotaExceeded`, raise generic `Exception`, return `SendResult` with mismatched `from`).
- For the lockfile concurrency test: open `data/.send.pid` in the test, acquire `fcntl.flock(LOCK_EX | LOCK_NB)`, then invoke `main()` and assert exit code 2 + the documented message.
- For the kill-mid-send test: monkeypatch `progress.mark` so that the first time it is called with `status="sent"` it raises `SystemExit(137)` (simulating SIGKILL after gmail.send returned but before persistence). Then call `main()` again with `--resume` and assert the counter shows 1 reserved-and-spent slot more than the persisted-sent rows, and no duplicate row is sent.
- For the throttle test: monkeypatch `time.sleep` to record its arguments into a list; assert `sum(args) in [5.0, 15.0]` (inclusive on both ends because of `uniform`'s closed interval).
- Use `freezegun.freeze_time("2026-05-21")` for date-key tests; advance with `tick(timedelta(days=1))` for next-day rollover.

## 8. Implementation skeleton (signatures only — do NOT write the full implementation here)

```python
# scripts/send_emails.py

"""Stage 5: read outbox.csv, run Phase A (test batch) then Phase B (bulk on --confirm-test)."""

PHASE_A_COMPLETE_KEY = "__phase_a_complete__"  # sentinel; not a valid email so cannot collide
TERMINAL_STATUSES = {"sent", "skipped_suppressed", "terminal_error"}
COUNTED_STATUSES = {"sent", "skipped_suppressed"}  # what counts toward n_sent

def parse_args() -> argparse.Namespace: ...

def preflight(args, brief) -> tuple[ProgressStore, Deduper, GmailClient, IO]:
    """Returns (progress, deduper, gmail, lockfile_handle). Exits on any pre-flight failure."""

def acquire_send_lock(data_dir: Path) -> IO:
    """fcntl.flock(LOCK_EX|LOCK_NB) on data/.send.pid; writes pid; returns fd to hold open."""

def decide_phase(progress: ProgressStore, send_test_count: int,
                 confirm_test: bool) -> Literal["A", "A_finalize", "B", "refuse"]: ...

def read_counter(path: Path) -> dict: ...
def write_counter(path: Path, counter: dict) -> None: ...  # .tmp + rename, under flock

def increment_today(counter_path: Path, from_gmail: str,
                    cap: int) -> tuple[bool, int]:
    """Acquire flock; prune stale dates; if today < cap, increment and persist;
    return (ok, current_after_increment)."""

def decrement_today(counter_path: Path, from_gmail: str) -> None: ...

def send_one(row: OutboxRow, brief: Brief, gmail: GmailClient,
             deduper: Deduper, obs: StageObserver,
             counter_path: Path, sent_log_path: Path,
             progress: ProgressStore, campaign_slug: str) -> None:
    """One iteration of the loop body. Marks progress before returning."""

def phase_a(...): ...
def phase_b(...): ...
def main() -> int: ...

if __name__ == "__main__":
    raise SystemExit(main())
```

## 9. `playbooks/06-sending.md` — required content sections

Replace the stub with substantive prose. Required sections (~150–300 words each is plenty):

- **Purpose** — what Stage 5 does, where it sits in the pipeline.
- **When Claude reads this** — at the moment Claude Code is about to invoke `send_emails.py`, BEFORE the Phase A invocation, and AGAIN before Phase B.
- **Test-batch philosophy** — why we send 10 first, what the user is checking for (rendering, inbox vs spam, link clickability), what failure modes look like (bulk into spam → STOP, don't re-run with `--confirm-test`).
- **Throttle rationale** — why `throttle_seconds * uniform(0.5, 1.5)`: spaces sends so Gmail's anti-burst heuristics don't trigger; jitter de-correlates the cadence so it doesn't look like an automated mailer.
- **Daily-cap-rollover behavior** — how the script exits cleanly at the cap; how the user re-invokes the next day.
- **Common failure modes** —
  - `QuotaExceeded` repeatedly: token might be stale, Workspace tenant might be flagged. Remediation: pause for 24h, then send a tiny test.
  - `From rewritten` warning: Gmail "Send mail as" not configured. Not fatal but mention to user.
  - Lockfile collision: another invocation is running.
  - Suppression hits during Phase A: indicates the brief is using stale dedup scope; not blocking.

## 10. Acceptance criteria

This section is done when:

1. `uv run pytest tests/test_send_emails.py` is green.
2. A manual small-campaign run (12 outbox rows, brief `send_test_count=10`):
   - First invocation sends exactly 10 real emails (verifiable in the user's Gmail Sent folder), prints the documented stdout banner, exits 0, and persists `__phase_a_complete__=true` in `progress/send_emails.json`.
   - Second invocation without `--confirm-test` exits 1 with the refusal message.
   - Third invocation with `--confirm-test` sends the remaining 2, exits 0.
3. Adding a recipient's email to `data/suppression.csv` between invocations prevents that recipient from being sent to (`skipped_suppressed`).
4. Hitting `send_rate_per_day` mid-run exits 0 with the rollover message; rolling the system clock forward 1 day and re-invoking with `--resume` continues cleanly, no duplicates.
5. The `data/.send.pid` lockfile prevents a second concurrent invocation with the documented message.
6. `playbooks/06-sending.md` is substantive (not a stub) and covers all six required sections.

## 11. Notes for the implementer (gotchas)

- The phase-decision logic is fiddly. Step through it on paper with `phase_a_complete ∈ {missing, false, true} × n_sent ∈ {< target, == target, > target} × --confirm-test ∈ {absent, present}` before writing code. Most bugs hide in the "n_sent == target but sentinel not yet set" boundary.
- `fcntl.flock` is advisory and POSIX-only — fine for macOS (the user's platform per env). Use `fcntl.LOCK_EX | fcntl.LOCK_NB` for the `.send.pid` non-blocking lock; use `fcntl.LOCK_EX` (blocking) for the per-write counter lock.
- Counter file writes MUST be atomic: write to `data/send_counters.json.tmp`, then `os.replace(tmp, real)`. The append-only `master_contacts.csv` and `suppression.csv` pattern from `lib/dedup.py` does NOT apply here — counters are a small JSON file we rewrite each tick under flock.
- The `__phase_a_complete__` sentinel key in `progress/send_emails.json` must not collide with any `to_email` value. Email addresses contain `@`, the sentinel is wrapped in underscores; collision impossible by construction.
- When decrementing on hard failure, guard against the counter ever going negative (clamp at 0). Negative counters indicate a logic bug; log a warn but do not crash.
- The throttle sleep is at the END of the loop body, not the start, so the very first send is not delayed and the very last send is not followed by an unnecessary wait.
- On `QuotaExceeded` retries, the slot was already reserved by the pre-send increment. Do NOT increment again on retry, and do NOT decrement after a successful retry. Only decrement if all 3 retries fail.

---

Relevant absolute file paths the implementer will produce or modify:

- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/send_emails.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/06-sending.md`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/test_send_emails.py`