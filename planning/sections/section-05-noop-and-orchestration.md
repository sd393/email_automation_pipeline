Now I have all the context I need. Let me create the section content for section-05-noop-and-orchestration.

# Section 05 — No-op Stage, Orchestration, and Brief-Hash Invariant

## 1. Purpose and scope

This section closes **Milestone M0**. By the end of it, the user can:

1. Run a no-op pipeline stage against any valid `brief.yaml` and watch `status.md` update live.
2. Inspect the state of any campaign via a read-only inspector (`scripts/status.py`).
3. Optionally run all pre-send stages back-to-back via `scripts/run_pipeline.py` (a thin sequential wrapper — useful from M1 onward).
4. Trust that any stage which re-uses a campaign refuses to run if the underlying `brief.yaml` has been mutated between stages (the brief-hash invariant).

Everything in this section is "plumbing." There is **no segment-specific logic, no LLM calls, no Gmail calls**. We are proving the cross-cutting libraries from sections 02/03/04 actually work together end-to-end before M1 builds the first real stage on top of them.

The no-op stage gets **deleted at the start of section 06** (M1). It is scaffolding, not product code. The status inspector and the pipeline runner, however, are permanent.

## 2. Dependencies (must be merged before this section starts)

- **section-02-lib-foundations** — `lib/brief.py`, `lib/progress.py`, `lib/csv_schema.py`, `lib/rate_limit.py`, `lib/dns_check.py`, and `tests/conftest.py` with shared fixtures (`sample_brief`, `tmp_campaign_dir`).
- **section-03-lib-observability** — `lib/observability.py` exposes `CampaignObserver` and `StageObserver` with the `stage_start` / `tick` / `event` / `finish` API. `lib/dedup.py` exposes `Deduper` with `fcntl.flock`-based appenders.
- **section-04-lib-llm-and-gmail** — `lib/llm.py` and `lib/gmail.py` exist. The no-op stage does NOT actually call either, but `run_pipeline.py` and `status.py` may need to import their exception types to format errors uniformly.

Do not start this section until those three are merged and their tests are green.

## 3. Cross-cutting invariants that THIS section is responsible for verifying

These invariants are declared in §10 of the master plan; this section is where the M0 acceptance tests for them live. Quoting the relevant ones inline so the implementer doesn't need to flip between documents:

**Concurrency**
- `ProgressStore` is thread-safe via internal `RLock`. The no-op stage exercises the single-threaded path; the concurrency test below stresses 100 threads against the store directly (it is a `lib/progress.py` test that this section reuses as a gate).

**Brief stability across stages**
- The first stage to use a brief writes `progress/brief_hash.txt = sha256(<brief.yaml file bytes>)`.
- Every subsequent stage checks this hash and refuses to run if it differs. Exit 2 with a clear remediation message.

**Exit codes**
- 0: success.
- 1: refused operation (e.g., Phase B without `--confirm-test`). User can re-invoke correctly.
- 2: stage failure (pre-flight failed, halt condition, FAILED finish, brief-hash mismatch).
- 3: brief validation error (structured JSON on stderr; Claude Code parses to fix the brief).

**Observability split**
- `CampaignObserver` (singleton per campaign): owns the `status.md` pipeline header and cross-stage cost roll-up. Reads + writes `observer_state.json`.
- `StageObserver` (one per stage invocation): owns the stage's section of `status.md` and all `activity.log` lines for the stage.
- `event(level="warn")` for transient issues; `finish(status="FAILED")` is the only path to a FAILED stage. There is no `event(level="error")`.

## 4. Files to create

