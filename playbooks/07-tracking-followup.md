# Playbook: Stage 6 — Bounce polling

## Purpose

Stage 6 reads bounce notifications out of Gmail and appends hard-bounced
addresses to `data/suppression.csv` so future campaigns skip them at the
suppression hard-gate in Stage 5. It is a standalone, on-demand stage —
not part of the linear pipeline.

## When Claude reads this

- Before invoking `scripts/poll_bounces.py`, especially the first time
  (the OAuth re-auth pop-up will surprise users otherwise).
- When deciding cadence — see Strategy below.

## Strategy

Recommended cadence:

- After every Phase A test batch — bounces in test data are signal for
  whether the segment quality is good.
- Weekly during a long-running bulk send (Phase B).
- Never DURING an active send window. `send_emails.py` and
  `poll_bounces.py` both write to `data/suppression.csv` under a flock,
  but the right semantic is "no surprise additions mid-run" — run poll
  before/after a send, not during.

The script holds `data/.poll.pid` for its whole runtime; a concurrent
invocation exits 1 cleanly.

## Common failure modes

- **Re-auth pop-up on first run.** `lib/gmail.authorize` detects that
  `token.json` has `gmail.send` only and re-runs the OAuth flow with
  `[gmail.send, gmail.readonly]`. The user will see Google's consent
  screen open in a browser. The documented message "Gmail token has
  scopes [...]; required [...]. Re-authorizing." prints to stdout
  beforehand.
- **Manual mailbox deletes.** If the user empties their inbox between
  polls, the `since_message_id` anchor no longer resolves. The script
  falls back to "no anchor" (scan whole inbox).
- **Rate-limited Gmail searches on very large inboxes.** Transient
  retries handle short-term 429s; if it persists, wait and re-poll.

## Worked example

```
$ python scripts/poll_bounces.py
Gmail token has scopes ['gmail.send']; required ['gmail.send',
'gmail.readonly']. Re-authorizing.
... (browser opens, user grants gmail.readonly, returns to terminal)
poll_bounces: examined 3 bounces, added 3 new suppressions,
skipped 0 already-suppressed.

$ tail data/suppression.csv
nosuch1@nosuchcompany12345.example.org,hard_bounce,gmail-mid-abc,...
nosuch2@nosuchcompany12345.example.org,hard_bounce,gmail-mid-def,...
nosuch3@nosuchcompany12345.example.org,hard_bounce,gmail-mid-ghi,...
```
