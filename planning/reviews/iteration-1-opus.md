# Opus Review — iteration 1

**Model:** claude-opus-4-7 (via deep-plan:opus-plan-reviewer subagent)
**Generated:** 2026-05-21
**Plan reviewed:** `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/claude-plan.md`

---

## Issue: Concurrency hazard in ProgressStore and shared CSV writes

**Severity:** must-fix
**Where:** §2.2 (`lib/progress.py`), §2.8 (`lib/csv_schema.py`), §5.2 (`scripts/discover_contacts.py` with `ThreadPoolExecutor`)

**Problem:** The plan asserts atomicity via `.tmp`+rename, but that's wrong under multi-threaded writes from the same process. With N `ThreadPoolExecutor` workers in M2 all calling `progress.mark(key, ...)` on the same `ProgressStore`:

1. Two threads call `mark()` near-simultaneously. Thread A reads in-memory dict, edits, writes to `path.tmp`, renames. Thread B does the same. Last writer wins — thread A's update is silently lost. `os.replace` is atomic with respect to the filesystem but does NOT serialize the read-modify-write sequence in Python.
2. `write_csv_row` (§2.8) is described as "Append row… Atomic via `.tmp`+rename." But appending to an existing CSV via tmp+rename means: read whole CSV, append in memory, write tmp, rename. Under concurrent appends from workers, you get the same lost-update race AND O(N²) work.
3. The Phase 2 prior art used a single producer-consumer model — only the main thread wrote progress; workers returned results. The plan doesn't preserve that. The Phase 2 prior art is the reference, and the plan loses its concurrency model.

**Recommendation:** Add an explicit `threading.Lock` inside `ProgressStore`, held across read-modify-write. For `write_csv_row`, either (a) use a global `csv_writer_lock` per file path and plain `open(path, "a")` append (the OS guarantees atomicity for small writes under O_APPEND on POSIX, but only for `<PIPE_BUF` bytes, ~4KB, so explicit locking is safer), or (b) reinstate the prior-art pattern: workers return results via a Queue, the main thread is the sole writer. Document the locking model in §2 and test it: §3.3's `tests/lib/test_progress.py` "concurrent writes from two threads → no corruption" must include the lost-update case (write a counter from 100 threads and assert the final count equals 100, not just "no corruption").

---

## Issue: Cross-stage cross-campaign dedup race / write-back gap

**Severity:** must-fix
**Where:** §2.4 (`lib/dedup.py`), §6.2 (`send_emails.py` step 6 "Append to `data/master_contacts.csv`")

**Problem:** `Deduper.commit()` is described as "Atomically flush both files." But:

1. M1 reads `master_contacts.csv` for dedup of domains, but the plan only describes adding contacts to it in M3 (the send loop). So during M1/M2, dedup against master_contacts is read-only. Fine. But the brief says scope can be `all_campaigns` — meaning if you start campaign B while campaign A is in the middle of sending, B's M1 will not see A's in-flight contacts yet, defeating the dedup.
2. Two campaigns running concurrently in two terminals will both call `Deduper.commit()` on the same `data/master_contacts.csv` file. `.tmp`+rename means the last commit wins — the other campaign's appends are silently lost.
3. `send_emails.py` says "Append `(email, domain, name, role, slug, now)` to `data/master_contacts.csv`" — this is a single-row append on every send. Doing it via full-rewrite-tmp-rename is wasteful (rewrites the entire growing global file on every send). Doing it via simple append risks corruption if `poll_bounces.py` is also running.

**Recommendation:** Pick one model and document it: (a) per-process file lock (`fcntl.flock` on macOS/Linux) around all writes to `data/master_contacts.csv` and `data/suppression.csv`, with a documented "don't run two `send_emails.py` processes at once" constraint; OR (b) append-only with a single-writer invariant enforced by a pidfile. Add a test: simulate two concurrent appends and assert both end up in the final file. Also: §6.2 should NOT do a full rewrite per send; it should `open(path, "a")` append with a lock. The full-rewrite pattern in `csv_schema.rewrite_csv` is fine only for the `Deduper.commit()` reconciliation path.

---

## Issue: M3 `send_emails.py` daily counter has a TOCTOU window

**Severity:** should-fix
**Where:** §6.2 (`send_emails.py`, steps 3 and 6)

