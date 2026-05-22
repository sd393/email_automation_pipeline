# outreach-bot

A Claude Code-driven cold-outreach tool for sending personalized cold emails at scale. Each campaign is parameterized by a `brief.yaml`: you give Claude a one-sentence target ("contact medium-sized retailers about AI shopping agents") and it interviews you, writes the brief, and then drives a 5-stage pipeline (source domains → discover contacts → verify emails → compose → send) end to end.

## Prerequisites

- **Python 3.12+**
- **`uv`** installed locally — see https://docs.astral.sh/uv/getting-started/installation/
- **OpenAI API key** with access to `gpt-4.1-mini` and `gpt-5`-family models
- **Google Workspace Gmail account** (consumer `@gmail.com` is rate-limited too aggressively for cold outreach; use a paid Workspace domain so you get the 2,000/day cap)

## Setup

```sh
uv sync
cp config/secrets.example.env config/secrets.env
# edit config/secrets.env to fill in OPENAI_API_KEY and GOOGLE_OAUTH_CLIENT_SECRET_PATH
python scripts/lib/gmail.py authorize    # one-time OAuth, opens browser (implemented in section 04)
```

The OAuth `authorize` flow stores a refresh token at `config/token.json`. Both `secrets.env` and `token.json` are `.gitignore`d — never commit them.

## Running a campaign

Open Claude Code in this repo and tell it what you want to do. Example:

> contact medium-sized retailers about AI shopping agents

Claude will:

1. **Interview you** (Stage 0) — segment, geography, priority roles, value prop, message template, daily send rate, etc.
2. Write `campaigns/<YYYY-MM>_<slug>/brief.yaml` and confirm it with you.
3. Drive the pipeline: source domains → discover contacts → verify → compose → send Phase A (test batch).
4. **Pause after the test batch** so you can read the first 10 emails before Phase B continues to the full list.

## Layout

This repo has two layers:

- **Engine** (stable, version-controlled): `CLAUDE.md`, `playbooks/`, `scripts/`, `scripts/lib/`, `config/`, `templates/`, `tests/`.
- **Campaigns** (disposable, one folder per run, `.gitignore`d): `campaigns/<YYYY-MM>_<slug>/`.

The interface between the two layers is `brief.yaml`. Nothing in the engine layer hardcodes segment-specific values — change behavior by editing the brief, not the code.

## Out-of-scope for v1

These are intentionally NOT implemented to keep v1 shippable. Don't expect them.

- No automatic follow-up / bump emails.
- No reply detection (Stage 6 is bounce-tracking only).
- No Gmail warmup / auto-ramp send mode.
- No LLM response cache.
- No Brave search backend (OpenAI hosted `web_search` only).
- No `List-Unsubscribe` headers or CAN-SPAM footers.
- No pattern-only email tier (verified-or-skip).
- No geographic filtering of discovered domains.
- No campaign report / analytics dashboard.

## Security

`config/secrets.env` and `config/token.json` are `.gitignore`d. Do not commit them. The `.gitignore` is a backstop — keep secrets out of source code, never paste them into chat history, and rotate them if they leak.
