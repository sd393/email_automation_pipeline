Now I have all the context I need. Let me write the section-09-verify-emails content.

# Section 09: Verify Emails (verify_emails.py — closes M2)

## Purpose

Implements Stage 3 of the outreach pipeline. Reads `contacts.csv` (per-candidate rows from Stage 2) and walks the configured verifier chain to produce `emails.csv` — a verified-only output that downstream composition and send stages consume.

This section is the gate for Milestone M2. Once it lands, the pipeline has end-to-end coverage from domain sourcing through email verification.

## Dependencies (other sections, reference only)

- **section-07-discover-contacts** — produces the `contacts.csv` input. The `ContactRow` schema is defined in `lib/csv_schema.py` (from section-02). Each row carries `email_if_known: Optional[str]`, `confidence: float` (LLM confidence), plus identifying fields.
- **section-08-verifiers** — provides `lib/verifiers/base.py` (the `Verifier` Protocol, `VerificationResult`, `VerifierUnavailable`) and the three concrete verifiers: `lib/verifiers/smtp_probe.py`, `lib/verifiers/web_citation.py`, `lib/verifiers/api_provider.py`. This section CHAINS those verifiers — it does not implement them.
- **section-02-lib-foundations** — `lib/brief.py` (`Brief` and the nested `VerifierSection`), `lib/csv_schema.py` (`ContactRow`, `EmailRow`, plus `read_csv` / `write_csv_row`), `lib/progress.py` (`ProgressStore`, plus `check_brief_hash` / `write_brief_hash`), `lib/rate_limit.py` (`RateLimiter`, `HourlyLimiter`).
- **section-03-lib-observability** — `CampaignObserver` + `StageObserver`.

Do NOT re-derive contracts from those sections. Treat their interfaces as fixed.

## Background context (so this section reads standalone)

### What the verifier chain does

Each candidate row from `contacts.csv` has a possibly-non-null `email_if_known` (extracted by the discovery LLM from a primary or aggregator source). Stage 3 asks: is this email real?

We answer that by walking the brief's `verifier.chain` — an ordered list of verifier names like `["smtp_probe", "web_citation"]` or `["api_provider", "smtp_probe", "web_citation"]`. The first verifier whose result is "good enough" wins, and we write an `EmailRow` to `emails.csv`.

"Good enough" is verifier-specific:
- `smtp_probe` → `status="accepted"` wins. `status="catchall"` does NOT win on its own (catch-all servers accept any RCPT, including made-up addresses, so they prove nothing about whether the specific local-part is real).
- `web_citation` → `status="accepted"` wins. This verifier already gates "accepted" on a primary-source body match (per section-08), so a win here means the email literally appears on the company's own page.
- `api_provider` → `status="accepted"` wins.

If no verifier in the chain reports `accepted`, the candidate is NOT written to `emails.csv`. Progress is marked accordingly (`verified` / `unverified` / `verifier_exc`) so `--resume` knows where to pick up.

### Hard skips and caps

- **Pattern-only candidates (`email_if_known is None`) are skipped entirely in v1.** This is a deliberate scope decision from the interview (Q2.3) — the prior art's "guess a pattern like first@domain.com and probe it" tier is dropped. If discovery didn't find an email, we don't make one up.
- **Per-company verified cap.** After we have `who_to_contact.contacts_per_company` verified `EmailRow`s for a given domain, we stop probing further candidates from that same domain. This keeps probe volume bounded and matches the cap that discovery already enforces in the other direction.

### Rate limiting

SMTP probes against the same MX get a domain flagged for abuse fast — Spamhaus's SBL flags static IPs at roughly 100 probes/hr. The brief carries `verifier.rate_limit` (a `rate_per_sec` and `per_hour_cap`). Defaults from `claude-plan.md §2.9`: `rate_per_sec=0.5`, `per_hour_cap=50`, `burst=10`. The script wires `RateLimiter` AND `HourlyLimiter` around every verifier call.

The estimated-time pre-flight (see test list below) warns if `len(candidates_to_probe) / per_hour_cap > 8h`. Informational only — does not block.

### Pre-flight contract (uniform across M2/M3 scripts)

