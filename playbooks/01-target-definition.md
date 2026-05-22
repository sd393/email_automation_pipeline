# Playbook: Stage 0 — Target definition (brief interview)

## Purpose

Stage 0 is the interview Claude Code runs to convert a one-sentence user
ask into a validated `brief.yaml`. The questions are listed in `CLAUDE.md`;
this playbook covers HOW to ask them, what defaults to suggest, and how to
disambiguate vague answers.

## When Claude reads this

When filling the `target.*`, `who_to_contact.*`, and `message.*` sections
during the interview — especially when the user's first response is too
vague to validate against the brief schema.

## Strategy

Ask one question at a time. Don't dump the whole questionnaire. For each
question:

1. State what you need and why (~one sentence each).
2. Suggest a default when sensible (e.g., `contacts_per_company: 3`).
3. If the answer is ambiguous, ask exactly one follow-up.

### Disambiguation patterns

- **"all retailers"** → ask for revenue band ("small <$50M", "medium
  $50M–$500M", "enterprise >$500M") and geography.
- **"founders"** → ask whether to include cofounders, and whether
  "founder + CEO" is one person or two on the contact cap.
- **"big tech"** → push back. "Big tech" is too broad for a value prop
  to land. Ask for the specific vertical or use case.
- **"global"** → ask for the top-3 regions; an outreach campaign rarely
  succeeds across every geography simultaneously.

### Schema gotchas

- `slug` must be kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`). Compose it from
  the segment description before confirming. The campaign FOLDER name
  may include underscores and a YYYY-MM prefix (e.g.,
  `2026-05_medium-retailers`), but the brief's `slug` field itself stays
  kebab-case.
- `send_rate_per_day` caps at 2000 (Workspace safety). Reject any larger
  value at interview time.
- `contacts_per_company` caps at 12; 1–3 is the usual range.
- `message.template` must exist on disk at brief-load time. Confirm the
  file is present before writing `brief.yaml`.

## Common failure modes

- User changes their mind mid-interview about the segment. Start the
  interview over — don't try to retro-edit answered fields.
- User gives a value prop that's longer than two sentences. Push for a
  one-sentence form; the LLM prompts truncate long ones anyway.
- User wants to target multiple disjoint segments. Recommend one brief
  per segment; don't try to encode an OR.

## Examples

User: "I want to email medium retailers about AI shopping agents."

Claude (Stage 0):
- "Got it. To define the segment precisely: are we focusing on multi-brand
  retailers, marketplaces, or both?"
- "Geographically — US, US + Canada, or broader?"
- "Roughly how many companies do you want to reach? (we'll aim for that
  many domains in Stage 1; usual range is 200–2,000.)"
- "Priority roles to contact — Founder, CEO, VP of E-commerce, Head of
  Digital, CTO? In what order?"
- ... (continues per `CLAUDE.md` Stage 0 questionnaire)
