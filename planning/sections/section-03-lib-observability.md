Now I have a good understanding. Let me write the section content.

# section-03-lib-observability

## Purpose and scope

This section implements the **observability** and **cross-process coordination** primitives that every stage of the outreach pipeline depends on. After this section, all subsequent stages can:

1. Write live progress to `status.md` and an append-only `activity.log` via a clean two-class API (`CampaignObserver` + `StageObserver`).
2. Append rows to the shared `data/master_contacts.csv` and `data/suppression.csv` files safely from multiple processes via `Deduper`, using `fcntl.flock` rather than the (incorrect) `.tmp`+rename pattern.
3. Acquire the `data/.send.pid` and `data/.poll.pid` lockfiles that enforce the per-machine single-writer constraint for `send_emails.py` and `poll_bounces.py`.

This section depends on **section-02-lib-foundations** (it uses `lib/csv_schema.py`'s `MasterContactRow` / `SuppressionRow`, the shared `tests/conftest.py` fixtures, and the atomic-write helper used inside `lib/progress.py`). It does NOT depend on `lib/llm.py` or `lib/gmail.py`, so it can be implemented in parallel with section-04.

It blocks: section-05 (orchestration uses the observers), section-06 / 07 / 09 (every stage script wires up a `StageObserver`), section-11 (send uses both the dedup writers and the `.send.pid` lockfile).

## Files to create

| Path | What it contains |
|---|---|
| `scripts/lib/observability.py` | `CampaignObserver`, `StageObserver` classes + `status.md` template rendering |
| `scripts/lib/dedup.py` | `Deduper` class + module-level lockfile helpers (`acquire_send_lock`, `acquire_poll_lock`) |
| `tests/lib/test_observability.py` | Unit tests below |
| `tests/lib/test_dedup.py` | Unit tests below |

Both library files live under `scripts/lib/` (the package layout established in section-01). Tests live under `tests/lib/` per the conventions in `tests/conftest.py` from section-02.

## Background context (required reading)

### Observability split (review issue #8)

The split between **`CampaignObserver`** (singleton per campaign) and **`StageObserver`** (one per stage invocation) exists to resolve an earlier design ambiguity where a single `Observer` class was responsible for both campaign-level state (which stages have completed, total cost across stages) and stage-level state (current row count, current cost, last event). Splitting them makes cross-stage state survive process boundaries cleanly: each stage script instantiates its own `StageObserver`, which holds a reference to a `CampaignObserver` that reads/writes `campaigns/<slug>/observer_state.json`.

Semantics rules to preserve exactly:

- `event(level="warn")` is a **transient** signal (rate-limit retry, greylist retry). It writes a `WARN` line to `activity.log` and does NOT change the stage's status.
- There is **no** `event(level="error")`. The only way a stage transitions to `FAILED` is `finish(status="FAILED", ...)`. The stage script's `main()` catches its own unhandled exceptions and calls `finish(FAILED)` in a `finally` block before re-raising.
- `finish(status="COMPLETED", summary)` is what rolls the stage cost into the campaign-level total via `CampaignObserver.stage_complete(...)`.
- After a stage finishes COMPLETED, its section in `status.md` collapses to a one-line summary; the next stage's section appears below.
- Cadence rule: emit a milestone when EITHER `current_count - last_emit_count >= cadence_items` OR `time.monotonic() - last_emit_time >= cadence_seconds`. Per-stage defaults are configurable via constructor args (defaults: `cadence_items=50`, `cadence_seconds=120`).

### `status.md` template (rewritten from in-memory state on every milestone)

```
# <slug> — <STATUS> (stage N of 5: <stage name>)

Domains sourced:   1,491 / 1,500  [check]
Contacts found:    612 companies processed (41%)
Emails verified:   1,134 verified
Cost so far:       $18.40
Last event:        2026-05-21 14:03  verified aforch@huckberry.com
ETA this stage:    ~22 min
```

The exact rendering helper (how counters are formatted, which lines appear conditionally) is an implementation detail. The shape must match what's tested: a header banner with overall status, per-stage counter lines for completed/running stages, a "Cost so far" line that aggregates across stages, a "Last event" line.

### `activity.log` format

```
2026-05-21T14:03:21.105Z  [verify]  INFO   verified aforch@huckberry.com
2026-05-21T14:03:22.901Z  [verify]  INFO   milestone: 612/1491 (41.0%) verified=1134 catchall=148 cost=$18.40 elapsed=22m
2026-05-21T14:03:23.412Z  [verify]  WARN   greylist retry scheduled for foo@bar.com (90s)
```

Lines are ISO-8601 UTC timestamps, stage tag in brackets, level name (`INFO` or `WARN`), then the message. Append-only; never rewritten.

### Concurrency model for `lib/dedup.py` (review issue #2)

The earlier design said `data/master_contacts.csv` was rewritten atomically via `.tmp`+rename on every change. **That was wrong** for an append-only growing file: writing N rows would cost O(N²) I/O and would conflict with a concurrent reader (e.g., a `send_emails.py` reading suppression while `poll_bounces.py` writes to it).

Correct model:

- All writes to `data/master_contacts.csv` and `data/suppression.csv` use `fcntl.flock(fd, LOCK_EX)` held for the duration of a single-row `open(path, "a")` append. The lock serializes concurrent appenders.
- Reads use `LOCK_SH` so they don't block each other but DO block while a writer is mid-append.
- No full-file rewrites. The CSV grows monotonically.
- Idempotency on read: `Deduper.load_global()` builds the in-memory set from the file; duplicate rows for the same email are collapsed at that step (last-writer wins for non-key fields).

### Per-machine single-writer lockfiles

Two scripts hold long-running per-machine exclusive locks:

- `data/.send.pid` — held by `send_emails.py` for the entirety of a send run.
- `data/.poll.pid` — held by `poll_bounces.py` for the entirety of a poll run.

These are sentinel files; the lock is held via `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on an open file descriptor that's kept alive for the lifetime of the process. The file's contents are the holder's PID (informational only — the lock, not the PID, is what enforces exclusivity). If `flock` raises `BlockingIOError`, print a clean message including the PID from the file and exit with code 2.

The lockfiles live in `data/`, NOT in any campaign folder, because the constraint is **per-machine, not per-campaign**: two different campaigns can't both `send_emails.py` simultaneously because they both append to the shared `master_contacts.csv` and want to pick up each other's bounce suppressions.

## Public API stubs

### `scripts/lib/observability.py`

```python
class CampaignObserver:
    """Singleton per campaign. Owns campaign-level header in status.md and the
    observer_state.json file that tracks cross-stage cost roll-up + completed stages."""

    def __init__(self, campaign_dir: Path): ...

    def stage_complete(self, stage: str, summary: dict) -> None:
        """Record stage completion + summary in observer_state.json; rewrite
        status.md preserving prior completed stages and showing the new one as
        COMPLETED with a one-line summary."""

    def total_cost(self) -> float:
        """Sum of all stage costs to date from observer_state.json."""

class StageObserver:
    """One per stage invocation. Owns the stage-specific section of status.md
    and the activity.log lines for this stage. Holds a reference to its parent
    CampaignObserver."""

    def __init__(self, campaign_obs: CampaignObserver, stage: str,
                 cadence_items: int = 50, cadence_seconds: int = 120): ...

    def stage_start(self) -> None:
        """Mark stage RUNNING in status.md, log a 'stage X starting' event."""

    def event(self, message: str, level: Literal["info", "warn"] = "info") -> None:
        """Append a timestamped line to activity.log. Always emits.
        NOTE: 'error' is NOT a valid level here — transient errors use 'warn',
        terminal failures use finish(status='FAILED'). This avoids the
        ambiguity of 'one error => FAILED stage'."""

    def tick(self, counters: dict[str, int | float | str]) -> None:
        """Update internal counters. If cadence threshold crossed:
           - append [stage] milestone line to activity.log,
           - print [stage] milestone line to stdout,
           - rewrite stage section of status.md.
        Cadence: emit when (current_count - last_emit_count >= cadence_items)
        OR (time.monotonic() - last_emit_time >= cadence_seconds)."""

    def finish(self, status: Literal["COMPLETED", "FAILED"], summary: dict) -> None:
        """Terminal call. On COMPLETED, delegates to CampaignObserver.stage_complete.
        On FAILED, sets stage FAILED in status.md and prints traceback location
        to stdout. Only path to FAILED — bare event() calls never set FAILED."""
```

`observer_state.json` schema (rough):

```json
{
  "slug": "2026-05_medium-retailers",
  "stages": {
    "source": {"status": "COMPLETED", "cost": 4.20, "summary": {...}, "completed_at": "..."},
    "discover": {"status": "RUNNING", "started_at": "..."}
  },
  "total_cost": 4.20
}
```

### `scripts/lib/dedup.py`

```python
class Deduper:
    """Cross-campaign suppression + master-contacts dedup.
    Always-global suppression. scope flag only affects is_known() lookups."""

    def __init__(self, scope: Literal["this_campaign", "all_campaigns"]): ...

    def load_global(self) -> None:
        """Load data/master_contacts.csv + data/suppression.csv into memory
        under fcntl.flock(LOCK_SH)."""

    def is_suppressed(self, email_or_domain: str) -> bool: ...

    def is_known(self, email_or_domain: str) -> bool:
        """True if seen in any prior campaign. Only checked when scope=all_campaigns;
        always False when scope=this_campaign."""

    def append_contact(self, email: str, domain: str, name: str, role: str,
                       campaign_slug: str) -> None:
        """Append a MasterContactRow to data/master_contacts.csv. Acquires
        fcntl.flock(LOCK_EX) on the file. Plain open(path, 'a') append. If file
        doesn't exist, write header first under the lock."""

    def append_suppressed(self, email: str, reason: str, source: str) -> None:
        """Append a SuppressionRow to data/suppression.csv. Same lock model."""

    def reload(self) -> None:
        """Re-read both files under LOCK_SH. Used by long-running send loops
        to pick up bounces added by a concurrent poll_bounces.py."""


# Module-level lockfile helpers (per-machine single-writer enforcement)

def acquire_send_lock(data_dir: Path = Path("data")) -> int:
    """Acquire data/.send.pid via fcntl.flock(LOCK_EX | LOCK_NB).
    Writes os.getpid() to the file. Returns the open fd (caller keeps it alive
    for the lifetime of the process; the lock releases on close/exit).
    On BlockingIOError, print 'send_emails.py is already running (pid=<N>).
    Wait for it to finish or kill it.' and exit 2."""

def acquire_poll_lock(data_dir: Path = Path("data")) -> int:
    """Same model for data/.poll.pid."""
```

Both lockfiles are created with `O_CREAT | O_RDWR` mode 0644. The PID is written for diagnostics only — the actual exclusion mechanism is the `flock`.

## Implementation notes

### Observability

- **Time**: Use `time.monotonic()` for cadence decisions (immune to wall-clock jumps). Use `datetime.now(timezone.utc).isoformat()` for `activity.log` timestamps. Make the monotonic clock injectable (via a private `_now` attribute that defaults to `time.monotonic`) so the cadence-by-time test can pass a mocked clock.
- **`status.md` rendering** must be idempotent: rendering the same in-memory state twice produces byte-identical files. This avoids spurious diffs and makes the file safe to `cat` repeatedly.
- **Cross-stage handoff**: when stage 2 starts, the file produced by stage 1's `finish(COMPLETED)` must already include stage 1's banner. `CampaignObserver` re-reads `observer_state.json` on every `stage_complete()` so a fresh process picks up prior state. Don't keep stale in-memory state across stages.
- **Stdout milestones**: print to `sys.stdout`, flush immediately. The line should be the same string that goes into `activity.log` (minus the timestamp/level prefix, which stdout doesn't need).

### Dedup

- **In-memory state**: `load_global()` builds two `set[str]`s — `_suppressed_emails` and `_known_emails`. `is_suppressed` and `is_known` are O(1) dict lookups. For domains, build a parallel `_known_domains: set[str]` (used by `source_domains.py` for scope=all_campaigns dedup).
- **Idempotency on read** (per the test): if `suppression.csv` contains the same email twice (because two concurrent `poll_bounces.py` runs raced before the lock was added, or because a manual edit), `_suppressed_emails` is still a set; the duplicate is collapsed. No exception, no warning.
- **append-only**: never call `truncate()` or rewrite the file. The only file primitive used is `open(path, "a")` under `LOCK_EX`. If the file doesn't exist, the helper writes the CSV header inside the locked region first, then the row. Use `os.path.exists()` BEFORE opening to decide whether the header is needed (this is technically racy but harmless: two processes both seeing "no file" both write a header, but the lock serializes them and the second one sees the file and only writes the row — make sure to re-check inside the lock).

### Lockfile mechanics

- Open the file `os.open(path, O_CREAT | O_RDWR, 0o644)`. Call `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`. On `BlockingIOError`, read the file's existing contents (the PID), format the error message, exit 2.
- On success, `os.ftruncate(fd, 0)`, write `str(os.getpid()).encode()`, `os.fsync(fd)`.
- Return the open fd. The caller (i.e., `scripts/send_emails.py`) keeps it in a variable for the process's lifetime. The lock auto-releases on `close()` or process exit. Do NOT close it.

## Tests (TDD — write these first)

### `tests/lib/test_observability.py`

```python
# CampaignObserver tests
# Test: instantiation in empty campaign dir creates observer_state.json + empty status.md.
# Test: stage_complete("source", summary) updates status.md preserving prior stages.
# Test: total_cost() sums per-stage costs from observer_state.json.

# StageObserver tests
# Test: stage_start() writes "stage X starting" event + sets status.md section to RUNNING.
# Test: cadence by items — 50 ticks at cadence_items=50 → exactly one milestone line emitted.
# Test: cadence by time — 1 tick + 121s elapsed (mocked monotonic clock) → milestone emitted.
# Test: cadence reset — after a milestone fires, next milestone needs another full window.
# Test: status.md content matches the template (header banner, counters, last event).
# Test: activity.log lines are ISO-timestamped and ordered.
# Test: event(level="warn") writes WARN to activity.log; does NOT change stage status.
# Test: finish(status="COMPLETED", summary) transitions stage to COMPLETED + updates CampaignObserver.
# Test: finish(status="FAILED", summary) sets stage FAILED in status.md, prints traceback location.
# Test: a stage that calls finish(FAILED) and then exits is the ONLY path to FAILED — bare event()
#       calls (even repeated warn events) never set FAILED (review issue #8 semantics).
# Test: cross-stage handoff — after Stage 1 finish, Stage 2 instantiation preserves Stage 1 banner
#       in status.md (verifies observer_state.json round-trip across processes).
# Test: total cost shown in status.md is sum of stage costs across the campaign, not just current stage.
```

Fixtures expected from `tests/conftest.py` (section-02): `tmp_campaign_dir` (a freshly created `campaigns/<slug>/` tmp path), `sample_brief`. Use `mocker.patch("time.monotonic", ...)` or inject the clock via the `StageObserver`'s constructor for cadence-by-time.

### `tests/lib/test_dedup.py`

```python
# Test: is_suppressed returns True for an email in suppression.csv; False otherwise.
# Test: is_known returns True for an email in master_contacts.csv when scope=all_campaigns.
# Test: is_known returns False for the same email when scope=this_campaign.
# Test: append_contact appends a single row; doesn't rewrite the whole file
#       (compare os.stat inode + size delta before/after — size grew by exactly one row).
# Test: append_suppressed appends; if the same email is appended twice, load_global's
#       in-memory set still has one entry (read-side idempotency).
# Test: concurrent appends from two processes (multiprocessing.Process) — both rows
#       land in the final file. Verify by counting rows after both join().
# Test: file lock blocks a second writer until the first releases — second writer's append waits.
#       (Use a sleep+signal pattern: first process acquires lock, sleeps 0.5s while holding;
#        second process times its append duration; should be ≥ 0.5s.)
# Test: acquire_send_lock — calling it once succeeds, returns an fd > 0.
# Test: acquire_send_lock — calling from a second process while the first holds the lock
#       exits 2 with a message containing the holder PID. (Use multiprocessing + capsys/capfd.)
# Test: reload() picks up rows added by another process — after process A's load_global(),
#       process B appends, process A's reload() now sees the new row.
```

Fixtures: `tmp_data_dir` (a fresh `data/` directory pointed at by monkeypatched `Path("data")` or passed via parameter — see section-02's conftest for the agreed pattern).

## Acceptance criteria for this section

- `uv run pytest tests/lib/test_observability.py tests/lib/test_dedup.py` is green.
- All listed tests exist and pass; in particular:
  - The cadence-by-time test passes with a mocked monotonic clock.
  - The cross-process dedup test (using `multiprocessing`) shows both rows landing.
  - The lockfile contention test exits 2 with the holder's PID in the message.
  - The cross-stage handoff test confirms `observer_state.json` round-trips correctly.
- No `.tmp`+rename pattern anywhere in `dedup.py`. Only `fcntl.flock` + `open(..., "a")`.
- No `event(level="error")` codepath exists; mypy/static review would reject `Literal["info", "warn"]` deviations.
- `CampaignObserver` and `StageObserver` are separate classes with the documented division of responsibilities (review issue #8 satisfied).

## Out-of-scope for this section (handled elsewhere)

- The brief-hash invariant helper (`write_brief_hash` / `check_brief_hash`) lives in `lib/progress.py`, not here — see section-02 (declared) and section-05 (wired into `noop_stage.py`).
- The `gmail.list_bounces()` consumer that ultimately calls `append_suppressed(reason="hard_bounce", ...)` is section-12.
- `noop_stage.py`, which is the first script that wires up a `StageObserver` end-to-end, is section-05.
- The `MasterContactRow` and `SuppressionRow` Pydantic models that `append_contact` / `append_suppressed` serialize already exist from section-02 (`lib/csv_schema.py`); import them, don't redefine them.