1. **Brief load:** call `lib.brief.load(<campaign_dir>/brief.yaml)`. On `BriefValidationError`, the wrapper emits the exit-3 JSON contract on stderr (per `claude-plan.md §10` exit codes) and exits 3.
2. **Brief-hash check:** read `progress/brief_hash.txt`; if it differs from `sha256(brief.yaml bytes)`, exit 2 with: `"Brief changed since previous stage. Revert brief or start a fresh campaign."`
3. **Input-file check:** `contacts.csv` exists AND has ≥ 1 data row. Otherwise exit 2 with `"No contacts. Run discover_contacts.py first."`
4. **Verifier chain instantiation + availability:** for each verifier name in `brief.verifier.chain`, instantiate it from `config/verifiers.yaml` config (and secrets where applicable), then call `.assert_available()`. Any `VerifierUnavailable` is fatal — print the exception's remediation message and exit 2 BEFORE writing anything to `emails.csv`.
5. **Estimated-time check:** count candidates that will actually be probed (`email_if_known is not None`), divide by `verifier.per_hour_cap`. If `> 8h`, print a warning to stdout and to `activity.log` via `obs.event(level="warn", ...)`. Continue.

Exit codes follow `claude-plan.md §10`: 0 success, 1 refused (not used in this script), 2 stage failure (pre-flight or halt), 3 brief validation error.

## Files to create

- `scripts/verify_emails.py` — the new stage script.
- `tests/test_verify_emails.py` — integration tests (mocked verifiers).

Files referenced but NOT created here (they come from prior sections):
- `scripts/lib/brief.py`, `scripts/lib/csv_schema.py`, `scripts/lib/progress.py`, `scripts/lib/rate_limit.py`, `scripts/lib/observability.py`, `scripts/lib/verifiers/*` — all read-only consumers.
- `config/verifiers.yaml` — read at startup to enable/disable verifiers.

## CLI

```
python scripts/verify_emails.py --campaign-dir campaigns/<slug> [--resume] [--workers 5]
```

`--workers` defaults to 5. Workers are `ThreadPoolExecutor` threads; per `claude-plan.md §2.2` concurrency model they push results to a `queue.Queue` and the main thread is the sole writer of `emails.csv` and `progress/verify_emails.json`.

## Per-candidate verification flow

For each `ContactRow` in `contacts.csv` (after dedup filter and progress-skip):

1. **Skip pattern-only candidates.** If `row.email_if_known is None`: mark progress `pattern_only_skipped` and continue. Do not probe. Do not write to `emails.csv`.

2. **Check per-company cap.** Maintain an in-memory `dict[domain, int]` counter of verified wins per domain. If `verified_per_domain[row.domain] >= brief.who_to_contact.contacts_per_company`: mark progress `company_cap_reached` and continue.

3. **Suppression hard-gate.** If `dedup.is_suppressed(row.email_if_known)`: mark `skipped_suppressed`, continue. (Suppression is global per `claude-plan.md §2.4`.)

4. **Walk the verifier chain.** For each `Verifier` in `chain`:
   - Acquire `RateLimiter` AND `HourlyLimiter` (blocking; verifier-scoped instances shared across workers).
   - Call `verifier.verify(email=row.email_if_known, citation_url=row.email_source_url)`.
   - Inspect `VerificationResult`:
     - If `result.status == "accepted"`: emit an `EmailRow`, increment `verified_per_domain[row.domain]`, mark progress `verified` with the winning verifier name + confidence, break out of the chain loop.
     - Else (`catchall`, `rejected`, `unknown`): record the result in a per-row "trace" list and continue to the next verifier.
   - If all verifiers in the chain return non-accepted: mark progress `unverified` with the trace; do NOT write to `emails.csv`.

