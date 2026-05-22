# Outreach Bot — Repo Design & Build Plan

A reusable, Claude Code–driven system for running cold-outreach campaigns end to
end: define a target → source domains → find people → verify emails → compose →
send → track. The whole point is that you describe the campaign in one sentence
and the repo already knows how to run it — while keeping you informed the whole
way through without you having to babysit it.

---

## 1. The core idea: one stable engine, many disposable campaigns

The single most important design decision is splitting the repo into two layers.

**The engine** is everything that stays the same no matter who you're emailing:
the scripts, the strategy write-ups, the shared utilities, the orchestration
instructions. You build this once and rarely touch it.

**A campaign** is everything that changes per run: who you're targeting, the
filters, the email template, the sending identity, and all the data and progress
files that get produced. Each campaign lives in its own folder and never
overwrites another.

Your two example docs are essentially *one campaign* with the engine logic baked
into it. This design pulls the engine out so the next campaign is a 5-minute
setup instead of a rewrite.

The bridge between the two layers is a single file called the **brief**. The
brief is the campaign's spec sheet. You (or Claude Code, by interviewing you)
fill it out, and every script reads from it. "Just tell Claude Code what I want"
really means "Claude Code turns my sentence into a brief, then runs the engine
against it."

---

## 2. Repo structure

```
outreach-bot/
├── CLAUDE.md                      # The orchestrator. Tells Claude Code how to run a campaign.
├── README.md                      # Human-facing setup + quickstart
│
├── playbooks/                     # Reusable strategy knowledge — the "how", in plain English
│   ├── 00-pipeline-overview.md
│   ├── 01-target-definition.md    # Turning a vague target into a precise spec
│   ├── 02-domain-sourcing.md      # Strategies for finding domains in ANY segment
│   ├── 03-contact-discovery.md    # Finding high-leverage people at a domain
│   ├── 04-email-verification.md   # How verification works + which backend to use when
│   ├── 05-email-composition.md    # Cold-email principles + personalization
│   ├── 06-sending.md              # Gmail sending, throttling, the test-batch flow, compliance
│   └── 07-tracking-followup.md
│
├── scripts/                       # The stable, parameterized tools
│   ├── source_domains.py
│   ├── discover_contacts.py
│   ├── verify_emails.py
│   ├── compose_emails.py
│   ├── send_emails.py
│   └── lib/                       # shared building blocks
│       ├── brief.py               # loads + validates a campaign brief
│       ├── progress.py            # the resume/snapshot machinery (from your Phase docs)
│       ├── observability.py       # writes the live status.md + activity.log + chat milestones
│       ├── dedup.py               # within-campaign + cross-campaign dedup
│       ├── dns_check.py           # MX/A record validation
│       ├── llm.py                 # one LLM client, model fallback, cost tracking
│       ├── gmail.py               # send via your Gmail account
│       ├── csv_schema.py          # the canonical column definitions for each stage
│       └── verifiers/             # PLUGGABLE email-verification backends
│           ├── base.py            # the common interface every verifier implements
│           ├── smtp_probe.py      # your port-25 / VPN method
│           ├── web_citation.py    # "verified-web": accept if a primary source cites it
│           └── api_provider.py    # third-party verification API (optional)
│
├── config/
│   ├── defaults.yaml              # global defaults: model, rate limits, role priorities
│   ├── verifiers.yaml             # which verifier(s) to use + their settings
│   └── secrets.example.env        # template for API keys / Gmail auth (real one is .gitignored)
│
├── templates/                     # reusable email templates with {{slots}}
│   ├── ai-agent-integration.md
│   └── _example.md
│
├── campaigns/                     # ONE folder per campaign — the only thing that changes
│   └── 2026-05_medium-retailers/
│       ├── brief.md               # the campaign spec
│       ├── domains.csv            # Stage 1 output
│       ├── contacts.csv           # Stage 2 output (unverified candidates)
│       ├── emails.csv             # Stage 3 output (verified only)
│       ├── outbox.csv             # Stage 4 output (composed, ready to send)
│       ├── sent.log              # Stage 5 record
│       ├── status.md             # LIVE human-readable snapshot of the whole campaign
│       ├── activity.log          # LIVE append-only event trail with timestamps
│       └── progress/              # per-stage progress.json files for --resume
│
└── data/
    ├── master_contacts.csv        # everyone ever discovered, across all campaigns
    └── suppression.csv            # do-not-contact list (unsubscribes, bounces, opt-outs)
```

