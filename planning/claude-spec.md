# Outreach Bot — Synthesized Specification

This document combines the original design doc (`outreach-bot-design-and-plan.md`), research findings (`claude-research.md`), and interview answers (`claude-interview.md`) into a single normative spec the implementation plan can be derived from. Where the original design doc and the interview disagree, the interview wins.

---

## 1. Product goal

A reusable, Claude-Code-driven system for running cold-outreach campaigns end to end. The user describes the campaign in a sentence; Claude Code interviews the user to fill a `brief.md`; then the pipeline sources domains → discovers contacts → verifies emails → composes → sends → tracks bounces. Each campaign lives in its own folder; the engine is stable and reusable.

## 2. Architectural split

Two layers:
- **Engine** — `CLAUDE.md`, `playbooks/`, `scripts/`, `scripts/lib/`, `config/`, `templates/`, `data/`. Stable across campaigns.
- **Campaign** — one folder per run under `campaigns/<YYYY-MM>_<slug>/`. Contains `brief.md`, per-stage CSVs, progress files, `status.md`, `activity.log`. Disposable.

The interface between layers is `brief.md`. Every script reads from a loaded brief; nothing in the engine layer is allowed to hardcode segment-specific values (segment definitions, role priorities, rate limits, value prop, recipient identity).

## 3. Repo layout (authoritative)

```
email_automation/
├── CLAUDE.md                      # Orchestrator: instructs Claude Code how to run a campaign
├── README.md                      # Human-facing setup + quickstart
├── pyproject.toml                 # Python deps, single source of truth
├── .gitignore                     # ignores config/secrets.env, token.json, data/, campaigns/*/
│
├── playbooks/
│   ├── 00-pipeline-overview.md
│   ├── 01-target-definition.md
│   ├── 02-domain-sourcing.md
│   ├── 03-contact-discovery.md
│   ├── 04-email-verification.md
│   ├── 05-email-composition.md
│   ├── 06-sending.md
│   └── 07-tracking-followup.md
│
├── scripts/
│   ├── source_domains.py
│   ├── discover_contacts.py
│   ├── verify_emails.py
│   ├── compose_emails.py
│   ├── send_emails.py
│   ├── poll_bounces.py            # v1 Stage 6 scope: bounce tracking only
│   └── lib/
│       ├── brief.py
│       ├── progress.py
│       ├── observability.py
│       ├── dedup.py
│       ├── dns_check.py
│       ├── llm.py
│       ├── gmail.py
│       ├── csv_schema.py
│       ├── rate_limit.py
│       └── verifiers/
│           ├── base.py
│           ├── smtp_probe.py
│           ├── web_citation.py
│           └── api_provider.py    # behind feature flag, off by default
│
├── config/
│   ├── defaults.yaml
│   ├── verifiers.yaml
│   └── secrets.example.env
│
├── templates/
│   ├── ai-agent-integration.md    # the first real template
│   └── _example.md
│
├── campaigns/
│   └── <YYYY-MM>_<slug>/
│       ├── brief.md
│       ├── domains.csv
│       ├── contacts.csv
│       ├── emails.csv
│       ├── outbox.csv
│       ├── sent.log
│       ├── status.md
│       ├── activity.log
│       └── progress/
│           ├── source_domains.json
│           ├── discover_contacts.json
│           ├── verify_emails.json
│           ├── compose_emails.json
│           └── send_emails.json
│
├── data/
│   ├── master_contacts.csv        # every contact ever discovered
│   ├── suppression.csv            # do-not-contact (bounces, opt-outs)
│   └── send_counters.json         # per-account daily send counters, persists across restarts
│
└── tests/
    ├── lib/
    │   ├── test_brief.py
    │   ├── test_progress.py
    │   ├── test_observability.py
    │   ├── test_dedup.py
    │   ├── test_dns_check.py
    │   ├── test_llm.py
    │   ├── test_gmail.py
    │   ├── test_csv_schema.py
    │   ├── test_rate_limit.py
    │   └── verifiers/
    │       ├── test_smtp_probe.py
    │       ├── test_web_citation.py
    │       └── test_api_provider.py
    ├── test_source_domains.py
    ├── test_discover_contacts.py
    ├── test_verify_emails.py
    ├── test_compose_emails.py
    ├── test_send_emails.py
    └── conftest.py                # shared fixtures (sample brief, fake LLM client, fake Gmail client)
```

