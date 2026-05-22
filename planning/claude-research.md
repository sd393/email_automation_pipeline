# Research Findings — Outreach Bot

Source material:
- Codebase research (prior-art docs in `~/Downloads/`): `Phase 1 - Domain Search.md`, `Phase 2 - Email Discovery.md`, `find_email_process.md`, `brand_outreach_agent_spec.md`, plus any locatable `scrape-retailers.py` / `find-emails-bulk.py` source.
- Web research (2026): Gmail API send + OAuth, SMTP RCPT-TO probing & catch-all detection, cold-email deliverability + CAN-SPAM, LLM-driven web extraction patterns.

---

## Part A — Patterns to PORT from the prior art

### A.1 `progress.json` + resume machinery (port as-is)

Existing shape, keyed by URL or pseudo-key (`search:<query>`):
```json
{
  "https://example.com/listicle": {
    "status": "ok",
    "raw": 78,
    "count": 76,
    "cost": 0.45
  },
  "search:top 50 outdoor retailers US": {
    "status": "ok",
    "raw": 47,
    "count": 31
  }
}
```

Phase 1 status enum: `"ok" | "fetch_fail" | "too_short" | "extract_fail" | "search_fail"`.

Phase 2 status enum (per-domain): `"3-fill" | "2-fill" | "1-fill" | "0-fill" | "empty" | "empty-deep" | "no_people" | "discovery_fail" | "dns_fail" | "no_domain" | "worker_exc"`.

Where `N-fill` = count of verified emails produced for that company.

Resume contract:
- On startup, load `progress.json` + `output.csv` from disk.
- Skip any key already present in `progress.json` (status != error-class to retry).
- Pre-populate in-memory output rows from the CSV so we don't lose committed work.
- Every row addition: write CSV via `.tmp` rename (atomic). Every key completion: write `progress.json` via `.tmp` rename.
- Crash loss window: at most one in-flight worker's row.

### A.2 CSV schemas (port + generalize)

**Phase 1 — `domains.csv` columns:**
```
company_name, domain, is_pure_dtc, domain_inferred, category, source_url, notes
```
- `domain`: bare root, lowercase, no scheme, no www, no path.
- `is_pure_dtc`: stringly `"true"`/`"false"` — kept from LLM extraction; filtered at absorb time. **In new repo: rename to a generic `excluded`/`exclude_reason` keyed off brief.**
- `domain_inferred`: `"true"` when LLM guessed domain from company name only.
- `source_url`: actual URL scraped, or pseudo-key `search:<query>` for web-search results.
- `notes`: ≤300 chars.

**Phase 2 — `emails.csv` columns:**
```
name, email, company, domain, role, category, confidence, source_url, leverage_rationale
```
- `confidence` enum (exact strings): `verified-smtp` | `verified-web` | `pattern-only`. **`pattern-only` is never written to send batches** in the existing code; we keep that rule.
- `source_url` pseudo-values:
  - `https://verified-smtp/` — RCPT accepted, no citation
  - `https://verified-web/` — catch-all + primary source but no direct URL captured
  - real URL when citation exists

**Cross-campaign files (new in this repo):**
- `data/master_contacts.csv` — every contact ever discovered (one row per email).
- `data/suppression.csv` — do-not-contact (unsubscribes, bounces, opt-outs). Hard gate before every send.

### A.3 Model-fallback (port to `lib/llm.py`)

Existing chain:
```python
MODEL_FALLBACKS = ["gpt-5.2", "gpt-5", "gpt-4.1"]
```

`resolve_model(client)` mechanism (in `scrape-retailers.py:388`): iterate the chain, attempt a tiny call (`max_output_tokens=16`, input `"OK"`), return first model that succeeds; raise `RuntimeError` if all fail. Used once at startup.

Existing cost model (Phase 2 hardcoded):
```python
COST_PER_M_INPUT_USD = 5.0
COST_PER_M_OUTPUT_USD = 20.0
COST_PER_WEB_SEARCH_USD = 0.025
```
Per-call cost computed from `response.usage.input_tokens` + `output_tokens` + count of `web_search_call` items in `response.output`. Accumulate per-domain inside `progress.json` and emit total in milestone lines.

