# Outreach Bot — TDD Plan

Companion to `claude-plan.md`. Mirrors that document's structure. For each section, lists tests to write BEFORE writing the corresponding implementation.

**Conventions** (from `claude-research.md` "Testing Approach"):
- Framework: `pytest` with fixtures.
- Test location: `tests/` mirroring `scripts/` layout (`scripts/lib/brief.py` → `tests/lib/test_brief.py`).
- Shared fixtures: `tests/conftest.py` (sample brief, fake LLMClient, fake GmailClient, tmp campaign dirs, mocked dns/smtp).
- Mock libraries: `pytest-mock` (`mocker`), `responses` or `httpx-mock` for HTTP, `aiosmtpd` for SMTP server-side tests.
- Per the user's per-project override of the global "delete tests after ship" rule: tests stay in the repo permanently.

---

## §2 Cross-cutting libraries

### §2.1 `lib/brief.py`

```python
# tests/lib/test_brief.py
# Test: load() with a complete valid brief.yaml returns a populated Brief.
# Test: missing required field target.segment → BriefValidationError naming "target.segment".
# Test: empty priority_roles list → validation error.
# Test: send_rate_per_day > 2000 → validation error (safety cap).
# Test: slug = "Foo Bar" (not kebab-case) → validation error.
# Test: unknown extra top-level field in YAML → validation error (extra="forbid").
# Test: template path that doesn't exist → validation error naming the path.
# Test: from_gmail that doesn't look like an email → validation error.
# Test: BriefValidationError has structured attributes (field, message, brief_path) so the
#       main wrapper can emit the exit-3 JSON contract from §8.5.
# Test: load() of a non-existent path → FileNotFoundError with a clean message.
# Test: contacts_per_company > 12 → validation error (max cap per claude-spec.md §4).
```

### §2.2 `lib/progress.py`

```python
# tests/lib/test_progress.py
# Test: new ProgressStore on non-existent path → empty after load(); writes file on first mark().
# Test: mark("k1","ok") then is_done("k1") is true; is_done("k2") is false.
# Test: reload from disk preserves state.
# Test: terminal vs retriable status — is_retriable("worker_exc") true, is_retriable("ok") false.
# Test: lost-update under concurrency (review issue #1):
#       100 threads each call mark(f"k{i}", "ok") → final progress.json has exactly 100 keys.
# Test: concurrent mark() on the SAME key from two threads → final state is one of the two writes,
#       never half-written or absent.
# Test: crash simulation — write .tmp without rename → on next load(), .tmp ignored, old file used.
# Test: keys() returns all processed keys in insertion order.
# Test: extras passed to mark() are preserved in the JSON value.
# Test: brief-hash invariant — write_brief_hash(p, brief_bytes) then check_brief_hash(p, brief_bytes)
#       returns True; with mutated bytes returns False.
```

### §2.3 `lib/observability.py`

```python
# tests/lib/test_observability.py
# CampaignObserver tests:
# Test: instantiation in empty campaign dir creates observer_state.json + empty status.md.
# Test: stage_complete("source", summary) updates status.md preserving prior stages.
# Test: total_cost() sums per-stage costs from observer_state.json.

# StageObserver tests:
# Test: stage_start() writes "stage X starting" event + sets status.md section to RUNNING.
# Test: cadence by items — 50 ticks at item-cadence=50 → exactly one milestone line emitted.
# Test: cadence by time — 1 tick + 121s elapsed (mocked monotonic clock) → milestone emitted.
# Test: cadence reset — after a milestone fires, next milestone needs another full window.
# Test: status.md content matches the template (header banner, counters, last event).
# Test: activity.log lines are ISO-timestamped and ordered.
# Test: event(level="warn") writes WARN to activity.log; does NOT change stage status.
# Test: finish(status="COMPLETED", summary) transitions stage to COMPLETED + updates CampaignObserver.
# Test: finish(status="FAILED", summary) sets stage FAILED in status.md, prints traceback location.
# Test: a stage that calls finish(FAILED) and then exits is the only path to FAILED — bare event()
#       calls never set FAILED (semantics from review issue #8).
# Test: cross-stage handoff — after Stage 1 finish, Stage 2 instantiation preserves Stage 1 banner.
# Test: total cost shown in status.md is sum of stage costs, not just current stage.
```

### §2.4 `lib/dedup.py`