Tests are permanent (per the interview override of the global "clean up tests after ship" rule).

## 4. Brief schema (final, with interview deltas)

The brief is a markdown file with a YAML frontmatter block, OR a pure YAML file. The plan should pick one and stick with it (recommendation in the plan: pure YAML for machine-readability, with a sibling `brief.md` containing free-form notes). The schema (single source of truth):

```yaml
# Identity
slug: medium-retailers           # required, kebab-case, used in folder name
created_at: 2026-05-21           # ISO date, auto-filled

# Target — what's being targeted
target:
  segment: "Medium-sized multi-brand retailers"   # required
  include: ["curated marketplaces", "hybrid retailer-brands"]
  exclude: ["pure single-brand DTC", "enterprise (>$500M rev)"]
  geography: "US + Canada"
  target_domain_count: 1500       # required, int

# Who to contact (leverage)
who_to_contact:
  priority_roles:
    - Founder
    - CEO
    - VP E-commerce
    - Head of Digital
    - CTO
  deprioritize:
    - Marketing
    - PR
    - HR
    - "generic info@"
  contacts_per_company: 3         # default 3, max 12

# Message
message:
  template: templates/ai-agent-integration.md     # required, path relative to repo root
  value_prop: "Integrate AI shopping agents on your storefront"
  personalize_first_name: true    # whether to LLM-canonicalize first names; default true
  from_name: "Smrjit"
  from_gmail: "smrjit@example.com"
  reply_to: "smrjit@example.com"

# Verification
verifier:
  chain: [smtp_probe, web_citation]   # ordered cascade
  greylist_retry: true            # if true, 4xx → wait 90s → 1 retry → mark "unknown"
  rate_limit: 3.0                 # SMTP probes/sec; also used as upper bound

# Sending
sending:
  send_test_count: 10             # send this many first, then PAUSE for approval
  send_rate_per_day: 1500         # Workspace default
  throttle_seconds: 45            # base gap; actual delay = base * uniform(0.5, 1.5)

# Safety
safety:
  dedup_scope: all_campaigns      # all_campaigns | this_campaign
  require_approval_after: [send_test]   # only hard stop

# Notes (free text)
notes: |
  Anything Claude Code should know about this segment.
```

