# Playbook: Stage 5 — Sending

## Purpose

Stage 5 reads `outbox.csv` (rendered emails from Stage 4) and sends them
via Gmail. It runs in two phases:

* **Phase A** — sends the first `brief.sending.send_test_count` real
  recipients (default 10), then STOPS and writes a banner asking the user
  to verify Gmail Sent + inbox placement.
* **Phase B** — runs only when re-invoked with `--confirm-test`. Sends the
  remainder until `outbox.csv` is exhausted or the per-day cap
  (`brief.sending.send_rate_per_day`) is hit for `brief.message.from_gmail`.

Output: `campaigns/<slug>/sent.log` (one `SentLogRow` per attempt) and
appends to `data/master_contacts.csv` on every successful send.

## When Claude reads this

- Before invoking `scripts/send_emails.py` for Phase A.
- At the Phase A → Phase B approval gate, BEFORE invoking with
  `--confirm-test`. Stop and ask the user "ready for Phase B?".

## Test-batch philosophy

The 10-email test batch exists because Gmail anti-spam is unpredictable.
The user is checking three things:

1. **Rendering.** Does the body look right in the recipient's client?
   First names correct, no leftover `{{slot}}` placeholders, links
   clickable.
2. **Placement.** Did the email land in inbox or spam? If in spam, STOP.
   Do not invoke with `--confirm-test`. Diagnose first.
3. **Reply-to and Sent folder.** Confirm the right account sent it, and
   replies will route to `reply_to`.

If anything looks wrong, the campaign is recoverable: delete `outbox.csv`
and re-run Stage 4 with template fixes, then re-run Phase A.

## Throttle rationale

Each send is followed by `throttle_seconds * uniform(0.5, 1.5)` seconds of
sleep. The base spaces sends apart so Gmail's anti-burst heuristics don't
trigger; the jitter de-correlates the cadence so it doesn't look
machine-perfect.

## Daily-cap-rollover

When `send_rate_per_day` is hit for the current `from_gmail`, the script
prints `"Daily cap reached for <from_gmail>. Re-run tomorrow to continue."`
and exits 0. The pessimistic counter — incremented *before* the API call —
ensures we never over-send across kill/restart, at the cost of occasionally
over-throttling by one slot when a process is killed mid-send.

The next day, the user (or a cron job) re-invokes with `--resume` and
`--confirm-test`. Counter state lives in `data/send_counters.json`
(per-day, per-from_gmail; pruned at 14 days).

## Common failure modes

- **`QuotaExceeded` repeatedly.** Token may be stale, or Workspace tenant
  may be flagged. Pause 24h, send a tiny manual test from the same account,
  then resume.
- **`from rewritten` WARN.** Gmail rewrote the `From:` header — usually
  because "Send mail as" was not configured for the alias. Not fatal.
  Mention to the user.
- **Lockfile collision** (`data/.send.pid is already held`). Another
  invocation is running. Wait or kill it.
- **Suppression hits in Phase A.** Recipients in `data/suppression.csv`
  are skipped (counted as part of the 10). Indicates the brief might be
  using stale dedup scope; not blocking.