A few terms used above, defined plainly:

- **MX record / A record**: entries in a domain's DNS (the internet's address
  book) that say "this domain can receive mail" (MX) or "this domain points to a
  server" (A). No MX usually means the domain can't receive email, so it's not
  worth chasing.
- **Suppression list**: a master "never email these people" list. Anyone who
  unsubscribes, bounces, or asks to be removed goes here, and every future
  campaign checks against it. This is legally and reputationally important.

---

## 3. The brief — the interface to everything

This is the file you fill out (or Claude Code fills out by asking you questions).
Everything downstream reads it. Here's the schema, written as a fill-in-the-blank
markdown file so it doubles as documentation:

```markdown
# Campaign Brief: <slug>

## Target
- segment: "Medium-sized multi-brand retailers"
- include: curated marketplaces, hybrid retailer-brands
- exclude: pure single-brand DTC; enterprise (>$500M rev)
- geography: US + Canada
- target_domain_count: 1500

## Who to contact (leverage)
- priority_roles: [Founder, CEO, VP E-commerce, Head of Digital, CTO]
- deprioritize: [Marketing, PR, HR, generic info@]
- contacts_per_company: 3

## Message
- template: templates/ai-agent-integration.md
- value_prop: "Integrate AI shopping agents on your storefront"
- personalization: true        # generate a custom opening line per recipient
- from_name: "Smrjit"
- from_gmail: "smrjit@gmail.com"   # the account emails actually send from
- reply_to: "smrjit@gmail.com"

## Verification
- verifier: smtp_probe          # or: web_citation, api_provider, or a list (tried in order)
- accept_levels: [verified-smtp, verified-web]

## Sending
- send_test_count: 10            # send this many first, then PAUSE for your OK
- send_rate_per_day: 400         # stays safely under Gmail's daily ceiling (see §7)
- throttle_seconds: 45           # gap between sends, so it looks human-paced

## Safety
- dedup_scope: all_campaigns     # don't re-contact anyone from a prior campaign
- require_approval_after: [send_test]   # the ONLY hard stop is after the test batch

## Notes
<free text: anything Claude Code should know about this segment>
```

Why this matters: the example scripts have "retailer", "pure DTC", role
priorities, and rate limits **hardcoded in the code and prompts**. Lifting all of
that into the brief is ~80% of what turns your one-off into a general tool.

---

## 4. Live progress & observability

You shouldn't have to ask "how's it going?" or approve each micro-step. The repo
makes progress *ambient* — it streams to you and to files automatically, and only
stops to ask permission at the one moment that's irreversible (sending the bulk).

Three things happen at once, driven by `lib/observability.py`, which every script
calls as it works:

**1. A live snapshot file — `status.md`.** Continuously overwritten with a
glance-able summary of the whole campaign: which stage is running, counts so far,
percent done, last action, rough ETA, and running cost. Open it any time to see
exactly where things stand. It's "cordoned off" inside the campaign's own folder,
so each campaign's progress is isolated from every other.

Example `status.md`:
```
# medium-retailers — RUNNING (stage 2 of 5: contact discovery)

Domains sourced:   1,491 / 1,500  ✅
Contacts found:      612 companies processed (41%)
Emails verified:     1,134 verified  (3-fill=148 2-fill=...)
Cost so far:         $18.40
Last event:          2026-05-21 14:03  verified aforch@huckberry.com
ETA this stage:      ~22 min
```

**2. An append-only event trail — `activity.log`.** Every event, timestamped, in
the order it happened. This is the "what exactly happened and when" record you'd
scroll through if something looked off. (Your Phase docs' verbose log, basically,
now standard for every stage.)

**3. Milestone updates in chat, automatically.** Claude Code posts a short
progress line on a fixed cadence — every N items **or** every 2 minutes,
whichever comes first — without you prompting. This generalizes the cadence you
already wrote into both Phase docs (every 50 domains; every 20 companies). So in
the chat window you get a steady drip like:

```
[discovery] 60/1491 companies (4%) — 138 verified emails — $2.40 — 0 errors — 4m elapsed
```