```python
# tests/lib/test_dedup.py
# Test: is_suppressed returns True for an email in suppression.csv; False otherwise.
# Test: is_known returns True for an email in master_contacts.csv when scope=all_campaigns.
# Test: is_known returns False for the same email when scope=this_campaign.
# Test: append_contact appends a single row; doesn't rewrite the whole file (compare file inode
#       before/after via os.stat).
# Test: append_suppressed appends; idempotent if same email appears twice (deduped on read).
# Test: concurrent appends from two processes (multiprocessing) — both rows land in the final file
#       (file lock works). Verify by checking row count after both processes complete.
# Test: file lock blocks a second writer until the first releases — second writer's append waits.
# Test: pidfile model — calling Deduper.acquire_send_lock() twice in the same process is OK;
#       calling from a second process while the first holds the lock raises with a clean message.
# Test: reload() picks up rows added by another process (e.g., poll_bounces.py running concurrently).
```

### §2.5 `lib/dns_check.py`

```python
# tests/lib/test_dns_check.py
# Test: mx_records — mock dns.resolver.resolve to return canned MX → returns sorted hostnames.
# Test: mx_records — mock NoAnswer → returns [].
# Test: mx_records — mock NXDOMAIN → returns [].
# Test: mx_records — Timeout → raises (caller handles).
# Test: is_null_mx — mock priority=0, target='.' → True.
# Test: has_mail — MX present → True.
# Test: has_mail — no MX but A record present → True.
# Test: has_mail — no MX, no A → False.
# Test: has_mail — null MX → False.
# Test: LRU cache hits — second call for same domain doesn't re-resolve (count resolver calls).
```

### §2.6 `lib/llm.py`

```python
# tests/lib/test_llm.py
# Test: parse() with mocked OpenAI client returning structured output → ParseResult.parsed
#       is the expected Pydantic instance; cost non-zero.
# Test: parse() on 429 (first call), success (second) → retries; ParseResult.parsed set, cost reflects
#       both attempts' input/output tokens.
# Test: parse() with refusal in resp.output[0].refusal → refused=True, parsed=None,
#       refusal_text populated.
# Test: parse() with empty output_parsed and no refusal → refused=False, parsed=None.
# Test: parse() with parsed result whose confidence < threshold → low_confidence=True, parsed set.
# Test: cascade() — tier1 returns parsed=None refused=False → tier2 called; cost accumulates.
# Test: cascade() — tier1 returns refused=True → tier2 NOT called; ParseResult propagates refusal.
# Test: cascade() — tier1 low_confidence + tier2 high_confidence → tier2 result preferred.
# Test: model probe at startup — first fallback unreachable, second reachable → uses second.
# Test: all fallbacks unreachable → RuntimeError at __init__.
# Test: cost calculation — token counts × per-model rates + web_search_calls × $0.025 matches
#       known-good values for canned response.
# Test: temperature=0 passed through to the API call.
```

### §2.7 `lib/gmail.py`

```python
# tests/lib/test_gmail.py
# authorize() tests:
# Test: token.json present, scopes match → returns creds without browser prompt.
# Test: token.json present, scopes superset of existing → forces re-flow; documented message printed.
# Test: token.json present but expired with refresh_token → refresh() called, no browser.
# Test: token.json absent → InstalledAppFlow.run_local_server() called (mocked).

# send() tests:
# Test: mocked Gmail HTTP API; send() builds correct MIME structure.
#       Verify: raw field is base64.urlsafe_b64encode(msg.as_bytes()).decode() (not plain b64encode).
#       Decode raw → headers include To, From, Subject, Reply-To; body matches body_html.
# Test: 429 from API → raises QuotaExceeded.
# Test: "Daily user sending limit exceeded" 4xx → raises QuotaExceeded.
# Test: 5xx → raises (caller will retry).
# Test: 200 with a different from-address echoed back than requested → warning logged.

# list_bounces() tests:
# Test: mocked API returns matching messages → returns BounceRecord list with parsed Final-Recipient.
# Test: empty inbox → returns [].
# Test: malformed bounce body (no Final-Recipient header) → record skipped, warning logged.
# Test: since_message_id filter — only messages newer than the given id returned.
```

### §2.8 `lib/csv_schema.py`

