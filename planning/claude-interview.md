# Detailed Interview — Outreach Bot

Two upfront-locked decisions arrived with the original ask (verifier default, test-batch destination, Gmail tier, default `send_test_count`). The interview below covers everything that surfaced during research.

---

## Pre-interview locked decisions

**Q0.1 — Email verification default?**
A: `smtp_probe` first → `web_citation` fallback. Paid API (`api_provider`) implemented behind a feature flag, NOT the default. Verifiers pluggable via the `Verifier` interface (design doc §6).

**Q0.2 — 10-email test batch destination?**
A: First 10 *real* recipients. Doubles as a deliverability check. No dry-run-to-self mode in v1.

**Q0.3 — Gmail account tier?**
A: Paid Google Workspace. Default `send_rate_per_day = 1500` (under the ~2000/day ceiling). Default `throttle_seconds = 45`.

**Q0.4 — Plan depth?**
A: Deep plan with multi-LLM review (full `/deep-plan` flow).

---

## Round 1 — Architecture-level decisions

**Q1.1 — Search backend for Stages 1 & 2 (OpenAI hosted `web_search` vs Brave Search API)?**
A: **Stay with OpenAI hosted `web_search`.** Matches prior art; no new dependencies. Plan does NOT pull in Brave Search API.

**Q1.2 — Build automatic Gmail warmup mode into v1, or stick with the brief's fixed `send_rate_per_day`?**
A: **Skip warmup.** Trust the 1500/day cap. The throttle (45s default with jitter) handles human-pacing. No ramp logic.

**Q1.3 — Compliance: how to handle `List-Unsubscribe` headers / postal address / CAN-SPAM?**
A: **Skip all of it.** Direct quote: *"stop caring about whatever Google enforcement issues there are, dont add unnecessary footer text or whatever."* No `List-Unsubscribe` header. No `List-Unsubscribe-Post`. No compliance footer in templates.

**Q1.4 — Postal address ready?**
A: **Ignore CAN-SPAM entirely.** Direct quote: *"ignore can-spam, that is completely useless."* The brief schema does NOT include a `postal_address` field.

---

## Round 2 — Verification stage details

**Q2.1 — Greylisting (4xx temporary failures) in `smtp_probe.py`?**
A: **Configurable in brief.** Field `verifier.greylist_retry: true | false` (default `true`). When true, 4xx → wait 90s → 1 retry → if still 4xx, mark `"unknown"`. When false, 4xx → `"unknown"` immediately.

**Q2.2 — Outlook/O365/Proofpoint/Mimecast MX tarpits?**
A: **Hard-skip by MX hostname pattern.** Detect `*.mail.protection.outlook.com`, `*.olc.protection.outlook.com`, `*.pphosted.com`, `*.ppe-hosted.com`, `*.mimecast.com`. Mark such domains as `"catchall"` immediately without opening a connection. Cascade automatically falls to `web_citation`.

**Q2.3 — Pattern-only confidence tier?**
A: **Drop the tier entirely.** Don't emit `pattern-only` rows at all. Discovery returns either a verified-source-cited email (which we'll verify-smtp + verify-web) or nothing. Saves probe compute and keeps `emails.csv` clean.

**Q2.4 — SMTP-probe pre-flight check failure (port 25 blocked, e.g., off-VPN)?**
A: **Abort verify stage with clear remediation.** Print actionable error referencing the brief (`Connect to Dartmouth VPN, or set verifier=web_citation in the brief, or enable api_provider with a key`). Exit code 2. Pipeline stops cleanly — no silent fallthrough.

---

## Round 3 — Scope and follow-up

**Q3.1 — Stage 6 (Track & follow-up) scope for v1?**
A: **Bounce tracking only.** A small `scripts/poll_bounces.py` reads the user's Gmail inbox for hard-bounce notifications, parses out the affected recipient address, and appends it to `data/suppression.csv`. No reply detection, no auto follow-up bump, no campaign report. Replies + follow-up are explicit v2 scope.

**Q3.2 — Personalization in Stage 4 (composition)?**
A: **Minimal personalization.** Direct quote: *"There should be personalization in the sense that you fill in the template I give you, with one LLM call per recipient if needed. The LLM call will likely only be needed to see what the first name of the person is and replace a placeholder with that name."*

Concretely:
- Templates use slots like `{{first_name}}`, `{{company}}`, `{{role}}`, `{{value_prop}}`.
- Most slots are filled deterministically from the brief and from `emails.csv`.
- `{{first_name}}` is filled by splitting the `name` field on whitespace and taking the first token — no LLM call needed for the easy 95%.
- For ambiguous names (e.g., `"Dr. Robert J. Smith Jr."`, `"Li Wei"`, hyphenated names), the compose stage MAY fall back to a tiny `gpt-4.1-mini` call to extract the salutation-form first name. This fallback is at-most-one-call-per-recipient.
- The design doc's "custom opening line per recipient" idea is **dropped from v1.**

**Q3.3 — LLM cache (SQLite)?**
A: **Skip caching entirely in v1.** Don't add the dependency. The ~$5–20 cost per campaign run is acceptable.

**Q3.4 — Test lifecycle (cleanup-after-commit vs keep)?**
A: **Keep ALL tests permanently — override the global rule for this repo.** Tests go in `tests/` mirroring source layout. They live in the repo as a regression suite. This is a deliberate per-project override of the global CLAUDE.md guidance.

---

## Out of scope for v1 (explicitly)

Collected here so the plan-writing step doesn't accidentally re-introduce:
- Automatic warmup / ramp send logic
- `List-Unsubscribe` and `List-Unsubscribe-Post` headers
- CAN-SPAM compliance scaffolding (postal address, opt-out language, unsubscribe footer)
- Brave Search / Tavily / any non-OpenAI search backend
- LLM cache (`data/llm_cache.sqlite`)
- Pattern-only confidence tier (no `pattern-only` rows emitted)
- Reply detection
- Auto follow-up email at day +4
- Campaign report generation
- Custom-opening-line personalization (the "Claude writes a per-recipient hook" idea from the design doc)
- HTTPS unsubscribe endpoint
- Geographic recipient exclusion (DE/AT/FR etc.)

## Decisions made for the plan (locked)

- **6-stage pipeline collapses to 5 in v1:** Stage 0 brief → Stage 1 source domains → Stage 2 discover contacts → Stage 3 verify → Stage 4 compose → Stage 5 send → (Stage 6 reduced to just `poll_bounces.py`).
- **Verifier cascade:** `smtp_probe` (with MX-pattern hard-skip + brief-configurable greylist retry) → `web_citation` (primary-source detection via aggregator hostname denylist) → `api_provider` (behind feature flag, off by default).
- **LLM stack:** OpenAI hosted `web_search` tool for search; structured outputs (Pydantic + `strict=true`) for extraction; tier-1 = `gpt-4.1-mini`, tier-2 = `gpt-5` (escalate only on empty / low-confidence / refusal). Startup-time `MODEL_FALLBACKS` probe verifies which models reachable.
- **Observability:** `lib/observability.py` is cross-cutting; every stage calls `observability.milestone()` which (a) rewrites `status.md`, (b) appends to `activity.log`, (c) prints `[stage] ...` to stdout. Cadence: every N items per stage OR every 120 seconds, whichever first.
- **Tests:** in `tests/` permanently; mirror `scripts/` layout.
- **Brief schema** matches design doc §3 verbatim EXCEPT: no `compliance.*` block, no `sending.warmup_mode`, no `accept_levels` entry for `pattern-only`, plus add `verifier.greylist_retry: true|false`.

Interview complete.