5. **Exception handling within a verifier call** (matches `claude-plan.md §10` taxonomy):
   - Transient (`socket.timeout`, `dns.exception.Timeout`, `ConnectionError`, `requests.Timeout`, 429s the verifier itself didn't already swallow): retry once per row with a small jittered backoff; if still failing, treat as `unknown` status and continue to the next verifier.
   - Terminal-skip: anything else from the verifier call is caught at the worker boundary, recorded as `verifier_exc` with truncated exception text in `progress.json`, candidate is NOT written. On `--resume`, `verifier_exc` is retried (it's in the retriable set per `lib/progress.py`).
   - Halt: `VerifierUnavailable` raised AFTER pre-flight succeeded is unexpected; surface it as a stage failure via `obs.finish(status="FAILED", ...)` and exit 2.

6. **Failure budget.** Track `n_failures / n_processed` across the run. If `> 20%` AND `n_processed > 20`, halt with diagnostic per `claude-plan.md §5.2`. Re-runnable with `--resume`.

## EmailRow construction

From `claude-plan.md §2.8` and section-02 — the `EmailRow` shape is fixed:

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

The script populates each field from the contributing `ContactRow` plus the winning `VerificationResult`:
- `name`, `email`, `company`, `domain`, `role`, `leverage_rationale` from `ContactRow`.
- `confidence` from `VerificationResult.confidence` (one of the three `verified-*` literals).
- `source_url` from `VerificationResult.source_url` — for `smtp_probe` this is the sentinel `"https://verified-smtp/"`; for `web_citation` it's the real URL; for `api_provider` it's `"https://verified-api/"` (or provider-specific).
- `category` is read from a sidecar map of `domain → category` built from `domains.csv` at startup (Stage 1's output). If the domain is not present in `domains.csv` (shouldn't happen post-discovery, but defend), use the empty string and log a `warn`.

Append via `csv_schema.write_csv_row(<campaign-dir>/emails.csv, email_row)`. The helper writes the header on first append and is atomic-via-tmp-rename.

## Concurrency model recap

Per `claude-plan.md §2.2`: workers compute `VerificationResult`s; they push `(contact_row, winning_result_or_none, trace)` tuples onto a `queue.Queue`; the main thread is the sole consumer that calls `progress.mark()` and `csv.write_csv_row()`. This avoids per-CSV locking and matches the prior-art pattern.

`progress.json`'s `ProgressStore` is thread-safe regardless (per section-02's `RLock` defense), but the single-writer convention keeps the simple code simple.

## Observability

Use `StageObserver` with `stage="verify"`. Cadence defaults are fine (50 items / 120s — see section-03). Tick payload:

```python
obs.tick({
    "processed": n_processed,
    "verified": n_verified,
    "catchall": n_catchall_seen,
    "rejected": n_rejected,
    "unverified": n_unverified,
    "skipped": n_skipped,
    "cost": llm_cost_total,  # 0 in v1 since verifiers don't call LLM; reserved for the future
    "elapsed_s": int(time.monotonic() - t0),
})
```

Milestone-line example per `claude-plan.md §2.3`:
```
2026-05-21T14:03:22.901Z  [verify]  INFO   milestone: 612/1491 (41.0%) verified=1134 catchall=148 cost=$0.00 elapsed=22m
```

Transient retries (greylist, ConnectionError) → `obs.event(level="warn", ...)`. Terminal stage failure → `obs.finish(status="FAILED", summary=...)`. Per `claude-plan.md §2.3` there is NO `event(level="error")`.

## Configuration loading

`config/verifiers.yaml` (from section-01) controls which verifiers are enabled and their parameters:

```yaml
smtp_probe:
  enabled: true
  rate_per_sec: 3.0     # overridden by brief.verifier.rate_limit if present
  per_hour_cap: 100
web_citation:
  enabled: true
api_provider:
  enabled: false
  provider: zerobounce
```

Resolution order for chain entries (highest priority first):
1. `brief.verifier.chain` decides the order AND which verifiers are referenced.
2. `config/verifiers.yaml` provides defaults for each verifier's params; if a verifier appears in `brief.verifier.chain` but is `enabled: false` in `verifiers.yaml`, that is a pre-flight failure (exit 2 with `"Verifier 'api_provider' is in brief chain but disabled in config/verifiers.yaml."`).
3. `brief.verifier.rate_limit` overrides the per-verifier `rate_per_sec`/`per_hour_cap`.
4. Secrets (e.g., `ZEROBOUNCE_API_KEY`) are read from `config/secrets.env`. Missing required secret for an enabled verifier → `assert_available()` raises `VerifierUnavailable` with the documented message.

## Stub: top-level structure

```python
# scripts/verify_emails.py
"""Stage 3: walk the verifier chain over contacts.csv → emails.csv.

Closes M2. Reads brief, walks each contact's email_if_known through the
configured verifier chain, writes verified-only EmailRows. Resumeable via
--resume. Single-writer + queue concurrency model (see claude-plan.md §2.2).
"""

def main() -> int:
    """Parse args, pre-flight, run, emit exit code per claude-plan.md §10."""

def _preflight(campaign_dir: Path, brief: Brief, verifiers: list[Verifier]) -> None:
    """Brief-hash, input-file existence, verifier availability, est-time warn.
    Raises StagePreflightError(exit_code=2, message=...) on failure."""

def _build_verifier_chain(brief: Brief, verifiers_config: dict, secrets: dict) -> list[Verifier]:
    """Instantiate verifiers in brief.verifier.chain order; cross-check against
    config/verifiers.yaml enabled flags + secrets presence."""

def _verify_one(row: ContactRow, chain: list[Verifier],
                rate: RateLimiter, hourly: HourlyLimiter,
                domain_to_category: dict[str, str]) -> _Outcome:
    """Worker function. Walks chain. Returns _Outcome carrying either an
    EmailRow (on accepted) or progress status + trace (on no-accept)."""

def _drain_writer(out_queue: Queue, progress: ProgressStore, emails_csv: Path,
                  obs: StageObserver, brief: Brief) -> None:
    """Main-thread loop. Pops outcomes off the queue, writes EmailRow + marks
    progress + ticks observer. Enforces per-company cap. Enforces failure budget."""
```

`_Outcome` is a small internal dataclass (not part of any public API) carrying `contact_row`, `email_row | None`, `progress_status`, `progress_extras`. Defined locally in `scripts/verify_emails.py`.

## Tests FIRST (write these before the implementation)

These mirror `tests/test_verify_emails.py` from `claude-plan-tdd.md §5`. Use pytest with mocked verifier objects (NOT real SMTP / HTTP — those are exercised in section-08's unit tests).

```python
# tests/test_verify_emails.py
# Pipeline integration:
# Test: 3 contacts, chain=[smtp_probe, web_citation].
#       Contact 1: smtp_probe.verify -> status=accepted -> EmailRow with verified-smtp written.
#       Contact 2: smtp_probe.verify -> status=catchall (not accepted), web_citation.verify ->
#                  status=accepted -> EmailRow with verified-web written.
#       Contact 3: smtp_probe.verify -> status=rejected, web_citation.verify -> status=unknown
#                  -> NOT written to emails.csv; progress marked "unverified".

# Pre-flight:
# Test: missing contacts.csv → exit 2 with "No contacts. Run discover_contacts.py first."
# Test: brief-hash mismatch (mutate brief.yaml between runs) → exit 2 with documented message.
# Test: smtp_probe.assert_available raises VerifierUnavailable → exit 2; emails.csv not created;
#       remediation message printed to stderr.
# Test: brief.verifier.chain references api_provider, but config/verifiers.yaml has it
#       enabled=false → exit 2 with "Verifier 'api_provider' is in brief chain but disabled..." 
# Test: estimated-time > 8h (mock len(contacts) and per_hour_cap) → warning printed; run continues.
# Test: brief load failure (missing required field) → exit 3 with structured JSON on stderr.

# Per-company cap:
# Test: contacts_per_company=3, 5 contacts at the same domain, all would verify-accepted →
#       only the first 3 are probed/written; contacts 4 and 5 progress-marked "company_cap_reached";
#       verifier.verify called exactly 3 times for that domain.

# Pattern-only drop (interview Q2.3):
# Test: contact with email_if_known=None → no verifier called; progress marked
#       "pattern_only_skipped"; not in emails.csv.

# Suppression:
# Test: dedup.is_suppressed returns True for one contact's email → no verifier called for it;
#       progress "skipped_suppressed"; not in emails.csv.

# Chain ordering matters:
# Test: same 3 contacts as the pipeline-integration test, but chain=[web_citation, smtp_probe]
#       → for Contact 1 web_citation is tried first; if web_citation accepts, smtp_probe NEVER
#       called (verify count assertion).

# Resume:
# Test: kill at candidate 100/300 (simulate by setting up progress.json with 100 keys), resume →
#       only candidates 101–300 are processed; final emails.csv equals non-killed run's output.
# Test: a candidate marked verifier_exc in a prior run IS retried on --resume (matches §10).
# Test: a candidate marked verified in a prior run is NOT retried on --resume.

# Concurrency:
# Test: with workers=5 and 50 mocked contacts (each verifier returns accepted), all 50 EmailRows
#       land in emails.csv with no duplicates and no losses.
# Test: worker exception inside verifier.verify() → caught at worker boundary, progress marked
#       verifier_exc; other workers continue; main thread keeps writing.

# Rate limiting:
# Test: integration with HourlyLimiter — 60 mocked candidates with per_hour_cap=30 → mocked
#       monotonic clock advances by ≥ ~110 minutes (sustained-rate check from §2.9 review).
# Test: integration with RateLimiter — 10 candidates with rate_per_sec=2.0 → mocked monotonic
#       advances by ~5s.

# Failure budget:
# Test: 25 of 100 candidates raise unexpected exceptions (worker_exc) → stage halts with
#       diagnostic message after the 20% threshold trips (n_processed > 20).
# Test: 3 of 10 fail → continues (n_processed < 20).

# Observability:
# Test: status.md after the run shows "verified: N, catchall: M, rejected: K" counters matching
#       the actual outcomes.
# Test: activity.log has at least one milestone line for a 100-row run with cadence=50.
# Test: obs.finish(status="COMPLETED", summary=...) called exactly once on success.

# EmailRow fields:
# Test: winning verifier sets confidence correctly (smtp_probe→verified-smtp,
#       web_citation→verified-web, api_provider→verified-api).
# Test: source_url field uses VerificationResult.source_url from the WINNING verifier.
# Test: category field is populated from the domains.csv lookup; missing domain → "" + warn.
```

Mock strategy: define a `FakeVerifier(name, results_by_email: dict[str, VerificationResult])` test fixture that satisfies the `Verifier` Protocol. Tests assemble chains of FakeVerifiers with canned outcomes — no real SMTP or HTTP. The real verifier implementations are exercised in section-08's unit tests.

## Acceptance criteria

From `claude-plan.md §5.4` plus this section's additions:

1. On a real (small) brief with `chain: [smtp_probe, web_citation]` and 10 contacts, `emails.csv` is produced with only verified rows. Each row's `confidence` field is one of `verified-smtp`, `verified-web`, `verified-api`.
2. Swapping `chain: [web_citation, smtp_probe]` in the brief, with no other changes, alters which verifier wins for each row, demonstrably (visible in `progress/verify_emails.json` extras showing the trace).
3. Toggling `api_provider.enabled: true` in `config/verifiers.yaml` and adding it to `brief.verifier.chain` activates the third verifier; toggling back to `enabled: false` while still in the chain causes a clean pre-flight exit 2 with the documented message.
4. Pre-flight failures (port 25 blocked → `smtp_probe.assert_available` raises; missing API key; missing `contacts.csv`; brief-hash mismatch; brief validation error) each produce a distinct, actionable error message on stderr and the documented exit code.
5. `--resume` after `Ctrl-C` mid-run produces the same `emails.csv` content as a non-killed run (modulo ordering — verify by sorting both outputs).
6. `pytest tests/test_verify_emails.py` is green.
7. The per-company verified cap is honored — for a brief with `contacts_per_company=3` and 5 contacts at the same domain, no more than 3 rows for that domain appear in `emails.csv`.
8. The failure-budget halt fires when > 20% of candidates fail AND > 20 candidates have been processed.

## Out-of-scope reminders (do not creep in)

Per `claude-plan.md §1.3` and the section-index notes:
- No pattern-only tier. `email_if_known is None` → skipped, period.
- No geo filtering of recipients.
- No LLM call inside `verify_emails.py` itself. Verifiers are deterministic (SMTP probe, HTTP body match, third-party API). The LLM is for discovery (Stage 2) and first-name canonicalization (Stage 4), not verification.
- No `List-Unsubscribe`, no warmup logic, no campaign-report writing.
- No bounce handling here — that's Stage 6 (section-12).

Relevant absolute file paths to consume during implementation:
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/claude-plan.md` (sections §2.8, §2.9, §2.10, §5.2, §5.3, §10)
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/claude-plan-tdd.md` (§5 `verify_emails.py` test block)
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/planning/sections/index.md` (dependency graph)