```python
# tests/lib/test_csv_schema.py
# For each model (DomainRow, ContactRow, EmailRow, OutboxRow, SentLogRow, SuppressionRow,
# MasterContactRow):
#   Test: write_csv_row then read_csv round-trips identically.
#   Test: appending to existing CSV → header not duplicated.
#   Test: invalid row (missing required field) → ValidationError at construct time.
#   Test: extra="forbid" — unknown field rejected at construct time.
#   Test: Optional[X] field with default=None — missing in YAML/CSV → None, not error.
# Test: OpenAI strict-mode compliance — for every model, generate the JSON schema OpenAI
#       sends (via openai.lib._tools schema-from-pydantic helper or equivalent) and assert
#       it has additionalProperties:false and every property in required. This gates M0.
```

### §2.9 `lib/rate_limit.py`

```python
# tests/lib/test_rate_limit.py
# Test: RateLimiter(2.0) — 4 calls take ~2.0s ±0.1s (uses mocked monotonic).
# Test: HourlyLimiter(per_hour=3, burst=1) — first 3 immediate; 4th blocks ~1200s (mocked).
# Test: Sustained-rate (review issue #12) — HourlyLimiter(30/hr, burst=5):
#       60 acquires take ≥ ~110 minutes with mocked clock.
# Test: Mixed limiter — RateLimiter(0.5) + HourlyLimiter(50, burst=10) — first 10 unblocked,
#       then converges to ~50/hr.
# Test: RateLimiter wakes up correctly after a long pause (clock skip simulation).
```

### §2.10 `lib/verifiers/base.py`

```python
# tests/lib/verifiers/test_base.py
# Test: Verifier protocol — a dummy implementation satisfies it.
# Test: VerificationResult schema — status enum is enforced.
# Test: VerifierUnavailable exception carries a structured remediation message.
```

---

## §3 Milestone M0

In addition to all `lib/*` tests above (which gate M0):

```python
# tests/test_noop_stage.py
# Test: end-to-end run with target_count=200 → noop.csv has exactly 200 rows.
# Test: status.md ends with "COMPLETED".
# Test: activity.log has ≥ (200/cadence_items) milestone lines + start + finish events.
# Test: kill at row ~100 and rerun with --resume → final noop.csv has 200 unique rows.
# Test: brief-hash invariant — first run writes progress/brief_hash.txt; modifying brief.yaml
#       between runs (without --resume override) causes exit 2 with the documented message.
# Test: ProgressStore concurrency — synthetic 100-thread stress under the no-op stage scenario.
```

---

## §4 Milestone M1 — `source_domains.py`

```python
# tests/test_source_domains.py
# Happy path:
# Test: brief with target_domain_count=20; mocked LLM returns 5 retailers per query × 4 queries
#       → 20 unique domains in domains.csv.

# Filter / dedup:
# Test: rows with is_excluded=true dropped.
# Test: within-run dedup — same domain returned 3 times across queries → one row in output.
# Test: cross-campaign dedup — domain in master_contacts.csv, scope=all_campaigns → excluded.
# Test: same scenario, scope=this_campaign → included.

# DNS:
# Test: mocked has_mail=False → row dropped.
# Test: mocked is_null_mx=True → row dropped.

# LLM behavior:
# Test: LLM 429 first call, success second → row produced.
# Test: LLM refusal → mark progress search_fail, continue with next query, no row.
# Test: LLM empty result (parsed=None, refused=False) → cascade to tier2; if still none, mark search_fail.

# Resume:
# Test: kill after 10 rows + resume → final output identical to non-killed run; no duplicates.

# Observability:
# Test: 50-row milestone emitted to stdout and activity.log.
# Test: status.md counters match real counts after a run.

# Normalization:
# Test: input "Https://Www.RetailerX.com/path" → domains.csv has "retailerx.com".

# Termination:
# Test: target reached early — output capped at target_domain_count.
# Test: queries exhausted with target unmet — exit 0, status notes "queries exhausted".

# Pre-flight:
# Test: missing brief.yaml → exit 3 with structured JSON on stderr.
# Test: brief.yaml validation error → exit 3 with field-named error JSON.

# Concurrency model:
# Test: source_domains.py is single-threaded (no ThreadPoolExecutor in M1) — confirm only main
#       thread writes the CSV.
```

---

## §5 Milestone M2 — `discover_contacts.py` + verification

### `discover_contacts.py`

