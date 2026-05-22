<!-- PROJECT_CONFIG
runtime: python-uv
test_command: uv run pytest
END_PROJECT_CONFIG -->

<!-- SECTION_MANIFEST
section-01-skeleton-and-config
section-02-lib-foundations
section-03-lib-observability
section-04-lib-llm-and-gmail
section-05-noop-and-orchestration
section-06-source-domains
section-07-discover-contacts
section-08-verifiers
section-09-verify-emails
section-10-compose-emails
section-11-send-emails
section-12-bounces-and-polish
END_MANIFEST -->

# Implementation Sections Index

Sections map onto milestones M0–M4 from `claude-plan.md`. M0 is split into four sections (01–05) because the cross-cutting libraries are independent enough to parallelize. M1 = §06, M2 = §07–§09, M3 = §10–§11, M4 = §12.

## Dependency Graph

| Section | Depends On | Blocks | Parallelizable |
|---|---|---|---|
| section-01-skeleton-and-config | - | 02, 03 | Yes (alone) |
| section-02-lib-foundations | 01 | 03, 04, 06, 08 | No (foundation) |
| section-03-lib-observability | 02 | 05, 06, 07, 09, 11 | Yes (parallel with 04) |
| section-04-lib-llm-and-gmail | 02 | 05, 06, 07, 10, 11, 12 | Yes (parallel with 03) |
| section-05-noop-and-orchestration | 03, 04 | 06 | No (M0 gate) |
| section-06-source-domains | 02, 03, 04, 05 | 07 | No (M1 = single script) |
| section-07-discover-contacts | 06 | 09 | Yes (parallel with 08) |
| section-08-verifiers | 02, 03 | 09 | Yes (parallel with 07) |
| section-09-verify-emails | 07, 08 | 10 | No (M2 gate) |
| section-10-compose-emails | 09 | 11 | No |
| section-11-send-emails | 03, 04, 10 | 12 | No (M3 gate) |
| section-12-bounces-and-polish | 11 | - | No (M4 = polish) |

## Execution Order (with parallelization)

1. **section-01-skeleton-and-config** (no dependencies).
2. **section-02-lib-foundations** (after 01) — `brief`, `csv_schema`, `progress`, `rate_limit`, `dns_check`.
3. **section-03-lib-observability** AND **section-04-lib-llm-and-gmail** — *parallel* after 02. Different concerns; tests don't share fixtures.
4. **section-05-noop-and-orchestration** (after 03 + 04) — `noop_stage.py`, `status.py`, `run_pipeline.py`, brief-hash invariant. Closes M0.
5. **section-06-source-domains** (after 05) — `source_domains.py`. M1 complete.
6. **section-07-discover-contacts** AND **section-08-verifiers** — *parallel* after 06. Both depend on M1 conceptually but not on each other's outputs.
7. **section-09-verify-emails** (after 07 + 08) — `verify_emails.py`. Closes M2.
8. **section-10-compose-emails** (after 09) — `compose_emails.py`.
9. **section-11-send-emails** (after 10) — `send_emails.py`. Closes M3.
10. **section-12-bounces-and-polish** (after 11) — `poll_bounces.py`, playbook fills, README v2. Closes M4.

## Section Summaries

### section-01-skeleton-and-config
Repo plumbing only. `pyproject.toml` with dep list, `.gitignore`, README v1 with prerequisites, `templates/_brief_template.yaml`, `config/defaults.yaml`, `config/verifiers.yaml`, `config/secrets.example.env`, empty `playbooks/*.md` files (stubs), `CLAUDE.md` v1 orchestrator. No Python logic. Tests: shape-validation only (YAML parses, env file is well-formed, brief template loads via Pydantic).

### section-02-lib-foundations
The no-network, no-LLM library layer. Implements: `lib/brief.py`, `lib/csv_schema.py`, `lib/progress.py`, `lib/rate_limit.py`, `lib/dns_check.py`. Plus `tests/conftest.py` with shared fixtures (sample brief, tmp_campaign_dir, fake DNS). The OpenAI-strict-mode schema test for `csv_schema.py` is the M0 gate.