**Schema diff from design doc §3:**
- DROPPED `compliance.*` block (no postal address, no `List-Unsubscribe`).
- DROPPED `sending.warmup_mode`.
- DROPPED `accept_levels: pattern-only` option (the entire `pattern-only` tier is removed).
- ADDED `verifier.greylist_retry`.
- ADDED `message.personalize_first_name` (replaces the design doc's "custom opening line" toggle with a narrower scope).
- RENAMED `verifier` from a single string to a structured block with `chain`, `greylist_retry`, `rate_limit`.

## 5. Pipeline stages (5 in v1, plus a thin Stage 6)

### Stage 0 — Brief
`CLAUDE.md` instructs Claude Code to: read the user's one-sentence ask, run a fixed interview to fill any gaps, write `brief.yaml` (+ optional `brief.md` notes), and confirm once. Then proceed.

### Stage 1 — Domain sourcing (`source_domains.py`)
- Reads brief.
- Strategies (in `playbooks/02-domain-sourcing.md`): curated source URL lists; per-sub-category OpenAI `web_search` calls; LLM extraction from each result via structured outputs.
- Filters: include/exclude rules from brief; DNS pre-check via `lib/dns_check.py`; dedup against in-flight set + `data/master_contacts.csv` + `data/suppression.csv`.
- Output: `campaigns/<slug>/domains.csv`.
- Progress: `progress/source_domains.json` for `--resume`.
- Live: `status.md` + `activity.log` + `[source]` milestone every 50 domains or 120s.

### Stage 2 — Contact discovery (`discover_contacts.py`)
- Reads `domains.csv`.
- Per domain (parallel workers): DNS-validate, then OpenAI structured-output call with `web_search` tool to return up to `contacts_per_company` people (`name`, `role`, `leverage_rationale`, `email_if_known`, `source_url_if_known`, `confidence`).
- Output: `contacts.csv` (unverified candidates).
- Progress: `progress/discover_contacts.json`.
- Live: `[discover]` milestone every 20 companies or 120s.

### Stage 3 — Verification (`verify_emails.py`)
- Reads `contacts.csv`.
- Pre-flight: if `smtp_probe` is in the chain, call `verifiers/smtp_probe.assert_available()`. On failure, print actionable error and exit 2.
- Per candidate: walk the `verifier.chain` (default `[smtp_probe, web_citation]`).
  - `smtp_probe`: HELO → MAIL FROM → RCPT TO candidate → RSET → RCPT TO random → QUIT. Decision: both 250 → `"catchall"`; only candidate 250 → `"accepted"`; otherwise `"rejected"`. **MX hostname hard-skips** for `*.mail.protection.outlook.com`, `*.olc.protection.outlook.com`, `*.pphosted.com`, `*.ppe-hosted.com`, `*.mimecast.com` → return `"catchall"` without opening a connection. **Greylist retry** if `greylist_retry: true`: 4xx → wait 90s → retry once → still 4xx → `"unknown"`.
  - `web_citation`: accept the candidate only if `source_url_if_known` passes `is_primary_source()` (denylist of ~18 aggregator hosts).
  - `api_provider`: behind feature flag (`config/verifiers.yaml: api_provider.enabled: false`). If enabled and API key present, call provider; map result to `accepted | rejected | catchall | unknown`.
- Stop after `contacts_per_company` verified wins per company.
- Output: `emails.csv` (verified rows only; `confidence` enum is `verified-smtp | verified-web | verified-api`).
- Progress: `progress/verify_emails.json`.
- Live: `[verify]` milestone every 20 candidates or 120s, with fill-count breakdown.

### Stage 4 — Composition (`compose_emails.py`)
- Reads `emails.csv` + the brief's `message.template`.
- Per row:
  - Naive split: `first_name = name.split()[0]` after stripping titles ("Dr.", "Mr.", "Ms.", "Mrs.", "Prof.").
  - If `message.personalize_first_name: true` AND the naive split looks ambiguous (multi-token first name like "Mary Jane", hyphenated, contains "Jr.", non-Latin script), call a tiny `gpt-4.1-mini` LLM to canonicalize. At-most-one call per recipient.
  - Render template with slots: `{{first_name}}, {{name}}, {{company}}, {{role}}, {{value_prop}}, {{from_name}}`.
  - Lints (warnings, not blocking): subject is all-caps; body contains URL shortener (`bit.ly`, `t.co`, etc.); body has 0 line breaks; body length > 500 words.
- Output: `outbox.csv` (`to_email, to_name, subject, body, body_plain`).
- Progress: `progress/compose_emails.json`.
- Live: `[compose]` milestone every 50 rows or 120s.

### Stage 5 — Send (`send_emails.py`)
- Reads `outbox.csv` + `data/suppression.csv` (hard gate: drop any row whose `to_email` is in suppression).
- Reads `data/send_counters.json` to check today's already-sent count for `message.from_gmail` against `sending.send_rate_per_day`.
- **Phase A — Test batch:** send the first `send_test_count` (default 10) for real, throttled. Each send: `gmail.send(...)`, append to `sent.log`, increment counter, update `status.md`. **After phase A, STOP.** Print: "Sent 10 to first real recipients. Check your Gmail Sent folder. Run `python scripts/send_emails.py --resume --confirm-test` to send the rest."
- **Phase B — Bulk send:** only runs with `--confirm-test`. Resumes from row 11, respects daily cap and throttle (`throttle_seconds * uniform(0.5, 1.5)`). If cap hit, prints rollover message and exits cleanly; next invocation resumes next day.
- Output: appended `sent.log` rows (timestamp, recipient, status, gmail_message_id).
- Progress: `progress/send_emails.json`.
- Live: `[send]` milestone every 25 sends or 120s.

### Stage 6 (thin) — Bounce tracking (`poll_bounces.py`)
- Reads the user's Gmail inbox via Gmail API (`gmail.readonly` scope added) for messages from `mailer-daemon@*` matching subjects like "Delivery Status Notification (Failure)".
- Parses out the affected recipient address (`Final-Recipient:` header in the bounce body).
- Appends to `data/suppression.csv` with `reason=hard_bounce, source=<gmail_message_id>, date=<ISO>`.
- Idempotent: tracks last-processed message ID in `data/poll_bounces_state.json`.
- v1 deliverable: standalone script; user invokes manually (`python scripts/poll_bounces.py`) or wires via cron.

### Out of v1 scope
- Reply detection (manual: user reads their own inbox)
- Auto follow-up bump
- Campaign report
- Custom opening-line personalization
- `List-Unsubscribe` headers
- Postal address / CAN-SPAM scaffolding
- Warmup ramp
- LLM cache
- Brave/Tavily search backends
- Geographic recipient filtering
- HTTPS unsubscribe endpoint

## 6. Cross-cutting libraries (`scripts/lib/`)

- **`brief.py`** — Pydantic model matching §4 schema. `load(path)` returns validated `Brief`. Raises `BriefValidationError` with actionable messages on missing/invalid fields.
- **`progress.py`** — `ProgressStore(path)` with `load()`, `mark(key, status, **extras)`, `is_done(key)`, atomic `.tmp`-rename snapshots. Implements `--resume` semantics.
- **`observability.py`** — `Observer(campaign_dir, stage)` with `milestone(counters)`, `event(message, level)`, `set_status(line)`. Maintains in-memory state, writes `status.md` (overwrite) + `activity.log` (append) + stdout milestone lines. Cadence: configurable per stage (default 50 items / 120s) — every `milestone()` call decides whether to actually emit.
- **`dedup.py`** — `Deduper(scope)` reading `data/master_contacts.csv` + `data/suppression.csv`. `is_known(email_or_domain)`, `add(row)`, `commit()`. Scope respects brief's `safety.dedup_scope`.
- **`dns_check.py`** — `mx_records(domain)`, `has_mail(domain)` (true if MX or A; respects null MX per RFC 7505). Cached in-memory; resolver is `dns.resolver` from `dnspython`.
- **`llm.py`** — `LLMClient` wrapping OpenAI Responses API with `parse(text_format=PydanticModel, ...)`, two-tier cascade (`gpt-4.1-mini` → `gpt-5`), startup-time `MODEL_FALLBACKS` probe, per-call cost tracking, exponential-backoff retry on 429.
- **`gmail.py`** — OAuth setup helper (`authorize()`), `send(to, subject, body, body_plain, from_address)`, `list_bounces(since_message_id)`. Token storage in `config/secrets.env` (path; the actual `token.json` is at `config/token.json`, gitignored).
- **`csv_schema.py`** — Pydantic models for every CSV row: `DomainRow`, `ContactRow`, `EmailRow`, `OutboxRow`, `SentLogRow`, `SuppressionRow`, `MasterContactRow`. `read_csv(path, model)` and `write_csv_row(path, row, model)` helpers; atomic via `.tmp` rename.
- **`rate_limit.py`** — Token-bucket `RateLimiter(rate_per_sec)` + clock-based `HourlyLimiter(per_hour)`. Both reused across verification and sending.
- **`verifiers/base.py`** — Abstract `Verifier` interface: `name: str`, `verify(email, *, citation_url) -> VerificationResult`. `VerificationResult` is `Pydantic` with `status: Literal['accepted','catchall','rejected','unknown']`, `confidence: str`, `source_url: str`.

## 7. Observability (cross-cutting)

`Observer` is instantiated once per stage at startup. Every stage:
1. On start: `obs.event("stage X starting", level="info")`, writes `status.md` showing "RUNNING — stage X of 5".
2. On every item: `obs.tick(counters)` → may or may not actually emit, based on cadence rules. When it emits: append `[stage] ...` line to stdout + `activity.log`; rewrite `status.md`.
3. On stage finish: `obs.event("stage X complete", level="info")`, writes `status.md` showing "COMPLETED stage X / next: stage X+1".
4. On error: `obs.event(traceback, level="error")`, writes `status.md` showing "FAILED — see activity.log".

`status.md` is a templated markdown file with a fixed structure (so it's diffable and grep-able). Example in design doc §4.

## 8. Orchestration (`CLAUDE.md`)

`CLAUDE.md` at the repo root encodes the SOP. Sketch (verbatim from design doc §8, lightly adjusted for v1 scope):

```markdown
# How to run an outreach campaign

When the user describes a target:
1. Create `campaigns/<YYYY-MM>_<slug>/`, copy brief template, initialize status.md + activity.log.
2. Read `playbooks/01-target-definition.md`. Interview the user to fill the brief. Confirm once.
3. Run stages 1–4 without stopping. Post a chat milestone every ~2 minutes; status.md stays current.
4. Stage 5 — Sending:
   a. Send first 10 emails (test batch) from user's Gmail, throttled.
   b. STOP. Tell user the 10 are out; ask them to check Gmail Sent folder. Ask explicit go/no-go.
   c. On approval, run with `--confirm-test` to send the rest under daily cap + throttle. Suppression updated on every bounce (Stage 6).
5. Report final summary. Mention that the user can `python scripts/poll_bounces.py` periodically to catch bounces.

Rules:
- Always pass `--resume`; never restart a stage from scratch.
- Never send beyond the 10-email test without explicit approval.
- Never exceed `send_rate_per_day`. If a campaign needs more, spread across days (Phase B exits cleanly on cap-hit).
```

## 9. Testing strategy

Tests live in `tests/` permanently and mirror `scripts/`. Every section of the implementation plan must include a TDD-oriented test list with:
- Happy-path tests
- Edge-case tests (the global CLAUDE.md asks for these): null/missing fields, network failures, catch-all responses, kill-during-stage + resume, cross-campaign dedup collisions, LLM rate-limit errors, Gmail API quota exhaustion
- Property-ish tests where applicable (e.g., resume after kill → output identical to non-killed run)

Runner: `pytest`. Mocks for external services:
- `lib/llm.py`: a fake `LLMClient` in `tests/conftest.py` returning canned Pydantic instances.
- `lib/gmail.py`: a fake `GmailClient` that records sends without calling Google.
- `lib/verifiers/smtp_probe.py`: a fake SMTP server (`aiosmtpd` or a small socket mock) for the test runner.
- `lib/dns_check.py`: monkeypatch `dns.resolver`.

## 10. Security model (per user global CLAUDE.md)

- All sensitive logic is server-side (Python on user's machine). No browser-side anything.
- API keys (OpenAI, Google OAuth, optional `api_provider`) live in `config/secrets.env`, which is gitignored. `config/secrets.example.env` is the template, committed.
- Gmail OAuth token lives in `config/token.json`, gitignored.
- Suppression list is a hard gate before every send (checked in `send_emails.py`'s phase-A and phase-B loops).
- No logging of full email bodies in `activity.log`; only `to_email`, `subject`, `gmail_message_id`, `status`.
- No logging of LLM API responses verbatim (they may contain PII); log token counts + cost + model name only.
- Rate limiting on `verify_emails.py` and `send_emails.py` against external services.

## 11. Build milestones (refinement of design doc §10)

The plan will sectionize work into milestones following these boundaries. Each milestone is independently shippable; user can pause between milestones.

- **M0 — Skeleton + plumbing + observability** (1 day)
- **M1 — Domain sourcing** (½ day)
- **M2 — Contacts + pluggable verification** (1 day)
- **M3 — Composition + Gmail send + test-batch flow** (1 day)
- **M4 — Bounce tracking + polish** (½ day)

Total: ~4 days. Each milestone has acceptance tests (see design doc §10 for the user's intent).

## 12. Open issues for plan-writing

- Whether `brief.md` is markdown-with-YAML-frontmatter or pure YAML. Plan should pick: **pure YAML (`brief.yaml`)** with optional `brief.md` for notes — easier to validate.
- Default `target_domain_count` if user doesn't specify (plan should pick: warn at validate-load if missing; no default).
- Whether `discover_contacts.py` should call the LLM in batch (10 domains per call) or per-domain. Research says batching saves 10x but complicates retry. Plan should pick: **per-domain in v1** (simpler; we have headroom on cost).
- Whether to support Anthropic Claude in addition to OpenAI in `lib/llm.py`. Plan should: **OpenAI only in v1**, abstract minimally so adding Anthropic later is trivial.

These are minor enough that the plan-writer can choose; they're noted for completeness.