```python
# tests/test_discover_contacts.py
# Happy path:
# Test: 3 domains, mocked LLM returns 3 people each → contacts.csv has 9 rows.

# LLM behavior:
# Test: refusal at tier1 → tier2 attempted; if also refusal → mark discovery_fail.
# Test: empty result → cascade; if still none → mark no_people.
# Test: corrected_domain returned by LLM → ContactRow.domain uses corrected value.

# DNS:
# Test: pre-check DNS fail on a domain → skip, mark dns_fail.

# Concurrency model (review issue #11):
# Test: worker exception in one thread → marked worker_exc, other workers continue.
# Test: worker_exc is retriable on --resume.
# Test: queue-based write — main thread sole writer; concurrent test with 50 mocked domains
#       → no row dup, no row loss.

# Exception taxonomy:
# Test: openai.RateLimitError (429) → retried, eventually succeeds.
# Test: openai.AuthenticationError (401) → halts the stage, finish(FAILED), exit 2.
# Test: ConnectionTimeout in a worker → marked worker_exc.

# Failure budget:
# Test: 25 of 100 domains fail with worker_exc (>20%) → halt with diagnostic message.
# Test: 3 of 10 fail (small sample, even though 30% rate) → continue (n_processed < 20 threshold).

# Resume:
# Test: kill at row 50/200, resume → final output identical to non-killed run.

# Observability:
# Test: milestone every 20 companies.

# Pre-flight:
# Test: contacts.csv input precondition — N/A for M1 output; M2 reads domains.csv:
# Test: missing domains.csv → exit 2 with "No domains. Run source_domains.py first."
# Test: brief-hash mismatch → exit 2 with the documented message.
```

### `verifiers/smtp_probe.py`

```python
# tests/lib/verifiers/test_smtp_probe.py
# Use aiosmtpd or socket mock as the test SMTP server.

# Happy path:
# Test: HELO ok, candidate RCPT → 250, random RCPT → 550 → status=accepted, confidence=verified-smtp.

# Catch-all:
# Test: both 250 → status=catchall.

# Rejection:
# Test: candidate RCPT → 550 → status=rejected.

# Connection failure:
# Test: connect refused / 421 → status=unknown.

# Greylisting (review issue from interview Q2.1):
# Test: 4xx + greylist_retry=true → 90s mock-clock wait, retry; second 250 → accepted.
# Test: 4xx + greylist_retry=true, second also 4xx → status=unknown.
# Test: 4xx + greylist_retry=false → status=unknown immediately.

# MX tarpit hard-skip (review issue from interview Q2.2):
# Test: MX hostname matches *.mail.protection.outlook.com → status=catchall returned, socket
#       never opened (verify mock socket not called).
# Test: MX hostname matches *.olc.protection.outlook.com → same.
# Test: MX hostname matches *.pphosted.com → same.
# Test: MX hostname matches *.ppe-hosted.com → same.
# Test: MX hostname matches *.mimecast.com → same.
# Test: MX hostname matches *.mail.example.com (non-tarpit) → probe proceeds.

# DNS:
# Test: no MX → status=rejected (cannot receive mail).
# Test: null MX → status=rejected.

# Pre-flight:
# Test: assert_available mocked socket success → no exception.
# Test: assert_available mocked socket failure → raises VerifierUnavailable with the documented
#       remediation message ("Port 25 blocked. Connect to Dartmouth VPN, or set verifier.chain to
#       ['web_citation']...").

# Rate limiting:
# Test: 10 calls at rate_per_sec=2.0 → ~5s total (mocked clock).
# Test: HourlyLimiter integration — 60 calls at per_hour=30 → ~110 min total.
```

### `verifiers/web_citation.py`

```python
# tests/lib/verifiers/test_web_citation.py
# Test: citation_url is None → status=unknown.
# Test: citation_url is an aggregator host (e.g., "https://rocketreach.co/jane") → status=unknown.
# Test: citation_url is a subdomain of an aggregator → status=unknown (matches AGGREGATOR_HOSTS).
# Test: malformed URL → status=unknown.

# HEAD-200 + body-match (review issue #9):
# Test: HEAD returns 404 → status=unknown, notes='citation URL not reachable'.
# Test: HEAD returns 200, GET body contains BOTH local-part AND domain → status=accepted,
#       confidence=verified-web.
# Test: HEAD 200, body contains domain but NOT local-part → status=unknown, notes documents this.
# Test: HEAD 200, body contains neither → status=unknown.
# Test: HEAD/GET timeout → status=unknown (don't crash).
# Test: gzipped response → decompressed before search (real-world common case).
# Test: HEAD-200 but server-side redirect to an aggregator → status=unknown (final host check).
```

### `verifiers/api_provider.py`