### section-03-lib-observability
`lib/observability.py` (`CampaignObserver` + `StageObserver` split per review issue #8), `lib/dedup.py` (with `fcntl.flock` model from review issue #2), and the data-dir lockfile helpers. Tests include the lost-update concurrency test (review issue #1) and the cross-process dedup test.

### section-04-lib-llm-and-gmail
`lib/llm.py` (OpenAI wrapper with refusal-vs-empty distinction, cascade logic, strict-mode-compliant Pydantic schemas — review issue #5). `lib/gmail.py` (OAuth flow with scope-superset detection per review issue #7; `send()`; `list_bounces()` deferred to section 12). Tests use mocked OpenAI client and mocked Gmail HTTP.

### section-05-noop-and-orchestration
`scripts/noop_stage.py` (the M0 plumbing-verifier; deleted at the start of section 06). `scripts/status.py` (read-only campaign inspector per review issue #13). `scripts/run_pipeline.py` (optional sequential runner). Brief-hash invariant helper in `lib/progress.py` (`write_brief_hash` / `check_brief_hash`). Tests verify the M0 acceptance criteria from `claude-plan.md §3.4`.

### section-06-source-domains
Stage 1: `scripts/source_domains.py`. Closes M1. Implements: query-generation prompt, per-query LLM extraction with structured outputs, filter/dedup/DNS-validate, target-count termination. Plus `playbooks/02-domain-sourcing.md`. Tests use mocked LLM returning canned `DomainExtractionResponse`.

### section-07-discover-contacts
Stage 2 first half: `scripts/discover_contacts.py`. Parallel worker pool (review issue #1 queue-based writer pattern), exception taxonomy + failure budget (review issue #11), `DISCOVERY_SYSTEM_PROMPT` template-substituting brief sections. Plus `playbooks/03-contact-discovery.md`.

### section-08-verifiers
The pluggable verifier layer: `lib/verifiers/base.py`, `lib/verifiers/smtp_probe.py` (with MX tarpit hard-skip and greylist retry), `lib/verifiers/web_citation.py` (with HEAD-200 + local-part string-search per review issue #9), `lib/verifiers/api_provider.py` (behind feature flag). Plus `playbooks/04-email-verification.md`. Tests use `aiosmtpd` fake SMTP server for `smtp_probe`.

### section-09-verify-emails
Stage 3: `scripts/verify_emails.py`. Closes M2. Walks the verifier chain per row, hard-skips pattern-only candidates (pattern-only tier dropped per interview Q2.3), enforces per-company verified cap, rate-limited via `HourlyLimiter`. Tests integrate all three verifiers via mocks.

### section-10-compose-emails
Stage 4: `scripts/compose_emails.py`. Template rendering, first-name extraction with formal ambiguity rules + persistent on-disk cache (review issue #6), lint warnings. Plus `playbooks/05-email-composition.md`. Tests cover every ambiguity branch ("Mary Jane", "Marie-Claire", "Robert J. Smith", "李伟", "Robert Jr.").

### section-11-send-emails
Stage 5: `scripts/send_emails.py`. Closes M3. Phase A test-batch with sentinel, Phase B bulk with `--confirm-test` gate (review issue #4), pessimistic daily counter (review issue #3), suppression hard-gate, `.send.pid` lockfile, throttle with jitter. Plus `playbooks/06-sending.md`. Tests use mocked Gmail and mocked clock for counter/throttle behavior.

### section-12-bounces-and-polish
Stage 6 (thin): `scripts/poll_bounces.py` with `gmail.list_bounces` implementation. Plus M4 polish: fill in all `playbooks/*.md` stubs, README v2 with a worked-example walkthrough, `CLAUDE.md` v2 incorporating lessons. End-to-end manual smoke test documented in `tests/manual/smoke_test_m4.md`. Tests cover scope-re-auth (review issue #7) and idempotency.

## Notes for section writers

- Every section's output should be a self-contained markdown file (`sections/section-NN-name.md`) that an implementer can read top-to-bottom without flipping back to `claude-plan.md` or `claude-plan-tdd.md`. Quote the relevant function signatures, test stubs, and acceptance criteria inline.
- The locked decisions, locking models, brief-hash invariants, exit codes, and error taxonomies in `claude-plan.md §10` apply to every section. Sections should reference them, not re-derive them.
- Out-of-v1-scope items (per `claude-spec.md §1.3`) must not creep back in: no `List-Unsubscribe` headers, no warmup, no LLM cache, no Brave search, no pattern-only tier, no reply detection, no follow-up bump, no campaign report, no postal address, no geo filtering, no HTTPS unsubscribe.