The point: you can watch it live in chat, glance at `status.md` whenever, or dig
into `activity.log` if you want detail — but you're never *required* to sit there
clicking approve. The pipeline runs through stages 1–4 on its own and keeps you
posted.

---

## 5. The pipeline (generalized from your two docs)

Each stage is one script, reads the brief, writes a CSV, keeps a `progress.json`
so it can be killed and resumed, and reports live progress via §4. This is the
pattern your Phase docs already nailed — we're just generalizing the inputs and
making the reporting automatic.

### Stage 0 — Brief
Claude Code reads your one-sentence ask, runs a short structured interview to fill
any gaps (geography? company size band? who's worth emailing?), and writes
`brief.md`. Your debate/Mom Test instinct fits perfectly here — the interview is a
fixed list of sharp questions, not open-ended chit-chat.

### Stage 1 — Domain sourcing  *(= your Phase 1, generalized)*
Find ~N domains matching the segment. Strategies live in the playbook so they
apply to any vertical: curated source lists, web search per sub-category,
LLM-extraction from listicles/directories, then include/exclude filters and dedup
against the running set **and** the master + suppression lists. Streams progress
the whole time. Output: `domains.csv`.

### Stage 2 — Contact discovery  *(= Phase 2, steps 1–3)*
For each domain: DNS-validate it, then ask the LLM (with web search) for up to N
high-leverage people, each with a role and a one-line "why this person" rationale.
Role priorities come from the brief. Output: `contacts.csv` (candidates, not yet
verified).

### Stage 3 — Verification  *(= Phase 2, steps 4–6)*
Run each candidate email through the configured verifier(s). Only verified
addresses survive. Pluggable — see §6. Output: `emails.csv` (verified only).

### Stage 4 — Composition  *(new — only implied in your prompt)*
For each verified contact, render the chosen template and, if personalization is
on, have Claude generate a custom opening line from what we know about the company.
Output: `outbox.csv` (subject + body per recipient, ready to send). No separate
approval needed here — you'll see the real thing in the test batch next.

### Stage 5 — Send  *(the one place it pauses for you)*
1. Send the first **10** emails (the `send_test_count`) for real, to the first 10
   recipients, throttled.
2. **Stop and report.** Claude tells you they're out and points you at your Gmail
   Sent folder so you can confirm they look right and landed in the inbox (not
   spam). This single approval covers both "does the copy look good?" and "is
   delivery healthy?" — it replaces the old per-stage gates.
3. On your OK, send the rest, throttled, respecting the daily cap. Bounces and
   unsubscribes get written to the suppression list as they happen.

Output: `sent.log` + updated `data/suppression.csv`, with live progress the
whole way.

### Stage 6 — Track & follow-up
Pull whatever reply/bounce signal Gmail gives you, schedule one follow-up bump
(e.g. 4 days later if no reply), and produce a campaign report.

---

## 6. The pluggable verifier (the part I'd most change from your example)

Your Phase 2 leans entirely on **SMTP RCPT-TO probing over the Dartmouth VPN**.

Quick definition: when you send email, your server "talks" to the recipient's
server using a protocol called **SMTP**. Before sending, you can ask "does
`aforch@huckberry.com` exist?" using a command called **RCPT TO**. If the server
says "accepted," the address is probably real. A **catch-all** server says
"accepted" to *everything*, so it can't confirm a specific address — that's the
case your docs handle with the "verified-web" fallback.

This works, but as the foundation of a tool you'll reuse for months, it has two
weaknesses: it depends on one Dartmouth VPN IP (a single point of failure if that
IP gets blocklisted or you graduate), and ~7,500 probes from one IP sits near
monitoring thresholds.

So verification becomes an **interface** — a common shape any backend implements —
with swappable backends:

```python
class Verifier:
    def verify(self, email: str, *, citation_url: str | None) -> Result:
        # returns: accepted | catchall | rejected | unknown, + a confidence label
        ...
```

Backends:
- `smtp_probe` — your existing method. Keep it; it's free and good.
- `web_citation` — accept if a trustworthy primary source (the company's own site,
  a press release) explicitly lists the address. Your "verified-web."
- `api_provider` — a paid third-party verification service (the NeverBounce /
  ZeroBounce / Bouncer family). They probe at scale from rotating reputable IPs,
  which removes both weaknesses above. ~$0.003–0.01 per check.

The brief picks one or a *fallback chain* (`[smtp_probe, api_provider]`: free
first, pay only for leftovers) — the same deep-fallback idea you invented in
Phase 2, applied to verification.

---

## 7. Sending — from your Gmail, kept simple

Default: send straight from your Gmail account. No separate domains, no rotating
identities — that complexity is stripped out of the default path. A real
advantage of using Gmail: the sender-authentication records (the DNS entries that
prove "this mail is really from this account") are already set up by Google, so
there's nothing for you to configure.

How `gmail.py` connects: via the **Gmail API with OAuth** — "OAuth" just means you
grant the script permission once through a Google login screen, instead of pasting
a password. (An app-password + SMTP route also works as a fallback.)

**One honest flag, said once.** Gmail isn't built for high-volume cold mail, so
two limits matter:

- **A daily ceiling.** A free `@gmail.com` account caps at roughly 500 recipients
  per day; a paid Google Workspace account at roughly 2,000. The brief's
  `send_rate_per_day` should stay under your account's ceiling, and big campaigns
  just spread over several days automatically.
- **Reputation risk.** Blasting hundreds of cold emails fast can get a personal
  account throttled or, worst case, temporarily limited. The guardrails below keep
  you well clear of that, and the 10-email test batch is itself a safety check.

Guardrails baked into `send_emails.py`, all pulled from the brief:
- **Hard daily cap**, enforced — never exceeds `send_rate_per_day`.
- **Throttle** — a gap between sends (`throttle_seconds`) so it's human-paced, not
  a burst.
- **The 10-email test batch first**, then pause for your OK (also a deliverability
  check: confirm they hit the inbox, not spam).
- **A real reply-to and a one-line unsubscribe path**, honored instantly into the
  suppression list. This keeps you compliant with anti-spam law (CAN-SPAM in the
  US) and protects your account's reputation.

If a campaign ever genuinely needs thousands of sends a day, that's the moment to
graduate to a dedicated sending domain + a real email service — kept as a
documented *upgrade path* in `playbooks/06-sending.md`, not part of the default
you run day to day.

---

## 8. Orchestration — what makes "just tell it" work

`CLAUDE.md` is the brain. It encodes the standard operating procedure so the
prompt lives in the repo, not in your head. Sketch:

```markdown
# How to run an outreach campaign

When the user describes a target (e.g. "contact all medium-sized retailers"):

1. Create campaigns/<YYYY-MM>_<slug>/, copy in the brief template, and start
   status.md + activity.log.
2. Read playbooks/01-target-definition.md. Interview the user to fill the brief.
   Confirm the brief once, then proceed.
3. Run stages 1–4 (source → discover → verify → compose) WITHOUT stopping for
   approval. Post a chat milestone every ~2 minutes and keep status.md current.
   The user can watch live or check the files; don't make them click through.
4. Stage 5 — sending:
   a. Send the first <send_test_count> emails from the user's Gmail, throttled.
   b. STOP. Report that they're out; tell the user to check their Gmail Sent
      folder. Ask for an explicit go/no-go.
   c. On approval, send the rest under the daily cap + throttle, updating
      suppression on every bounce/unsubscribe.
5. Report the final summary and offer the follow-up bump.

Rules:
- Always pass --resume; never restart a stage from scratch.
- Never send beyond the 10-email test without explicit approval.
- Never exceed send_rate_per_day. If a campaign needs more, spread across days.
```

The test-batch pause is the only hard stop — and it does double duty as content
review and deliverability check, so you're not nagged at every stage.

---

## 9. Cross-campaign dedup & the master list

Your Phase docs dedup *within* a task. A reusable tool needs to dedup *across* all
tasks, or you'll eventually email the same VP three times from three campaigns and
look like a spammer.

- `data/master_contacts.csv` — every contact ever discovered. New campaigns check
  against it.
- `data/suppression.csv` — the do-not-contact list. Checked at sourcing,
  discovery, and (hard gate) before every send.

The brief's `dedup_scope` chooses whether a campaign avoids only its own dupes or
everyone you've ever touched.

---

## 10. Build plan (milestones)

You already have ~70% of stages 1–3 written in your two example scripts. Most of
the early work is *refactoring* (pulling hardcoded assumptions into the brief),
not greenfield building.

### Milestone 0 — Skeleton + plumbing + observability  *(~1 day)*
- Create the folder structure and empty playbooks.
- Build `lib/`: `brief.py`, `progress.py` (lift from your docs), **`observability.py`**
  (the status.md + activity.log + chat-milestone helper from §4), `dedup.py`,
  `dns_check.py`, `llm.py` (your gpt-5.2 → gpt-5 → gpt-4.1 fallback), `csv_schema.py`,
  config loader.
- Write `CLAUDE.md` v1 and the brief template.
- **Acceptance test:** Claude Code creates a campaign, fills a brief by interview,
  and runs a no-op stage end to end — and you can watch `status.md` update live.

### Milestone 1 — Domain sourcing  *(~½ day)*
- Port `scrape-retailers.py` → `source_domains.py`. Replace the retailer-specific
  prompt + `is_pure_dtc` filter with brief-driven include/exclude criteria. Wire
  in the observability calls.
- Write `playbooks/02-domain-sourcing.md`.
- **Acceptance test:** two different segments both produce sensible `domains.csv`
  from the same script, with live progress.

### Milestone 2 — Contacts + pluggable verification  *(~1 day)*
- Port `find-emails-bulk.py` discovery into `discover_contacts.py`.
- Build the `verifiers/` interface + `smtp_probe` and `web_citation`. Stub
  `api_provider` behind a flag. Keep your deep-fallback logic, generalized.
- **Acceptance test:** `emails.csv` with verified-only rows; swapping the verifier
  in the brief changes behavior without touching the script.

### Milestone 3 — Compose + Gmail send + test-batch flow  *(~1 day)*
- `compose_emails.py` with template rendering + per-recipient personalization.
- `lib/gmail.py` (OAuth) + `send_emails.py` with the 10-email test batch, the
  approval pause, daily cap, throttle, and suppression updates.
- `playbooks/05-email-composition.md` and `playbooks/06-sending.md`.
- **Acceptance test:** a real run sends 10 to live recipients, pauses, and on your
  OK sends a few more — all logged, with a test unsubscribe writing to suppression.

### Milestone 4 — Tracking, follow-ups, polish  *(~½–1 day)*
- Reply/bounce tracking, one follow-up bump, campaign report.
- Tighten the playbooks and `CLAUDE.md` SOP based on the first real run.

---

## 11. What to port directly vs. rebuild

| From your docs | Status in the new repo |
|---|---|
| `progress.json` + `--resume` machinery | **Port as-is.** Spine of every stage. |
| Per-stage milestone cadence (every 50 / every 20) | **Port + generalize** into `observability.py`. |
| LLM client with model fallback + cost tracking | **Port** into `lib/llm.py`. |
| Domain dedup logic | **Port + extend** to cross-campaign + suppression. |
| `scrape-retailers.py` extraction logic | **Port**, swap hardcoded filters for brief. |
| `find-emails-bulk.py` discovery + SMTP probe | **Port**, wrap probe behind verifier interface. |
| Deep-fallback cost optimization | **Port the pattern**, apply to discovery + verification. |
| Hardcoded "retailer / pure DTC / roles / rate limits" | **Delete from code → move to brief.** |
| Live status.md / activity.log files | **New build** (Milestone 0). |
| Composition, Gmail send, test-batch flow, suppression | **New build** (Milestone 3). |

---

## 12. Open decisions for you

1. **Verification default.** Keep SMTP-via-VPN as the default and add the paid API
   only as a fallback? Or move the default to a paid API now to kill the
   single-point-of-failure risk? (~$5–15 to verify a 1,500-domain campaign via API.)
2. **Test batch destination.** Default is the first 10 *real* recipients (so you
   see true delivery). Want the option to instead send those 10 to your own inbox
   as a pure dry run before any real recipient gets touched?
3. **Your Gmail type.** Free `@gmail.com` (~500/day) or paid Workspace (~2,000/day)?
   This just sets the default daily cap; everything else is identical.
4. **Templates.** One template per campaign, or a small library you pick from per
   brief? The library is barely more work and pays off fast.