**Web research recommendation overrides this:** rebuild the cascade as **tier 1: `gpt-4.1-mini` / `gpt-5-nano`** for the easy 80%, **tier 2: `gpt-5`** only on empty/low-confidence/refusal. 10–20× cost savings. Keep `MODEL_FALLBACKS` available for upgrade if newer models exist at runtime.

### A.4 Deep-fallback verification cascade (port + abstract)

Existing `verify_candidate()` decision tree:
```
probe_email(domain, candidate) →
  "accepted" → ("smtp", citation_url or "https://verified-smtp/")
  "catchall" → if citation_url and is_primary_source(citation_url):
                   ("web", citation_url)
               else:
                   ("none", "")
  "rejected" / "unreachable" → ("none", "")
```

Strategy at the company level (`find-emails-bulk.py:403`): stop after 3 verified wins per company. Per person, build a queue `[web-search-best, web-search-alts, generated-patterns]` (6–12 candidates depending on deep mode). Try each candidate in order; first SMTP-or-WEB win is recorded. Session-level `session_dnt` set avoids re-probing same address within a company.

**Generalized in new repo:** this becomes the `Verifier` interface's *cascade implementation*. The cascade itself moves into `verify_emails.py` while each tier (`smtp_probe`, `web_citation`, `api_provider`) becomes a separate `Verifier` subclass.

### A.5 Milestone-cadence reporting (port + generalize)

Phase 1 (`scrape-retailers.py:483`):
```python
PROGRESS_EVERY_DOMAINS = 50
PROGRESS_EVERY_SECONDS = 120
```

Phase 2 (`find-emails-bulk.py:601`):
```python
PROGRESS_EVERY_COMPANIES = 20
PROGRESS_EVERY_SECONDS = 120
```

Pattern: emit a `[progress]` stdout line when *either* the item-count delta crosses the threshold *or* the time delta crosses 120s. Line includes ratio, fill-count breakdown, cost, elapsed time, error count.

**Generalize in new repo:** move both thresholds into `lib/observability.py` as `MILESTONE_EVERY_ITEMS` (per-stage override) and `MILESTONE_EVERY_SECONDS = 120` (global). Every stage calls `observability.milestone(stage, counters)` instead of printing directly. The same call also appends to `activity.log` and rewrites `status.md`.

### A.6 LLM prompts to PORT (and generalize)

Three big system prompts exist in the prior art:

