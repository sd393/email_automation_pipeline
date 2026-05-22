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

## Manual 5-minute walkthrough

If you want to drive the pipeline yourself instead of through Claude Code:

```sh
# 0. Create the campaign folder.
python scripts/setup_campaign.py --slug 2026-05_demo
vim campaigns/2026-05_demo/brief.yaml   # fill out target, who_to_contact, message, etc.

# 1-4. Run pre-send stages.
python scripts/source_domains.py    --campaign-dir campaigns/2026-05_demo
python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_demo
python scripts/verify_emails.py     --campaign-dir campaigns/2026-05_demo
python scripts/compose_emails.py    --campaign-dir campaigns/2026-05_demo

# 5. Phase A test batch. Script PAUSES after `send_test_count` real sends
# and prints a banner asking you to verify Gmail Sent + inbox placement.
python scripts/send_emails.py --campaign-dir campaigns/2026-05_demo

# After verifying:
python scripts/send_emails.py --campaign-dir campaigns/2026-05_demo --confirm-test

# 6. After a few hours, poll for bounces:
python scripts/poll_bounces.py
```

At any point, see live state via:
```sh
python scripts/status.py --campaign-dir campaigns/2026-05_demo
```
Pass `--json` for machine-consumable output.

## Troubleshooting

- **Port 25 blocked** during Stage 3 → `smtp_probe.assert_available()`
  fails with a remediation pointing at three escape hatches: connect to
  the VPN, drop SMTP from the brief's verifier chain, or enable the
  `api_provider` verifier.
- **OAuth re-auth pop-up on first `poll_bounces.py`** → expected. The
  send-scope token from Stage 5 doesn't include `gmail.readonly`. Grant
  consent once; subsequent polls don't re-prompt.
- **`Brief changed since previous stage`** → revert `brief.yaml` to the
  hash recorded in `progress/brief_hash.txt`, or start a fresh campaign
  in a new folder.
- **Brief validation error (exit 3)** → parse the JSON line on stderr to
  see the offending field. Fix it in `brief.yaml`, re-run.
- **Gmail daily-cap rollover** → clean exit 0 with "Daily cap reached".
  Re-invoke with `--resume --confirm-test` the next day.

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
