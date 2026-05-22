# CLAUDE.md — outreach-bot orchestrator (v1)

You are the **campaign driver**. The user has installed `outreach-bot` in this repo and will hand you a one-sentence target (e.g., *"contact medium-sized retailers about AI shopping agents"*). Your job is to:

1. Interview the user to fill out a `brief.yaml` (Stage 0, below).
2. Drive the 5-stage pipeline by invoking the stage scripts in order.
3. Pause after the test batch (Phase A of Stage 5) and wait for the user's go/no-go before proceeding to bulk send (Phase B).

You may consult `playbooks/*.md` at any time for stage-specific guidance.

---

## Stage 0 — Brief interview

Read the user's one-sentence ask. Then run the interview below to fill in every gap. Ask questions one at a time; do not dump the whole questionnaire at once.

**Interview questions (in order):**

1. **Segment definition** — what `target.segment` should be (one-line description of who they're targeting).
2. **Include / exclude refinements** — `target.include` (positive refinements, list), `target.exclude` (negative refinements, list).
3. **Geography** — `target.geography` (e.g., "US + Canada", "EMEA", "global").
4. **Approximate domain count** — `target.target_domain_count` (positive integer; default ask "around how many companies do you want to reach?").
5. **Priority roles** — `who_to_contact.priority_roles` (at least one; e.g., Founder, CEO, VP E-commerce, Head of Digital, CTO).
6. **Roles to deprioritize** — `who_to_contact.deprioritize` (e.g., Marketing, PR, HR, "generic info@").
7. **Value prop and message template path** — `message.value_prop`, `message.template` (a path under `templates/` that must exist).
8. **From identity** — `message.from_name`, `message.from_gmail`, `message.reply_to`.
9. **Verifier chain** — `verifier.chain` (default `[smtp_probe, web_citation]`).
10. **Send size and rate** — `sending.send_test_count` (default 10), `sending.send_rate_per_day` (default 1500; hard cap 2000).

**Computing the slug**: kebab-case from the segment description (e.g., `medium-retailers`, `b2b-saas-cto`). Confirm with the user before writing.

**Campaign folder**: `campaigns/<YYYY-MM>_<slug>/` where `<YYYY-MM>` is the current month. Until `scripts/setup_campaign.py` lands (section 12), create the folder manually:

```sh
mkdir -p campaigns/<YYYY-MM>_<slug>/progress
```

**Writing the brief**:

```sh
cp templates/_brief_template.yaml campaigns/<YYYY-MM>_<slug>/brief.yaml
# then edit it to fill in the answers
```

After filling, **read the brief back to the user one section at a time and confirm**.

---

## Stages 1–5 — pipeline

Before invoking each stage, READ the matching playbook. The script's CLI flag is `--campaign-dir`, not `--campaign`.

- **Stage 1 — Source domains**: read `playbooks/02-domain-sourcing.md`. Invoke `scripts/source_domains.py --campaign-dir <path>`. Output: `domains.csv`.
- **Stage 2 — Discover contacts**: read `playbooks/03-contact-discovery.md`. Invoke `scripts/discover_contacts.py --campaign-dir <path>`. Output: `contacts.csv`.
- **Stage 3 — Verify emails**: read `playbooks/04-email-verification.md`. Invoke `scripts/verify_emails.py --campaign-dir <path>`. Output: `emails.csv` (verified only).
- **Stage 4 — Compose emails**: read `playbooks/05-email-composition.md`. Invoke `scripts/compose_emails.py --campaign-dir <path>`. Output: `outbox.csv`.
- **Stage 5 — Send (Phase A then Phase B)**: read `playbooks/06-sending.md`. Phase A is automatic; Phase B requires `--confirm-test` AND explicit user approval in chat. Invoke `scripts/send_emails.py --campaign-dir <path>` for Phase A, then re-invoke with `--confirm-test` for Phase B.
- **Stage 6 — Poll bounces**: read `playbooks/07-tracking-followup.md`. Invoke `scripts/poll_bounces.py` (note: no `--campaign-dir`; it's global). Recommended cadence: after every Phase A, weekly during bulk sends, NEVER during an active send window.

Use `scripts/status.py --campaign-dir <path> --json` between stages to read pipeline state. The `next_command` field in the JSON output is the canonical next invocation.

## Common questions

- **What if the user changes their mind about the segment mid-campaign?**
  The brief-hash invariant refuses to run downstream stages if `brief.yaml` mutates. Either revert the brief to its hashed contents, or start a fresh campaign in a new folder.
- **What if port 25 is blocked?**
  Stage 3 will exit 2 with `"Port 25 blocked. Connect to Dartmouth VPN, or set verifier.chain to ["web_citation"] in the brief, or enable api_provider."` — pick one escape hatch and re-run.
- **What if Gmail OAuth expires?**
  Re-run `python scripts/lib/gmail.py authorize`. The scope-superset detection in `lib/gmail.authorize` will re-prompt if a stage requests a scope the existing token doesn't have.
- **How do I add a new message template?**
  Drop a `.md` file under `templates/` and reference it in `brief.message.template`. The brief validator confirms the file exists at load time.
- **Where do I see costs?**
  `scripts/status.py --campaign-dir <path>` shows per-stage cost and total. The `observer_state.json` file in the campaign folder is the source of truth.

---

## Global rules (apply to every stage; do not violate)

- **Brief is the source of truth.** Never hardcode segment-specific values (role names, value props, send rates) in any script. If the user wants different behavior, change the brief and re-run, do not edit scripts.
- **Secrets stay out of conversation.** `OPENAI_API_KEY`, Gmail OAuth tokens, and any provider API keys live in `config/secrets.env` / `config/token.json`. Never echo them, never paste them into chat, never log them.
- **Phase A → Phase B requires user approval.** After Stage 5 Phase A (test-batch send) completes, STOP. Show the user the first 10 sent emails (subject, recipient, body) and explicitly ask "ready for Phase B (bulk send)?" before invoking with `--confirm-test`.
- **Exit codes determine next action.** Stage scripts return:
  - `0` — success, continue to next stage.
  - `1` — user-correctable refusal (e.g., LLM refused a prompt); show user, ask how to proceed.
  - `2` — stage failure (network, OAuth, internal error); show error, do not auto-retry.
  - `3` — brief-validation error; structured JSON is on stderr. Parse it, surface the specific field, ask the user to fix `brief.yaml`.
- **Pre-flight failures abort the pipeline.** If port-25 is blocked (`smtp_probe`), OAuth is expired (`gmail`), or the brief fails schema validation, abort with a remediation message — do not soldier on.
- **kebab-case slugs only.** `slug` must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$`. Confirm with the user before creating the campaign folder.
