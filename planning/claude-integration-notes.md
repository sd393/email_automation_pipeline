# Integration Notes — iteration 1 review

Source: `reviews/iteration-1-opus.md`

All 13 issues are valid and worth integrating. Below is the triage: what I'm integrating, what I'm partially integrating, what I'm consciously NOT changing.

| # | Issue | Severity | Decision |
|---|---|---|---|
| 1 | ProgressStore + CSV writes concurrency hazard | must-fix | **Integrate fully.** Add `threading.Lock`, document writer model, update tests. |
| 2 | Cross-stage cross-campaign dedup race | must-fix | **Integrate fully.** Use `fcntl.flock` on shared `data/` files + document single-`send_emails.py` constraint. |
| 3 | M3 daily counter TOCTOU | should-fix | **Integrate fully.** Pessimistic accounting (increment before send, decrement on hard failure). Specify schema + timezone. |
| 4 | `--confirm-test` gate + Phase A/B ambiguity | should-fix | **Integrate fully** the n_sent semantics + Phase A sentinel. **Do NOT integrate** the "user-deletes-file bypass" defense — that's the user's own machine, not a threat model. |
| 5 | LLMClient refusal handling + strict-mode gotchas | should-fix | **Integrate fully.** Distinguish refusal vs empty-output; add `default=None` + `extra="forbid"` to all Optional fields; add strict-mode-compliance test. |
| 6 | Compose first-name cache idempotency | should-fix | **Integrate fully.** Persist cache to `progress/first_name_cache.json`. Add "Mary Jane" test. |
| 7 | `gmail.authorize` scope expansion breaks M3 | must-fix | **Integrate fully.** Detect scope superset in `authorize()`; force re-auth with explicit message. Document in README. |
| 8 | Observability missing campaign-level cost / handoff / failure | should-fix | **Integrate fully.** Split into `CampaignObserver` (owns pipeline-level status.md sections) + `StageObserver` (owns stage-specific sections). Add `stage_start()` to interface. Clarify `event(error)` vs `finish(FAILED)` semantics. |
| 9 | `web_citation` weak hallucination guard | should-fix | **Partially integrate.** Add HEAD-200 + local-part string-search in the page body. This is the "minimum acceptable v1" the review names. Do NOT add full crawl + multi-source agreement — keeps v1 simple. Document residual risk. |
| 10 | Plan misses brief-changed-between-stages | should-fix | **Integrate fully.** `progress/brief_hash.txt` written by first stage; each subsequent stage refuses if hash mismatches. |
| 11 | M2 worker exception underspecified + failure budget | should-fix | **Integrate fully.** Specify exception-class taxonomy; `worker_exc` IS retried on `--resume`; hard halt at >20% failure budget. |
| 12 | SMTP probe rate-limit defaults misaligned with research | should-fix | **Integrate fully.** New defaults: `rate_per_sec=0.5`, `per_hour_cap=50`. Brief-load warning when total candidates × cap ratio implies multi-day verification. |
| 13 | No inter-stage orchestrator | should-fix | **Integrate fully.** New `scripts/status.py` (read-only campaign state inspector). New `scripts/run_pipeline.py` (sequential runner with fail-fast). Document brief-validation-error contract for Claude Code. |

## What I'm consciously NOT changing

- **Issue 4's "delete the progress file to bypass" angle.** This is the user's local machine; defending against it adds complexity without benefit. The pessimistic-counter and sentinel changes address the real failure modes (process crash, error storms).
- **Issue 9's full crawl + multi-source agreement.** Out of scope for v1. The HEAD-200 + string-search captures the cheap, high-leverage 80%; full crawl is a v2 improvement.
- **Issue 13's `run_pipeline.py` being mandatory.** I'll add it as an option, but Claude Code can still drive each stage individually. The `status.py` is more important; that's how Claude Code (or the human) finds out where things stand between invocations.

## Where the integration goes

Edits land in `claude-plan.md`:
- §2.2: add `threading.Lock`, document queue-based writer pattern.
- §2.3: split Observer into `CampaignObserver` + `StageObserver`; document semantics.
- §2.4: add `fcntl.flock` to all `data/` writes.
- §2.6: refusal vs empty distinction; cascade trigger spec; strict-mode requirement.
- §2.7: scope-subset detection.
- §2.8: `default=None` on all Optional fields + `extra="forbid"` + strict-mode test.
- §2.9: revised defaults; new test for sustained rate.
- §3.2/§3.3: M0 acceptance includes brief-hash invariant + concurrency tests.
- §5.2: discover_contacts exception taxonomy + failure budget; web_citation HEAD-200 + string-search; verify pre-flight checks brief hash + input file.
- §6.2: compose persistent first-name cache; send pessimistic counter + Phase A sentinel + timezone spec.
- §7.2: M4 documents the re-auth requirement.
- §8: new sub-section §8.5 covering `scripts/status.py` and `scripts/run_pipeline.py`, brief-validation-error contract.
- New §11 listing v1 invariants the plan now depends on (locks, hashes, timezone, etc.) so a reader doesn't have to scan for them.

## Why every-issue-integrated

Each of the 13 surfaces an honest gap. None of them recommend adding features the user excluded. They're all defenses against silent failure modes (lost data, double-sends, Gmail lockout, inconsistent personalization, stuck pipelines). For a tool the user will rely on over months, taking the review pass is a clear win.