```python
# tests/lib/verifiers/test_api_provider.py
# Test: mocked provider returns "valid" → status=accepted, confidence=verified-api.
# Test: mocked provider returns "invalid" → status=rejected.
# Test: mocked provider returns "unknown"/"catchall" → status mapped accordingly.
# Test: 401 from provider on assert_available → raises VerifierUnavailable.
# Test: enabled=false in verifiers.yaml → verifier not instantiated by verify_emails.py.
# Test: provider key missing from secrets.env, enabled=true → assert_available fails with
#       "ZEROBOUNCE_API_KEY not set" message.
```

### `verify_emails.py`

```python
# tests/test_verify_emails.py
# Pipeline integration:
# Test: 3 contacts, chain=[smtp_probe, web_citation].
#       Contact 1: smtp accepted → EmailRow with verified-smtp.
#       Contact 2: smtp catchall, citation primary-source + body match → EmailRow with verified-web.
#       Contact 3: smtp rejected → not written.

# Pre-flight:
# Test: smtp_probe.assert_available raises → exit 2 with documented message; emails.csv not touched.
# Test: missing contacts.csv → exit 2 with "Run discover_contacts.py first."
# Test: brief-hash mismatch → exit 2.
# Test: estimated-time > 8h → warning printed, run continues.

# Per-company cap:
# Test: contacts_per_company=3, 5 contacts at same domain; first 3 verified → stop probing 4 & 5.

# Pattern-only drop (interview Q2.3):
# Test: contact with email_if_known=None → skipped entirely (not pattern-generated).

# Resume:
# Test: kill at candidate 100/300, resume → state in progress/verify_emails.json honored.

# Rate limiting:
# Test: integration with HourlyLimiter — sustained run respects per_hour_cap.
```

---

## §6 Milestone M3 — composition + send

### `compose_emails.py`

```python
# tests/test_compose_emails.py
# Happy path:
# Test: 3 EmailRows, template with all slots → 3 OutboxRows with correct substitutions.

# First-name extraction — naive path:
# Test: "Dr. Robert Smith" → first_name "Robert".
# Test: "Jane Doe" → "Jane".
# Test: "Andy" (single token) → "Andy".

# First-name extraction — ambiguity rules (review issue #6):
# Test: "Marie-Claire Dupont" + personalize=true → LLM called (mocked) → returns "Marie-Claire".
# Test: "Mary Jane Smith" + personalize=true → LLM called → returns LLM result (e.g., "Mary Jane").
# Test: "Robert J. Smith" + personalize=true → ambiguity rule "middle initial" → LLM NOT called,
#       first_name="Robert" via naive split.
# Test: "李伟" + personalize=true → LLM called.
# Test: "Robert Smith Jr." + personalize=true → LLM called (Jr. trigger).
# Test: personalize=false → naive split always used, LLM never called.

# Persistent cache (review issue #6):
# Test: same `name` value processed twice → LLM called exactly once; second call reads cache.
# Test: kill + resume → cache loaded from disk; no re-call for already-cached names.
# Test: temperature=0 passed to llm.parse for first-name canonicalization.

# Lints:
# Test: subject "OFFER INSIDE!!!" → activity.log warning; row still written.
# Test: body containing "bit.ly/foo" → warning; row still written.
# Test: body with 0 newlines → warning; row still written.
# Test: body > 500 words → warning; row still written.

# Template:
# Test: missing template file → exit with clean error mentioning path.
# Test: slot {{nonexistent}} in template but not in row → exit with error naming the slot.
# Test: extra unused field in row (e.g., notes) → no error, just ignored.

# Pre-flight:
# Test: missing emails.csv → exit 2 with "Run verify_emails.py first."
# Test: brief-hash mismatch → exit 2.

# Resume:
# Test: kill at row 100/200 + resume → final outbox.csv identical to non-killed.
```

### `send_emails.py`