**Problem:** The flow is "read `send_counters.json` → check cap → send → increment counter." If the Gmail API call succeeds but the process crashes or is killed between steps 4 and 6, the email IS sent but the counter is not updated. Next run will see the counter as N-1 and send one more than the cap. This is a serious deliverability foot-gun: hitting `send_rate_per_day` in v1 is the difference between "happy run" and "24-hour Gmail lockout."

A separate but related concern: the counter is keyed by `from_gmail`, but `send_counters.json` is described as `{"YYYY-MM-DD": {"from@gmail.com": N}}`-shape (implicitly). The plan doesn't actually specify the schema. What about timezone? If the counter rolls over at UTC midnight but the user's "day" is PST midnight, you get an early reset that lets you exceed the daily cap by up to `send_rate_per_day` extra.

**Recommendation:** (1) Increment counter BEFORE the Gmail API call (pessimistic accounting), then decrement on hard failure. This caps over-send at 0, not N. (2) Specify the JSON schema and timezone in §6.2. Use Pacific (or the user's local) timezone and document it. (3) Add a test that kills the process between API success and counter update and verifies the next run does NOT exceed the cap.

---

## Issue: `--confirm-test` gate is bypassable and the Phase A/B boundary is ambiguous

**Severity:** should-fix
**Where:** §6.2 (`send_emails.py`, Phase decision)

**Problem:** The gate is "if `n_sent < send_test_count`: Phase A; else if no `--confirm-test`: refuse." But `n_sent` is `progress/send_emails.json`-derived. The user can trivially delete that file (or one row in it) and bypass the test gate. More likely failure mode: if `send_test_count=10` and Phase A had 3 emails marked `error` (Gmail 429 storm), is `n_sent=7` or `n_sent=10`? The plan says "Count rows with status=sent" — so n_sent=7. Phase A will then try to send 3 more emails. But what if those 3 fresh emails also error? The user can be stuck looping in Phase A. Worse: what if Phase A "succeeds" with status=sent but the 10 emails actually went to spam? The test-batch deliverability check is meaningless because the user doesn't know whether 7 sent + 3 error counts as "the test batch."

Also: §6.3's "Phase A: 12 OutboxRows, `send_test_count=10` → exactly 10 sent" test doesn't cover the "what if 3 of the 10 error" case.

**Recommendation:** (1) Define `n_sent` semantics precisely: `n_sent` = count of `status in ("sent", "skipped_suppressed")`. Errors don't count and force re-attempt. But cap retries (e.g., 3) so a permanently-failing recipient doesn't block Phase A from completing. (2) Add a test: 12 rows, 3 of first 10 error 3 times → those 3 marked terminal, Phase A advances to row 13 to fill the test batch. (3) Add a `Phase A complete` sentinel to `progress/send_emails.json` that gets written exactly once when Phase A finishes; gate Phase B on the sentinel, not on the row count.

---

## Issue: `LLMClient` cascade has unclear refusal handling and missing structured-output gotcha guards

**Severity:** should-fix
**Where:** §2.6 (`lib/llm.py`)

**Problem:** The plan says `parse()` returns `(None, cost)` "On refusal or empty `output_parsed`." Several issues:

1. The OpenAI `responses.parse` refusal mechanism puts the refusal text on `resp.output[0].refusal`, not just `output_parsed=None`. The plan conflates "model refused for safety" (rare, signal: don't retry) with "model returned no parseable output" (more common, signal: maybe retry/escalate). These need separate handling.
2. The `cascade()` policy "if tier1 returns None, try tier2" — but research §B.4 says escalation should also trigger on "low-confidence." How is low-confidence detected? The schemas have a `confidence: float` field for `DiscoveryPerson` but not for `DomainExtractionItem`. Inconsistent.
3. The strict-mode gotcha (§B.4: "Every property in the schema must be in `required` (use `Optional[X]` for null-able)") is mentioned in §9.1 risk #4 but not actually enforced in the type definitions. `Optional[X]` in Pydantic does NOT automatically make a field optional in the JSON schema OpenAI receives; you need `default=None`. If the Pydantic model uses bare `Optional[str]` without `default=None`, OpenAI's strict mode WILL reject the request. The plan's `DomainExtractionItem`, `DiscoveryPerson`, etc. use `Optional[X]` notation without defaults.

**Recommendation:** (1) In §2.6, separately surface `(parsed=None, refusal_text="...", cost)` vs `(parsed=None, refusal_text=None, cost)` — only the latter should be re-tried/escalated. (2) Specify in §2.6 the exact cascade trigger conditions, including a `low_confidence_threshold: float` parameter. (3) Add a `model_config = ConfigDict(extra="forbid")` and `default=None` to every `Optional` field in §2.8 schemas. Add a test that runs every model through `openai.lib._tools.pydantic_function_tool` (or whatever helper) to verify strict-mode compliance. Make this a precondition: tests/lib/test_csv_schema.py must include "every schema is OpenAI-strict-mode-compliant."

---

## Issue: Compose stage has no idempotency or skew tolerance for the LLM first-name call

**Severity:** should-fix
**Where:** §6.2 (`compose_emails.py`, step 1)

**Problem:** The plan says "Cache: per-campaign in-memory dict to avoid re-asking for the same `name`." But this is a per-process cache. On `--resume` after a kill, you re-call the LLM for every name you already canonicalized. For a 1500-row campaign with 200 unique ambiguous names, that's an extra ~$0.50–$2 per resume and unnecessary LLM jitter.

Worse: the LLM's `FirstNameResult.first_name` can differ between calls (LLMs are non-deterministic even at temp=0 with web_search). After a resume, you can have two `OutboxRow`s for the same `name` field — one says "Marie-Claire" and one says "Marie." Inconsistent personalization across a single campaign.

Also: the "ambiguous" detection rules (multi-token first names, hyphenated, "Jr."/"Sr." present, non-Latin script) aren't fully specified. "Mary Jane" with personalize=true triggers LLM, but the LLM might return "Mary" (wrong). The test list says `"Marie-Claire" → returns "Marie-Claire"` and `"李伟" → returns canonicalized form` — but doesn't define what "canonicalized form" is, or test the "Mary Jane" case which the plan explicitly calls out as ambiguous.

**Recommendation:** (1) Persist the first-name cache to disk (e.g., `progress/first_name_cache.json` in the campaign dir, or a column in `progress/compose_emails.json`). On resume, load it. (2) Define the ambiguity rules formally and test each branch. (3) Use temperature 0 for this call and add a test that confirms the same input deterministically maps to the same output (allowing for one canonical answer, accepting some flakiness). (4) Add a "Mary Jane" test case to §6.3 explicitly.

---

## Issue: `gmail.authorize` scope expansion in M4 will silently break M3

**Severity:** must-fix
**Where:** §7.2 (`poll_bounces.py` step 1), §2.7 (`lib/gmail.py`)

**Problem:** M3 uses `gmail.send` scope only. M4 says "Authorize Gmail via `lib/gmail.authorize` (requires `gmail.readonly` scope; first run may re-prompt for added scope)." Adding a new scope to an existing OAuth token doesn't merge; it forces re-consent. But the way `authorize()` is described in §2.7 — "Run OAuth flow if no token; refresh if expired" — there's no logic to detect that the existing token's scope set is a subset of the requested scopes. The user will get a confusing "permission denied" error on `poll_bounces.py` and may not know they need to delete `token.json` and re-auth.

Additionally: §B.1 of research notes scope creep triggers stricter Google verification — adding `readonly` to a Workspace app may flag it for the app-review process if the OAuth consent screen isn't "Internal."

**Recommendation:** In §2.7, the `authorize()` signature already takes `scopes: list[str]`. Add explicit logic: compare requested scopes to `creds.scopes`; if there are new ones, force re-flow (delete and recreate token). Add a clear error message: "Gmail token has scopes [X]; required [X, Y]. Re-authorizing." Add this to the test plan in §3.3 or §7.3. Also document in README that M4 requires re-auth.

---

## Issue: Missing observability requirements that the engine will need from day 1

**Severity:** should-fix
**Where:** §2.3 (`lib/observability.py`), §3.4 (M0 acceptance)

**Problem:** The `Observer` interface is missing several things that downstream stages will need but aren't surfaced:

1. **Cost roll-up across stages.** `status.md` shows "Cost so far: $18.40" but each stage has its own Observer and `tick({"cost": x})` only updates that stage's status. There's no campaign-level cost view. M3 sends, M2 verifies, M1 sources — the user wants total spend. The `Observer` model needs a campaign-level aggregator.
2. **Stage-to-stage handoff.** When M1 completes, the campaign's `status.md` should show "Stage 1 COMPLETED, Stage 2 next" so when Stage 2 starts and instantiates its own Observer, it doesn't overwrite the completed-stage info. The current `set_stage_status` does individual-stage status, not pipeline status.
3. **Failure attribution.** "`event(level="error")` writes ERROR to activity.log but doesn't change tick state." But the test says "On error: writes status.md showing 'FAILED'." The semantics conflict. Does a single `event(level="error")` set the stage to FAILED, or does that need a separate `finish(status="FAILED")` call? What about transient errors (rate-limit, retried successfully)?
4. **No `stage_start()` method** — every test expects a "stage X starting" event but no method is in the interface.

**Recommendation:** Add to §2.3: a `CampaignObserver` (singleton, owns `status.md` pipeline section) vs `StageObserver` (owns stage-section + activity.log lines) split. Or simpler: every Observer instance reads existing `status.md` on init and preserves prior-stage headers. Specify in §2.3 whether `event(level="error")` is "transient warning" or "FAIL the stage." Probably: `event(level="error")` is a logged warning; `finish(status="FAILED")` is the terminal call. Document this with examples.

---

## Issue: `web_citation` verifier interacts badly with discovery's `email_if_known` semantics

**Severity:** should-fix
**Where:** §2.10 (`lib/verifiers/base.py`), §5.2 (`web_citation.py`)

**Problem:** The web_citation verifier signature is `verify(email, *, citation_url)`. But where does `citation_url` come from? The contact discovery stage writes `email_source_url` in the `ContactRow`. So `verify_emails.py` passes `email_source_url` as `citation_url`. But:

1. The plan says discovery should "Always require `source_url` in every result item (hallucination guard)" — but `ContactRow.email_source_url` is `Optional[str]`. If the LLM provides the email but no URL (or fabricates a URL), what happens?
2. The `web_citation` verifier returns `accepted` based purely on "is the URL not on the aggregator blocklist." This is weak: an LLM that's hallucinated an email can also hallucinate a plausible-looking primary URL (e.g., `https://huckberry.com/about-us`) that doesn't actually contain the email. The verifier accepts it, the email is added to outbox, the email is sent, it bounces, suppression list grows. Yet the bot reported "verified-web."
3. The research doc §B.4 explicitly recommends "Post-validate URLs: HEAD must 200; fetch page must mention the claimed entity name" — this is "5–10× hallucination reduction" — but the plan's `web_citation` verifier does NO HTTP fetch. It just checks the hostname.

**Recommendation:** Either (a) make `web_citation` actually fetch the URL and string-search for the email, OR (b) downgrade `web_citation` confidence below `verified-smtp` in the cascade so its acceptance is treated as low-confidence and another verifier (api_provider) is tried first. The minimum acceptable v1 behavior is to HEAD-200 the URL and string-search the (probably gzipped) HTML for the local-part of the email; reject if not present. Add this to §5.2 and to the test plan.

---

## Issue: Plan doesn't address what happens when stage outputs are inconsistent with brief

**Severity:** should-fix
**Where:** §5.2 (verify pre-flight), §6.2 (compose pre-flight)

**Problem:** The plan does pre-flight for verifier availability (port 25 etc.) but doesn't pre-flight for cross-stage state validity:

1. M2's `verify_emails.py` is invoked. What if `contacts.csv` is empty (M1 returned zero domains)? Or doesn't exist? The plan doesn't say. Probably it should print a clear error referencing M1's progress file.
2. What if `brief.yaml` is modified between stages? E.g., user changes `verifier.chain` after M2 partial-completes. The progress file's already-verified rows are based on the old chain; new rows would be based on the new chain. Inconsistent `confidence` values in `emails.csv`.
3. What if the user changes `message.template` between M3 invocations on the same campaign? Phase A used one template, Phase B uses another. The 10 test-batch recipients got a different message than the bulk recipients.

**Recommendation:** Add a "brief fingerprint" concept: `progress/brief_hash.txt` written on first stage that uses the brief. Each stage checks if `hash(brief.yaml) == saved_hash`; if not, refuse to run with a clear message ("brief changed since stage N; either revert or start a fresh campaign"). Add tests for each stage. Also: each stage should pre-flight check the existence and minimum-row-count of its input file.

---

## Issue: M2 worker exception handling is underspecified and can leave the pipeline stuck

**Severity:** should-fix
**Where:** §5.2 (`discover_contacts.py`), §5.3 (test plan)

**Problem:** The test plan says "Worker exception in one thread: other workers continue, the bad domain marked `worker_exc`." Good — but the plan doesn't say:

1. What types of exceptions get caught? Network timeouts, JSON decode errors, OpenAI 5xx, Pydantic validation errors all need different handling. Catching all of them with a bare `except Exception` makes debugging genuinely hard.
2. Does `worker_exc` count as terminal (don't retry on `--resume`) or non-terminal (retry)? The §2.2 progress.py interface doesn't distinguish. Looking at the prior art (§A.1 in research): `worker_exc` is one of the Phase 2 status enums but the plan doesn't say if it's retried.
3. What if every domain fails with `worker_exc` (e.g., OpenAI API key revoked mid-run)? The pipeline silently finishes "successfully" with an empty `contacts.csv`. The user has no signal until they look at M3 and find 0 outbox rows.

**Recommendation:** (1) Specify in §5.2 which exceptions are retried (transient: 429, 5xx, ConnectionTimeout, asyncio.TimeoutError) vs marked-terminal (Pydantic validation, 4xx auth errors). (2) Add a "failure budget" — if >20% of items fail in a stage, halt with a clear error rather than producing degraded output. (3) Add a test for `worker_exc` on `--resume`: does it retry, or is it skipped? Document the decision. The prior art used the "retry if not in `progress.json`" model; the plan should explicitly say `worker_exc` IS in `progress.json` and IS retried on `--resume`.

---

## Issue: SMTP probe rate-limit reality is misaligned with brief defaults

**Severity:** should-fix
**Where:** §2.9 (`lib/rate_limit.py`), §5.2 (smtp_probe verifier)

**Problem:** The plan defaults to `rate_per_sec: 3.0` and `per_hour_cap: 100`. But research §B.2 explicitly says: "Default rate `--max-rate 3.0` from prior art is too aggressive for sustained runs (Spamhaus threshold ~50–100/hour ≈ 0.01–0.03/sec). Override `verifier.rate_limit` in `config/defaults.yaml` to a per-hour cap rather than a per-second cap, with burst tolerance."

So a campaign with 1500 domains × ~3 candidates each = 4500 probes / 100 per hour cap = 45 hours of verification, and the rate-per-second of 3.0 is way over the per-hour-cap-implied rate of 0.03. The `HourlyLimiter` is built to handle this but the values are inconsistent with research.

Also: the test "10 calls at rate=2.0/sec → takes ~5s ±0.5s" doesn't exercise the hourly cap, which is the actually-important constraint for Spamhaus protection.

**Recommendation:** (1) Set the default to `per_hour_cap: 50` and `rate_per_sec: 0.5` (burst-allowed for the first few, then settle into the hourly cap). (2) Add a test for sustained-rate behavior: 200 calls at hourly cap=50/hour should take ~4 hours, not 200/2.0 = 100 seconds. (3) Surface estimated-verification-time in the brief-validation step (e.g., "Verifying 4500 candidates at 50/hr will take ~90 hours; consider enabling `api_provider` or splitting the campaign"). This prevents a foot-gun where the user runs M2 and discovers it'll take a week.

---

## Issue: No plan for what happens between stages — the orchestrator gap

**Severity:** should-fix
**Where:** §8 of claude-spec.md and §1.1, M4 polish in plan

**Problem:** `CLAUDE.md` v2 in M4 is described as "incorporate lessons from the first real run" but the plan never specifies what Claude Code actually does between stages. Concretely:

1. Who decides when to invoke `verify_emails.py` vs `discover_contacts.py`? The user? Claude Code? In what order? The plan implies Claude Code runs M1→M2→M3→M4 sequentially without stopping, but Claude Code's behavior is not deterministic — it might error mid-pipeline, the user might Ctrl-C, the network might fail.
2. What's the resume story across stages? If M1 completed but M2 is running and dies, does Claude Code re-run M1 to be safe? Or does it know M1's progress file is terminal?
3. The Stage 0 interview (CLAUDE.md v1) and the brief-validator (`brief.py`) overlap. If Claude Code generates an invalid `brief.yaml`, the validation error needs to flow back to Claude Code so it can fix the brief and retry. Currently there's no spec for that loop.

**Recommendation:** Add §8.5 or §11 to the plan: "Inter-stage orchestration." Specify:
- A `scripts/run_pipeline.py` wrapper that runs stages sequentially and reports cross-stage status, OR explicitly delegate orchestration to Claude Code and document the prompts in `CLAUDE.md`.
- A "stage status check" command (`scripts/status.py --campaign-dir X`) that prints which stages are complete, in-progress, failed. Claude Code calls this between every action to know where it is.
- How brief-validation errors are surfaced to Claude Code (probably: structured error JSON to stderr with a non-zero exit, and a documented protocol Claude Code uses to recover).