| Path | Purpose | Permanent? |
|---|---|---|
| `scripts/lib/progress.py` (additions only) | Add `write_brief_hash(progress_dir, brief_bytes)` and `check_brief_hash(progress_dir, brief_bytes) -> bool` helpers. | Permanent |
| `scripts/noop_stage.py` | The M0 plumbing-verifier. Deleted at the start of section 06. | Temporary |
| `scripts/status.py` | Read-only campaign inspector (review issue #13). | Permanent |
| `scripts/run_pipeline.py` | Optional sequential runner (Stages 1–4; stops before Stage 5). | Permanent |
| `tests/test_noop_stage.py` | Acceptance tests for M0 plumbing. | Permanent |
| `tests/test_status.py` | Tests for `scripts/status.py`. | Permanent |
| `tests/test_run_pipeline.py` | Tests for `scripts/run_pipeline.py`. | Permanent |
| `tests/test_error_contract.py` | Tests that every script wrapper emits the documented exit-3 JSON contract. | Permanent |

The brief-hash unit tests themselves (`write_brief_hash` / `check_brief_hash` round-trips) live in `tests/lib/test_progress.py` — that file already exists from section 02; this section just adds two more test cases to it.

## 5. Brief-hash invariant helper (additions to `lib/progress.py`)

### 5.1 Function signatures

Add to the existing `scripts/lib/progress.py` (do NOT make a new file — these are tiny module-level helpers that belong next to `ProgressStore`):

```python
import hashlib
from pathlib import Path

def _brief_hash_path(progress_dir: Path) -> Path:
    """Return the canonical brief_hash.txt path inside a campaign's progress/ dir."""
    return progress_dir / "brief_hash.txt"

def write_brief_hash(progress_dir: Path, brief_bytes: bytes) -> None:
    """Write sha256(brief_bytes) hex digest to <progress_dir>/brief_hash.txt.
    Atomic via .tmp + os.replace. Idempotent: if the file already exists with
    the same content, no-op. If it exists with different content, OVERWRITE
    silently — callers are responsible for calling check_brief_hash() first
    if they want mismatch detection."""

def check_brief_hash(progress_dir: Path, brief_bytes: bytes) -> bool:
    """Return True if <progress_dir>/brief_hash.txt is absent OR matches
    sha256(brief_bytes). Return False ONLY when the file exists and differs.
    Absent file is treated as 'first time' (returns True) so the very first
    stage to touch a campaign doesn't trip the invariant."""
```

### 5.2 Hash content

The hash is `hashlib.sha256(brief_bytes).hexdigest()`. The input is the raw bytes of `brief.yaml` as read from disk — not the parsed Pydantic model, not normalized YAML. This means trailing whitespace and comment changes WILL trip the hash. That's intentional: the user should not be editing the brief between stages at all.

### 5.3 The standard pre-flight pattern (used by every stage from M1 onward)

Every real stage script (`source_domains.py`, `discover_contacts.py`, `verify_emails.py`, `compose_emails.py`, `send_emails.py`, and `noop_stage.py`) follows the same pattern at the top of `main()`:

1. Load `brief.yaml` via `lib.brief.load(<campaign_dir>/brief.yaml)`. On `BriefValidationError`, emit the exit-3 JSON contract (see §8) and exit 3.
2. Read the raw bytes of `brief.yaml` for hashing.
3. Compute `progress_dir = <campaign_dir>/progress/`. Create if missing.
4. If `check_brief_hash(progress_dir, brief_bytes)` is `False`: print the documented remediation message to stderr and exit 2.
5. Else: `write_brief_hash(progress_dir, brief_bytes)` (no-op if same; writes on first run).
6. Proceed with stage logic.

Documented remediation message for mismatch (printed to stderr, exit 2):
```
Brief changed since previous stage. Either revert brief.yaml or start a fresh
campaign in a new directory.

Expected hash: <expected>
Found hash:    <found>
Brief path:    <campaign_dir>/brief.yaml
```

### 5.4 Tests to add to `tests/lib/test_progress.py`

```python
# Test: brief-hash invariant — write_brief_hash(p, brief_bytes) then check_brief_hash(p, brief_bytes)
#       returns True; with mutated bytes returns False.
# Test: check_brief_hash returns True when the file is absent (first-run case).
# Test: write_brief_hash overwrites existing file silently (idempotency for re-runs).
# Test: write_brief_hash is atomic (kill between .tmp write and rename → old hash file
#       still readable; .tmp file ignored on next call).
```

## 6. `scripts/noop_stage.py` — the plumbing-verifier

### 6.1 CLI contract

```
python scripts/noop_stage.py \
  --campaign-dir campaigns/2026-05_noop \
  [--target-count N]      # default: pulled from brief.target.target_domain_count
  [--resume]
```

Exit codes follow the §3 contract (0/2/3).

### 6.2 Behavior (top-to-bottom of `main()`)

1. **Parse args.** `--campaign-dir` is required (positional path); `--target-count` and `--resume` are optional.
2. **Brief pre-flight.** Use the §5.3 pattern. On `BriefValidationError`, emit exit-3 JSON and exit. On hash mismatch, emit the remediation message and exit 2.
3. **Determine target count.** If `--target-count` was passed, use it; else use `brief.target.target_domain_count`.
4. **Instantiate observability.**
    - `campaign_obs = CampaignObserver(campaign_dir)`
    - `obs = StageObserver(campaign_obs, stage="noop", cadence_items=50, cadence_seconds=120)`
    - `obs.stage_start()`
5. **Open the progress store.** `progress = ProgressStore(campaign_dir / "progress" / "noop_stage.json")`; call `progress.load()`.
6. **Open the output CSV writer.** Output path: `campaign_dir / "noop.csv"`. Use `csv_schema.write_csv_row` so the header is emitted on first write.
7. **Loop `i` from 0 to `target_count - 1`:**
    - `key = f"item-{i:06d}"` (zero-padded for stable ordering).
    - If `progress.is_done(key)`: skip (this is the `--resume` path).
    - `time.sleep(0.05)` — synthetic per-item work.
    - Construct a one-column "noop row" — use a minimal Pydantic model defined inline in `noop_stage.py` (do NOT pollute `lib/csv_schema.py` with this; it gets deleted in section 06):

      ```python
      class NoopRow(BaseModel):
          model_config = ConfigDict(extra="forbid")
          idx: int
          key: str
      ```
    - `write_csv_row(noop_csv_path, NoopRow(idx=i, key=key))`.
    - `progress.mark(key, "ok", idx=i)`.
    - `obs.tick({"items": i + 1, "cost": 0.0})`.
8. **Finish.** `obs.finish(status="COMPLETED", summary={"items": target_count, "cost": 0.0})`.
9. **Exit 0.**

### 6.3 Error handling wrapper

Wrap the body of `main()` in a `try` / `except` / `finally`:

```python
def main():
    try:
        # ... steps 1-9 above ...
        sys.exit(0)
    except BriefValidationError as e:
        # Emit exit-3 JSON; see §8 below for the exact contract.
        ...
        sys.exit(3)
    except SystemExit:
        raise  # propagate cleanly
    except Exception as e:
        # If we got far enough to create the observer, mark FAILED.
        if 'obs' in locals():
            obs.finish(status="FAILED", summary={"error": str(e)})
        print(f"noop_stage failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
```

### 6.4 Why the no-op stage uses `target.target_domain_count` from the brief

Two reasons. First, it forces the implementer to wire brief-loading through the script (you can't get away with hardcoding `target_count=200`). Second, the acceptance test in §6.5 uses `target_domain_count=200` from a fixture brief, so the test exercises both the brief loader and the CLI override.

### 6.5 Tests — `tests/test_noop_stage.py`

```python
# tests/test_noop_stage.py
# Test: end-to-end run with target_count=200 → noop.csv has exactly 200 rows
#       (header + 200 data rows).
# Test: status.md after a clean run ends with "COMPLETED" (case-sensitive substring check).
# Test: activity.log has >= (200/50) milestone lines plus a stage_start line and a finish line.
# Test: kill at row ~100 and rerun with --resume → final noop.csv has 200 unique rows,
#       no duplicates. Killing is simulated by running the loop ~half-way then re-invoking;
#       you can implement this by monkeypatching time.sleep to raise KeyboardInterrupt on
#       call number 100, then re-running without the monkeypatch.
# Test: brief-hash invariant — first run writes progress/brief_hash.txt; mutating
#       brief.yaml between runs (without overriding) causes exit 2 with the documented
#       remediation message in stderr.
# Test: brief.yaml missing → exit 3 (FileNotFoundError path → BriefValidationError contract).
# Test: brief.yaml invalid (e.g., empty priority_roles) → exit 3 with structured JSON on stderr.
# Test: ProgressStore concurrency stress — 100 threads each calling progress.mark(unique_key)
#       against a single store → final progress.json has exactly 100 keys. (This duplicates
#       the test in tests/lib/test_progress.py but runs it through the no-op stage's
#       ProgressStore instantiation to confirm wiring.)
```

Use the `sample_brief` and `tmp_campaign_dir` fixtures from `tests/conftest.py` (added in section 02). The `sample_brief` fixture should be tunable via parameters so the brief-mutation test can produce a "different brief" without rewriting the whole YAML.

## 7. `scripts/status.py` — read-only inspector

### 7.1 CLI contract

```
python scripts/status.py --campaign-dir <dir> [--json]
```

- Default output: human-readable text (markdown-ish, suitable for printing).
- `--json`: structured JSON to stdout for Claude Code consumption.
- Exit code is **always 0** unless the campaign directory itself is missing or unreadable (then exit 2). A campaign in any inconsistent state is still reported, not errored out — the inspector's job is to surface state, not to gatekeep.

### 7.2 What it reports

Read-only inspection. Touches no file other than to read:

- `<dir>/brief.yaml` — loads via `lib.brief.load`. If load fails, report `brief_status="invalid"` and include the validation error message, but do NOT exit non-zero.
- `<dir>/progress/brief_hash.txt` — compares against the current brief bytes to detect drift.
- `<dir>/progress/<stage>.json` for each known stage — counts keys by status.
- `<dir>/observer_state.json` — per-stage costs and durations.
- `<dir>/domains.csv`, `<dir>/contacts.csv`, `<dir>/emails.csv`, `<dir>/outbox.csv`, `<dir>/sent.log` — count rows (excluding header). These files only exist for stages that have run or are partway through.

### 7.3 Stage state enum

For each stage in `["source", "discover", "verify", "compose", "send"]`:

- `NOT_STARTED`: `progress/<stage>.json` does not exist and the corresponding output file does not exist.
- `RUNNING`: `progress/<stage>.json` exists AND `status.md` shows this stage as the active one (heuristic: `observer_state.json` has the stage in `started_stages` but not in `completed_stages`). Note: if the orchestrator process died, this state is indistinguishable from RUNNING via filesystem alone — that's accepted in v1.
- `COMPLETED`: `observer_state.json` lists the stage in `completed_stages`.
- `FAILED`: `observer_state.json` lists the stage in a `failed_stages` collection (which `StageObserver.finish(status="FAILED")` must populate — confirm this matches the section-03 implementation).
- `INCONSISTENT`: brief-hash drifted, or progress file exists but expected input is missing, or any state where two heuristics disagree. Include a human-readable reason.

### 7.4 JSON output shape

```json
{
  "campaign_dir": "campaigns/2026-05_medium-retailers",
  "brief": {
    "status": "valid",
    "slug": "medium-retailers",
    "hash": "sha256:abc123...",
    "saved_hash": "sha256:abc123...",
    "hash_matches": true
  },
  "stages": {
    "source": {
      "status": "COMPLETED",
      "row_count": 1491,
      "cost_usd": 4.12,
      "duration_seconds": 312.5
    },
    "discover": {
      "status": "RUNNING",
      "row_count": 612,
      "cost_usd": 8.40,
      "duration_seconds": null
    },
    "verify": {"status": "NOT_STARTED"},
    "compose": {"status": "NOT_STARTED"},
    "send": {"status": "NOT_STARTED"}
  },
  "next_command": "python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_medium-retailers --resume",
  "total_cost_usd": 12.52
}
```

The `next_command` field is the single most important consumer-facing piece — Claude Code calls `status.py --json` and reads `next_command` to know what to invoke next. The rule for computing it:

1. If `brief.status != "valid"`: `next_command = null` (Claude must fix the brief).
2. If `brief.hash_matches == false`: `next_command = null` (Claude must reconcile).
3. Find the first stage in the canonical order that is not `COMPLETED`. The `next_command` is the canonical invocation of that stage with `--resume` if a partial progress file exists, else without.
4. If all stages are `COMPLETED`: `next_command = null`.

### 7.5 Human-readable output

A short markdown-like report. Example:
```
Campaign: medium-retailers (campaigns/2026-05_medium-retailers)
Brief:    VALID (hash matches)
Total spend: $12.52

  source     COMPLETED   1491 rows   $4.12   5m 12s
  discover   RUNNING     612 rows    $8.40   in progress
  verify     NOT_STARTED
  compose    NOT_STARTED
  send       NOT_STARTED

Next: python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_medium-retailers --resume
```

### 7.6 Tests — `tests/test_status.py`

```python
# tests/test_status.py
# Test: status.py --json on an empty campaign dir (only brief.yaml present) → JSON
#       with all stages NOT_STARTED, next_command for source_domains.py.
# Test: after the noop stage completes, status.py reports noop=COMPLETED with row count
#       matching noop.csv. (The noop stage isn't in the canonical stage list, so this
#       test uses a fixture campaign that has run a fake "source" stage instead.)
# Test: brief-hash mismatch → status.py reports brief.hash_matches=false, all stages
#       INCONSISTENT, next_command=null.
# Test: brief.yaml invalid → status.py reports brief.status="invalid", does NOT exit
#       non-zero (the inspector should be safe to call on broken campaigns).
# Test: per-stage row count, cost, duration are read from observer_state.json correctly.
# Test: --json mode is parseable JSON; default mode is human-readable text (smoke test only).
# Test: missing campaign directory → exit 2 with a clean error message.
```

## 8. `scripts/run_pipeline.py` — sequential runner

### 8.1 CLI contract

```
python scripts/run_pipeline.py --campaign-dir <dir> [--resume]
```

This is **optional** infrastructure. Claude Code may invoke stages individually via the playbooks, or it may use this wrapper. The wrapper exists so a user who wants "just run it all" can get that without writing a shell loop.

### 8.2 Behavior

1. Validate `--campaign-dir` exists and contains `brief.yaml`.
2. Run, in order:
   - `python scripts/source_domains.py --campaign-dir <dir> [--resume]`
   - `python scripts/discover_contacts.py --campaign-dir <dir> [--resume]`
   - `python scripts/verify_emails.py --campaign-dir <dir> [--resume]`
   - `python scripts/compose_emails.py --campaign-dir <dir> [--resume]`
3. **STOP before `send_emails.py`.** The test-batch decision is the one place the user MUST be in the loop.
4. After the final pre-send stage completes, print to stdout:

   ```
   Pre-send stages complete. Inspect outbox.csv, then:
     python scripts/send_emails.py --campaign-dir <dir>
   ```
5. Exit 0.

If any stage exits non-zero, `run_pipeline.py` propagates that exit code immediately (fail-fast). No retries — the user (or Claude Code) decides whether to re-invoke with `--resume`.

### 8.3 Implementation note

In M0, the four script files (`source_domains.py`, etc.) **do not exist yet** — they're built in sections 06–10. `run_pipeline.py` must still be implementable now because it just invokes them via `subprocess.run`. The integration test for it can mock the subprocess calls. The end-to-end test (full pipeline through real stages) happens at M3 or M4.

Subprocess invocation should use `sys.executable` (not bare `python`) so the active venv's Python is used:

```python
import subprocess, sys
result = subprocess.run(
    [sys.executable, "scripts/source_domains.py", "--campaign-dir", str(campaign_dir)]
    + (["--resume"] if resume else []),
    check=False,
)
if result.returncode != 0:
    sys.exit(result.returncode)
```

### 8.4 Tests — `tests/test_run_pipeline.py`

```python
# tests/test_run_pipeline.py
# Test: run_pipeline.py runs all four pre-send stages in order; stops before send_emails.
#       Mock subprocess.run to return success; assert call_args_list is exactly the
#       four expected stages in order, none of them send_emails.
# Test: failure in any stage → run_pipeline exits with that stage's exit code
#       (e.g., source_domains exits 3 → run_pipeline exits 3 immediately, does NOT
#       call discover_contacts).
# Test: --resume flag is propagated to each invoked stage.
# Test: missing --campaign-dir or non-existent path → exit 2 before invoking any stage.
# Test: brief.yaml missing in campaign-dir → exit 3 (the wrapper does its own brief
#       pre-check OR delegates to the first stage; either is acceptable as long as
#       the exit code is right).
```

## 9. Exit-3 JSON error contract (used by every script wrapper)

When `lib.brief.load(path)` raises `BriefValidationError`, every script wrapper (noop_stage.py, source_domains.py, discover_contacts.py, etc.) must:

1. Print a single line of JSON to **stderr**:
   ```json
   {"error":"BriefValidationError","field":"<field>","message":"<message>","brief_path":"<path>"}
   ```
   The `field` and `message` come from structured attributes on `BriefValidationError` (added in section 02; e.g., `e.field`, `e.message`, `e.brief_path`).
2. Print nothing to stdout.
3. Exit with code 3.

Helper function (put it in `lib/brief.py` to keep all brief logic together, or in a new tiny `lib/errors.py` — implementer's call, but consistent across scripts):

```python
def emit_brief_error_and_exit(e: BriefValidationError) -> NoReturn:
    """Print the exit-3 JSON contract to stderr and exit 3."""
    payload = {
        "error": "BriefValidationError",
        "field": e.field,
        "message": e.message,
        "brief_path": str(e.brief_path),
    }
    print(json.dumps(payload), file=sys.stderr)
    sys.exit(3)
```

### 9.1 Tests — `tests/test_error_contract.py`

```python
# tests/test_error_contract.py
# Test: noop_stage.py with a brief.yaml missing target.segment → stderr is a single
#       parseable JSON line with error="BriefValidationError", field="target.segment",
#       exit code is 3.
# Test: stdout is empty when exit 3 (no leakage of stack traces to stdout).
# Test: every script's wrapper produces the same JSON shape (parameterized test
#       across [noop_stage]; expanded as more scripts come online — this test file
#       is updated incrementally in later sections).
# Test: exit code is 3 on brief errors; exit code is 2 on a brief-hash mismatch
#       (NOT 3 — the hash check is a separate failure mode).
```

## 10. M0 acceptance checklist

Before merging this section, confirm:

- `uv run pytest tests/lib/ tests/test_noop_stage.py tests/test_status.py tests/test_run_pipeline.py tests/test_error_contract.py` is green.
- `uv run python scripts/noop_stage.py --campaign-dir campaigns/2026-05_noop --target-count 200` produces:
   - `campaigns/2026-05_noop/status.md` showing live progress during the run, ending with "COMPLETED" after.
   - `campaigns/2026-05_noop/activity.log` with at least 5 timestamped milestone lines (200 items / 50 cadence + start + finish).
   - `campaigns/2026-05_noop/noop.csv` with exactly 200 data rows + header.
   - `campaigns/2026-05_noop/progress/noop_stage.json` with 200 keys.
   - `campaigns/2026-05_noop/progress/brief_hash.txt` with a hex sha256 of `brief.yaml`.
- Killing the no-op mid-run (Ctrl-C around item 100) and re-invoking with `--resume` produces a 200-row `noop.csv` indistinguishable from a non-killed run.
- Mutating `brief.yaml` between two no-op invocations (e.g., changing one character in `target.segment`) causes the second invocation to exit 2 with the documented remediation message.
- `uv run python scripts/status.py --campaign-dir campaigns/2026-05_noop --json` prints valid JSON.
- All Pydantic models defined in this section (just `NoopRow`) pass the OpenAI strict-mode schema test from section 02 — i.e., `extra="forbid"` and no plain `Optional[X]` without default.

## 11. Out of scope (do NOT add to this section)

- Anything that calls OpenAI or Gmail — those are sections 06–11.
- Web search, DNS lookups against the real internet, SMTP probes — tests must use fixtures, never live network.
- A campaign-report generator (deferred to v2 per spec §1.3).
- A wholesale `Pipeline` orchestration class — `run_pipeline.py` is a 30-line subprocess loop; resist the temptation to build abstractions.
- Stage 0 (the brief interview) — that's `CLAUDE.md` content authored in section 01, not Python.

## 12. Implementation tips

- Keep `scripts/noop_stage.py` under 150 lines. If it grows past that, you're putting too much in it — the cross-cutting libraries should be doing the work.
- The `NoopRow` Pydantic model is defined inline in `noop_stage.py`, NOT in `lib/csv_schema.py`. The whole point is that this stage gets deleted in section 06 with no traces left in the library layer.
- `scripts/status.py` is read-only — verify this by checking that NO `open(..., "w")` or `open(..., "a")` calls exist in it. Add a grep-based unit test if paranoid.
- The pipeline runner's subprocess invocations should NOT capture stdout/stderr — they should inherit the parent's terminal so the user sees live progress from each stage. Use `subprocess.run(...)` without `capture_output=True`.
- When testing exit codes, prefer `subprocess.run([sys.executable, "scripts/noop_stage.py", ...]).returncode` over importing `main()` and asserting on `SystemExit`. The subprocess form exercises the same code path the user hits.