1. **`EXTRACTION_SYSTEM_PROMPT`** (Phase 1, `scrape-retailers.py:199`) — extracts retailers from page HTML. Heavily retailer-specific: definitions of "retailer" vs "pure DTC", domain inference rules, exclusion rules ("skip media/blogs/aggregators").
2. **`SEARCH_SYSTEM_PROMPT`** (Phase 1, `scrape-retailers.py:265`) — same job but via `web_search` tool aggressively. Same retailer-specific definitions.
3. **`DISCOVERY_SYSTEM_PROMPT`** (Phase 2, `find-leverage-emails.py:258`) — finds 7 high-leverage contacts at a company. Embeds a four-tier role-priority list (decision authority → tech implementer → growth ops → likely-responsive backups), de-rank list (Marketing/PR/HR/store managers), and an email-rule section (primary-source only; aggregators don't count).

**Anti-pattern strip list:**
- All three prompts contain the *segment-specific definitions*. Lift those into the brief: `target.segment`, `target.include`, `target.exclude`, `who_to_contact.priority_roles`, `who_to_contact.deprioritize`, and pass them as runtime template variables to the prompts.
- The pitch sentence ("integrating AI shopping agents on your storefront") is hardcoded in the discovery prompt. Move to `message.value_prop`.
- The 7-person cap is hardcoded. Move to `who_to_contact.contacts_per_company` (default 7, override 12 for deep mode).

### A.7 SMTP probe specifics (port behind interface)

`smtp_probe()` (in `find-emails.py:257`):
```
HELO probe.local
MAIL FROM: probe@gmail.com
RCPT TO: <candidate@domain>          → code1
RSET
MAIL FROM: probe@gmail.com
RCPT TO: <random-{ts}-{rand}@domain>  → code2
QUIT

code1 in 250..259 and code2 in 250..259 → "catchall"
code1 in 250..259                      → "accepted"
otherwise                              → "rejected"
```

Pre-flight check (`assert_verifier_available()`): open port 25 to `gmail-smtp-in.l.google.com`; abort with exit 2 if blocked. **Port this as `verifiers/smtp_probe.py:assert_available()` and call it at the start of `verify_emails.py` when SMTP is configured.**

Rate limiter (`find-emails-bulk.py:245`): token-bucket with monotonic clock, 3 probes/sec default, configurable `--max-rate`. **Port as `lib/rate_limit.py`** and reuse for both verification and sending throttles.

### A.8 Web-citation verification (port the `is_primary_source` filter)

`AGGREGATOR_HOSTS` set (~18 hosts) in `find-leverage-emails.py:154`:
```
contactout.com, rocketreach.co/.com, zoominfo.com, apollo.io, lusha.com,
hunter.io, success.ai, snov.io, leadiq.com, salesintel.com, dropcontact.com,
getprospect.com, kendo.tools, signalhire.com, swordfish.ai, leadlist.com,
voilanorbert.com, skrapp.io, anymailfinder.com, nymeria.io, uplead.com
```

`is_primary_source(url)`: returns `False` if host is in `AGGREGATOR_HOSTS` (or its subdomain). **Port verbatim; allow brief to extend the list.**

### A.9 Anti-pattern strip table (mirrors design §11)

| Item | Action in new repo |
|---|---|
| `is_pure_dtc` filter at absorb time | DELETE — replaced by brief `target.exclude` |
| Hardcoded role priority list in prompt | DELETE from code; template-inject from brief |
| `PROGRESS_EVERY_*` constants | DELETE from per-stage scripts; centralize in `observability.py` |
| Hardcoded pitch sentence | DELETE; template variable `{{value_prop}}` from brief |
| Dartmouth-VPN-only assumption | RE-FRAME: pre-flight check at runtime; failure → suggest fallback verifier |
| `--max-rate 3.0` hardcoded default | KEEP as default in `config/defaults.yaml`; override via brief `verifier.rate_limit` |
| 7-person cap hardcoded | DELETE; brief `who_to_contact.contacts_per_company` |
| Aggregator host list hardcoded | KEEP as default in `verifiers/web_citation.py`; brief can extend |

---

## Part B — Best-practice findings from web research

### B.1 Gmail API send + OAuth (2026)

**What to actually do (highlights):**
1. **OAuth2 with a Desktop app credential** is the default. App-password+SMTP still works in Workspace 2026 with 2FA, but plain SMTP+password died May 2025 (LSA shutdown).
2. **Scope to `https://www.googleapis.com/auth/gmail.send` only.** Broader scopes (modify, readonly) trigger stricter Google verification.
3. **Refresh-token flow:** `google-auth-oauthlib` + `google-api-python-client`. Store refresh token in `token.json` next to script (gitignored). On startup: load `token.json`; if `creds.expired and creds.refresh_token` → `creds.refresh(Request())` (no browser).
4. **OAuth consent screen mode matters:** "Testing" mode → refresh tokens expire every 7 days; "Production" mode → long-lived. For a personal CLI with <100 users, "Testing" is fine if you're prepared to re-auth weekly.
5. **MIME construction:** `email.message.EmailMessage` → `base64.urlsafe_b64encode(msg.as_bytes()).decode()` → `service.users().messages().send(userId="me", body={"raw": raw})`. The `urlsafe_` prefix is the #1 footgun.

**Quota reality:**
- API quota (per project): 1,200,000 units/min, `messages.send` costs 100 units. Effectively unhittable for our scale.
- Send quota (per user/day, Workspace paid): **~2,000 messages/day, max 500 external recipients per message**. Soft throttling begins well before the ceiling.
- **For cold outreach the real safe number is 15–50/day per inbox**, not 2,000 — deliverability ceiling, not quota ceiling.
- Exceedance returns `550 Daily user sending limit exceeded` → account locked from sending ~24h.

**Implications for our plan:**
- `send_rate_per_day = 1500` is below the Workspace 2,000/day quota ceiling but **far above the recommended deliverability ceiling for a fresh sender.** Plan must include a warmup mode (start at 20–30/day, ramp).
- `From` must match authenticated user or a verified "Send mail as" alias; otherwise Gmail rewrites silently.
- 429s are per-user-per-minute; daily-cap 4xx/5xx has body `quotaExceeded` or `Daily user sending limit exceeded`. Use exponential backoff (1s, 2s, 4s, ..., 32s + jitter).

### B.2 SMTP RCPT-TO probing & catch-all detection

**What to actually do:**
1. **Don't probe from the sending IP.** Separate egress (or a paid API like ZeroBounce/Prospeo/MillionVerifier for at-scale runs) so a blocklisting event doesn't kill your send reputation.
2. **Always probe a random control address alongside the real one** (the catch-all probe). Standard recipe (matches prior art):
   ```
   probe1 = "random32@domain" → if 250, catch-all = true
   probe2 = "target@domain"   → if catch-all: status="risky"; if not catch-all and 250: status="valid"
   ```
3. **4xx is inconclusive.** Retry once after 60–120s (greylisting defense). Two 4xx → mark "unknown."
4. **Hard-skip Outlook/O365 and Yahoo MX**: `*.mail.protection.outlook.com`, `*.olc.protection.outlook.com`, `*.pphosted.com`, `*.mimecast.com`. They tarpit (uniform 250) and probing is useless. Effectively ~100% catch-all-by-policy.
5. **Cap probes at ~50–100 distinct domains/hour from any one IP.** Spamhaus/Barracuda flag probe-pattern traffic.

**Response-code map (corrects/extends the prior art):**
- `250–259` → accepted
- `550/551/553` → permanent reject (mailbox doesn't exist) — reliable only if domain isn't catch-all
- `552 / 5.2.2` → mailbox over quota (exists)
- `450/451/452` → temp; retry once
- `421` → service shutting down / rate-limit; back off entirely
- `556 / 5.1.10` → null MX (RFC 7505); domain refuses mail
- `554` → policy reject (probing IP likely flagged)

**DNS quirks:**
- No MX → fall back to A record (RFC 5321 §5.1) but skip in practice (low-quality signal).
- Null MX (priority 0, target `.`, RFC 7505) → mark all addresses invalid.
- MX → `localhost`/`127.0.0.1`/private IP → misconfigured, skip.

**Pattern-only bounce rate:** 15–30% (industry data) for `firstname.lastname@` and friends, vs <2% for properly-verified addresses. Workspace sender reputation can be wrecked above 2% bounce.

**Implications for our plan:**
- `smtp_probe.py` needs the response-code map above (extended from the prior art).
- `smtp_probe.py` needs to refuse-by-host: hard-skip O365/Outlook/Proofpoint/Mimecast MX → return `"catchall"` immediately so the cascade falls to `web_citation`.
- `smtp_probe.py` should implement greylist retry (1 retry after 90s, second 4xx → `"unknown"`).
- Default rate `--max-rate 3.0` from prior art is **too aggressive** for sustained runs (Spamhaus threshold ~50–100/hour ≈ 0.01–0.03/sec). Override `verifier.rate_limit` in `config/defaults.yaml` to a per-hour cap rather than a per-second cap, with burst tolerance.

### B.3 Cold-email deliverability & CAN-SPAM compliance (2025–2026)

**What to actually do:**
1. **Warmup ramp**: cap 20–30/day per inbox for the first 4–6 weeks, then 2×/day. Workspace tenants doing 50+/day on fresh domains saw >30% suspension rates in late 2025.
2. **Use a secondary sending domain** (not your brand domain). SPF + DKIM + DMARC `p=quarantine` → `p=reject` after 30 days clean. MX-Toolbox verify before first send.
3. **Always include `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`** even below the 5K/day bulk-sender threshold:
   ```
   List-Unsubscribe: <https://your-domain.com/u/abc123>, <mailto:unsubscribe@your-domain.com?subject=unsubscribe>
   List-Unsubscribe-Post: List-Unsubscribe=One-Click
   ```
   Plus: a footer text unsubscribe link AND a physical postal address (CAN-SPAM hard requirement).
4. **Pace sends with jitter**: 30–180s between sends per inbox (`throttle_seconds=45` is reasonable but should vary ±50%). Vary send times across business hours.
5. **Honor opt-outs within 24h**, not the CAN-SPAM 10 business days.

**CAN-SPAM (US):**
- Don't use deceptive headers. Identify as an ad. **Include physical postal address (street/PO Box/CMRA).** Provide opt-out working ≥30 days. Honor within 10 business days.
- Penalty: up to **$53,088 per email** (FTC inflation-adjusted Jan 2025). Per-email, not per-campaign.

**Gmail/Yahoo bulk-sender rules (RFC 8058):**
- Officially: senders of 5,000+ messages/day to consumer Gmail/Yahoo. We're below this.
- **Implement anyway.** Google escalated enforcement Nov 2025 from delays to permanent rejections. The HTTPS unsubscribe URL must accept a POST with `List-Unsubscribe=One-Click` and respond 200 within ~10s. No CAPTCHA, no login.

**GDPR (EU recipients):**
- Legal under legitimate interest for B2B if: real business purpose, address from professional context (not scraped consumer data), relevant to recipient's role, easy opt-out, disclose source on first contact or request.
- **Run a documented Legitimate Interest Assessment (LIA).**
- Penalty: €20M or 4% global revenue.
- ePrivacy Directive (Germany, others) requires prior consent even B2B. **Default-safe move: exclude DE/AT/FR for cold sends.**

**CASL (Canada):**
- Default requires express consent. B2B exemption: business address conspicuously displayed, message relevant to role.
- Penalty: up to **$10M CAD per violation.**
- Treat .ca like EU: require LinkedIn/website evidence the role is public and relevant.

**Content red flags (Gmail spam classifier signals, 2025–2026):**
- Single image or >40% image:text ratio.
- Links in first 100 chars; >2 links; link domain ≠ sending domain.
- URL shorteners (bit.ly, t.co) — major flag.
- All-caps subject lines; excessive `!!!`/`???`.
- HTML-only (no plain-text alternative) — set `multipart/alternative`.
- Reply-To ≠ From → flag.
- First-touch with link-to-schedule (Calendly) → very high spam classification; text-only first touch.

**Reputation recovery playbook (when sender starts hitting spam):**
1. Stop all sends.
2. Check Postmaster Tools (postmaster.google.com): spam rate must be <0.10%, ideally <0.05%.
3. Audit SPF/DKIM/DMARC on mxtoolbox.com.
4. Re-warm: 5–10 manual conversational sends/day for 2 weeks; replies = strongest signal.
5. Purge: anyone not engaged in 60+ days, all role accounts.
6. Wait 7–14 days; ramp 2×/day. Recovery: Good→Medium ~20min if caught early; Medium→Good 1–2 weeks; Low/Bad 30–60 days, often easier to burn the domain.
7. If suspended: wait 24–48h before appealing.

**Implications for our plan:**
- `lib/gmail.py` must emit `List-Unsubscribe` + `List-Unsubscribe-Post` headers on every send, with a real working URL.
- We need an *unsubscribe endpoint*. v1 simplification: use `mailto:unsubscribe@…?subject=unsubscribe` with a polling script (`scripts/poll_unsubscribes.py`) that reads that inbox via Gmail API and appends to `data/suppression.csv`. The HTTPS variant can be added later.
- The brief schema needs three new fields:
  - `compliance.postal_address` (required)
  - `compliance.unsubscribe_email` (defaults to derived `unsubscribe+<campaign-slug>@<from_domain>`)
  - `compliance.geography_exclude` (defaults to `["DE","AT","FR"]`)
- The send pipeline must have a **warmup mode** flag in the brief: `sending.warmup: true | false`. Warmup mode overrides `send_rate_per_day` with a ramp schedule.
- Content lints in `compose_emails.py`: warn if subject all-caps, body has >40% image ratio, body has URL shortener, etc.
- Jitter in `send_emails.py`: actual delay = `throttle_seconds * uniform(0.5, 1.5)`.

### B.4 LLM-driven web extraction patterns (2026)

**What to actually do:**
1. **Always use OpenAI Structured Outputs with `strict=true` + a Pydantic model.** Never `json_object` mode. Never raw-prompt-with-retry.
2. **Require a `source_url` field for every extracted fact (non-null in schema).** Single biggest hallucination reducer.
3. **Tiered model cascade: `gpt-4.1-mini` first, escalate to `gpt-5` only on empty / low-confidence / refusal.** 10–20× cost savings.
4. **Don't ask the LLM to "find an email" directly. Ask for: domain + role + person's name. Then construct candidate emails via pattern + RCPT-verify yourself.** LLMs hallucinate emails much more than they hallucinate person names or domains.
5. **Use Brave Search API + Firecrawl/trafilatura** for web search + content extraction. Tavily if willing to pay 2–3× for one-API simplicity. Avoid OpenAI hosted `web_search` if you need vendor independence or caching control.

**Canonical Structured Outputs pattern:**
```python
from openai import OpenAI
from pydantic import BaseModel
from typing import Optional

class CompanyEmail(BaseModel):
    domain: str
    person_full_name: Optional[str]
    job_title: Optional[str]
    source_url: str       # REQUIRED, no nulls — every fact needs grounding
    confidence: float

client = OpenAI()
resp = client.responses.parse(
    model="gpt-4.1-mini",
    input=[...],
    text_format=CompanyEmail,
    tools=[{"type":"web_search"}],
)
result = resp.output_parsed
```

Strict-mode gotchas:
- Every property in the schema must be in `required` (use `Optional[X]` for null-able).
- 16k output token limit on structured outputs (batch in groups of 10–25 for list extraction).
- Refusals: when model refuses (safety-flagged), `resp.output_parsed is None` and `resp.output[0].refusal` is set. Handle it.

**Hallucination mitigation order of impact:**
1. Required `source_url` in schema (5–10× reduction).
2. Post-validate URLs: HEAD must 200; fetch page must mention the claimed entity name.
3. Multi-source agreement: accept fact only if ≥2 distinct domains assert it.
4. Temperature 0 for extraction (not for generation).
5. Constrain to enums where possible (job titles, categories).

**Cost cascade (concrete):**
- Tier 1: `gpt-4.1-mini` / `gpt-5-nano` ≈ $0.15/1M in, $0.60/1M out. 80%+ of straightforward extractions.
- Tier 2: `gpt-5` / `gpt-5.1` ≈ $10/1M in, $30/1M out. Only on empty/low-confidence/refusal.
- Cache aggressively in SQLite keyed on `hash(model + prompt + tool_calls)`.
- Dedupe before LLM. Batch in groups of 10–25.

**Web search API comparison (2026):**
| Provider | $/1k | Latency | Index | Full content? |
|---|---|---|---|---|
| Brave | ~$3 | ~670ms | Independent 30B pages | Snippets only |
| Serper | ~$0.30–0.75 | ~1s | Google | Snippets only |
| Tavily | ~$8 | ~1s | Aggregated + extraction | Yes |
| Exa | ~$5 | ~1.2s | Embedding-based | Yes |
| OpenAI hosted | per-call | adds to model | Bing-backed | Snippets |

**Implications for our plan:**
- `lib/llm.py` builds on `openai.responses.parse(text_format=PydanticModel, tools=[{"type":"web_search"}])`. Define Pydantic schemas in `lib/csv_schema.py` (one per CSV row).
- `lib/llm.py` implements the tier-1/tier-2 cascade. Tier 1 = `gpt-4.1-mini`, tier 2 = `gpt-5`. Existing `MODEL_FALLBACKS` becomes a *startup probe* for which models are reachable, not a per-call cascade.
- SQLite cache in `data/llm_cache.sqlite` (added to repo layout; not in prior art).
- Pydantic models *require* `source_url`. Discovery model returns `email_if_known` AND `email_source_url`; if model can't ground the email, both must be null.
- Stage 1 (`source_domains.py`) uses Brave Search API for breadth + cheap LLM extraction; web_search tool reserved for harder Stage 2 lookups.

---

## Part C — Cross-cutting consequences for the build plan

1. **Suppression list is mandatory infrastructure, not a feature.** Persist forever. Check before every send. Auto-add on: explicit unsubscribe, hard bounce, soft bounce 3× in 7 days, manual reply containing "stop"/"unsubscribe"/"remove me".
2. **Two separate IPs**: sending egress (Gmail API to Workspace — no IP control needed; Google handles) vs. probing egress (your RCPT prober — Dartmouth VPN currently). Don't conflate.
3. **Per-inbox daily counter with hard stop**, persisted across restarts. Lives in `data/send_counters.json` (new file in repo layout).
4. **Schema-first LLM design**: define `Domain`, `Contact`, `EmailCandidate`, `VerificationResult`, `ComposedEmail` Pydantic models in `lib/csv_schema.py` up front. CSV reads/writes go through these.
5. **Warmup mode** is a new brief feature not in the design doc. We'll add it.
6. **Compliance fields are new brief features** not in the design doc. We'll add `postal_address`, `unsubscribe_email`, `geography_exclude`.
7. **Content lints** in `compose_emails.py` are a new feature not in the design doc — fast guards against spam-classifier red flags.
8. **`lib/rate_limit.py`** (new) — token-bucket reused for verification *and* sending throttles, ported from the prior `RateLimiter` class. Time gating supports both per-second and per-hour caps (the per-hour cap matters for SMTP probing).
9. **LLM cache (`data/llm_cache.sqlite`)** — new file, new dependency. Saves 10× on iterative runs of the same campaign.
10. **Greylist-aware probing**: the prior art doesn't handle 4xx retry. New code does (1 retry after 90s, then "unknown").

---

## Part D — Open implementation questions surfaced by research

These should be answered during the Interview step (Step 8) before plan writing:

1. **Stage 1 search provider**: OpenAI hosted `web_search` (zero plumbing, locked in to OpenAI) vs Brave Search API (cheaper, independent, requires new key/dep)? Current design assumes hosted; web research suggests Brave is better cost/quality.
2. **Warmup mode**: build into v1 or defer? Web research strongly suggests v1 — getting the account suspended in week 1 is the most likely failure mode.
3. **Unsubscribe endpoint**: mailto-only v1 (poll inbox) vs HTTPS-also v1 (need a tiny FastAPI server somewhere)? Mailto satisfies CAN-SPAM but Gmail's bulk-sender rules want one-click HTTPS.
4. **Pattern-only addresses**: prior art has a `pattern-only` confidence tier that's never written to send batches. Keep that hard rule? Or make it brief-configurable (`accept_levels: [verified-smtp, verified-web, pattern-only]`)?
5. **LLM cache**: opt-in (env var) or default-on? Cache hits cross campaigns can leak yesterday's stale info into today's run.
6. **Probing IP**: design doc assumes Dartmouth VPN. What if the user moves off campus? Plan should call out a config check: `verifiers/smtp_probe.py:assert_available()` runs on every start; failure → log clear remediation steps.
7. **Greylisting**: implement 1-retry-after-90s now, or defer? Adds latency to verification; current code skips it.
8. **Compliance — geography exclusion**: enforce at brief-load time (reject if EU recipient with `geography_exclude=[DE,AT,FR]`), at discovery time (skip company), or at send time (filter outbox)? Each has a tradeoff.