```python
# tests/test_send_emails.py
# Phase A:
# Test: 12 OutboxRows, send_test_count=10 → exactly 10 sent in Phase A, exit 0, banner printed.
# Test: After Phase A, progress.json has phase_a_complete=true sentinel.

# Phase A error semantics (review issue #4):
# Test: 12 OutboxRows; mock 3 of first 10 to error 3 times each → those marked terminal_error,
#       Phase A advances to rows 11–13 to fill the test batch. Final n_sent=10 (real sends).
# Test: 12 OutboxRows; mock 11 to error permanently → Phase A halts with diagnostic, exit 2.

# Phase A → Phase B transition:
# Test: 12 rows, run Phase A (10 sent), re-run without --confirm-test → refuse, exit 1.
# Test: 12 rows, run Phase A, re-run WITH --confirm-test → 2 more sent.

# Suppression hard-gate:
# Test: 1 row's to_email in suppression.csv → marked skipped_suppressed, not sent, counts toward n_sent.

# Pessimistic counter (review issue #3):
# Test: counter incremented BEFORE Gmail call; on hard failure, decremented.
# Test: simulate process kill between Gmail-success and progress-mark → counter is 1-higher than
#       sent rows; next run sees correct cap, doesn't over-send.
# Test: counter date-keyed by datetime.now().date() (system local tz).
# Test: stale dates (> 14 days old) pruned on read.
# Test: hitting send_rate_per_day mid-run → exit 0, "rolled over" message; next-day invocation
#       resumes cleanly.

# Quota exceeded:
# Test: Gmail raises QuotaExceeded → 3 retries with exp backoff; if still failing, decrement
#       counter, mark error, continue.

# Throttle jitter:
# Test: 10 rows with throttle=1.0 → total time between 5s and 15s (uniform 0.5-1.5x range).

# master_contacts.csv:
# Test: every successful send appends to master_contacts.csv via dedup.append_contact (file lock).

# Concurrency (review issue #2):
# Test: starting send_emails.py while another instance is already running → exits with
#       "Another send_emails.py is running (data/.send.pid)" message.

# Pre-flight:
# Test: missing outbox.csv → exit 2 with "Run compose_emails.py first."
# Test: brief-hash mismatch → exit 2.

# Replay safety:
# Test: re-running without --resume on a partially-sent campaign refuses with error.

# Send-as warning:
# Test: Gmail returns response with different From than requested → warning to activity.log.
```

---

## §7 Milestone M4 — bounce tracking

```python
# tests/test_poll_bounces.py
# Test: mocked list_bounces returns 3 BounceRecords → 3 appended to suppression.csv.
# Test: 1 of the 3 already in suppression → only 2 new rows (idempotent).
# Test: empty bounce list → no changes; state file updated to latest message ID.
# Test: missing poll_bounces_state.json → starts from scratch.
# Test: malformed bounce body (no Final-Recipient) → record skipped, warning logged.
# Test: concurrent invocation — second instance blocks on .poll.pid lock with clean message.

# Re-auth flow (review issue #7):
# Test: existing token.json has gmail.send only → poll_bounces invocation forces re-flow with
#       gmail.send + gmail.readonly; new token.json written; subsequent runs don't re-prompt.
# Test: token.json with both scopes already → no re-prompt.

# End-to-end smoke test (manual, NOT in pytest):
# Documented in tests/manual/smoke_test_m4.md; produces a real test campaign, sends to known-bad
# addresses, runs poll_bounces, verifies suppression updates.
```

---

## §8 Inter-stage orchestration (§8.5 in plan)

```python
# tests/test_status.py
# Test: status.py --json on an empty campaign dir → JSON with all stages NOT_STARTED.
# Test: after M1 completes, status.py reports source=COMPLETED, discover=NOT_STARTED.
# Test: brief-hash mismatch → status.py reports INCONSISTENT for the relevant stages.
# Test: status.py reports per-stage row count, cost, duration after each stage completes.

# tests/test_run_pipeline.py
# Test: run_pipeline.py runs all four pre-send stages in order; stops before send_emails.
# Test: failure in any stage → run_pipeline exits with that stage's exit code.
# Test: --resume flag is propagated to each invoked stage.

# tests/test_error_contract.py
# Test: every script wrapper catches BriefValidationError and emits the documented JSON on stderr.
# Test: exit code is 3 on brief errors; 2 on other failures.
```

---

## Test ordering — what to write first

Per TDD: write tests BEFORE the implementation in each section. Within a milestone, recommended order:

1. **M0**: write all `tests/lib/*` first → implement libs → then noop_stage tests → noop_stage.
2. **M1**: write `tests/test_source_domains.py` (mostly with mocked LLM) → implement.
3. **M2**: write verifier unit tests (mocked SMTP, mocked HTTP) first → implement verifiers → then `tests/test_discover_contacts.py` / `tests/test_verify_emails.py` → implement scripts.
4. **M3**: write `tests/test_compose_emails.py` → implement → `tests/test_send_emails.py` (mocked Gmail) → implement.
5. **M4**: write `tests/test_poll_bounces.py` → implement → manual smoke test.

CI hookup is out of v1 scope per `claude-spec.md`. Local `pytest` is the bar.
