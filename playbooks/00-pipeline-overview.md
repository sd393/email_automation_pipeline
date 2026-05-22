# Playbook: Pipeline overview

## Purpose

The outreach-bot pipeline has six stages. Five are linear (Stages 1–5);
Stage 6 is on-demand. The brief (Stage 0) is interactive and produced by
Claude Code talking to the user.

| Stage | Script                          | Reads from           | Writes              |
|------:|---------------------------------|----------------------|---------------------|
| 0     | (CLAUDE.md interview)           | user                 | brief.yaml          |
| 1     | source_domains.py               | brief.yaml           | domains.csv         |
| 2     | discover_contacts.py            | domains.csv          | contacts.csv        |
| 3     | verify_emails.py                | contacts.csv         | emails.csv          |
| 4     | compose_emails.py               | emails.csv + template| outbox.csv          |
| 5     | send_emails.py (Phase A then B) | outbox.csv           | sent.log + Gmail    |
| 6     | poll_bounces.py                 | Gmail (readonly)     | data/suppression.csv|

The single human checkpoint is at Phase A → Phase B: after the first
`send_test_count` emails ship, the script STOPS and prints a banner. Re-run
with `--confirm-test` to send the rest.

## When Claude reads this

At campaign start, after Stage 0 completes and before invoking Stage 1.
Skim once to refresh the mental model.

## Strategy

Stages are independent processes with strict file-based interfaces. Each
stage:

1. Loads `brief.yaml` via `lib.brief.load`.
2. Checks `progress/brief_hash.txt` against `sha256(brief.yaml)` — refuses
   to run if the brief changed since a prior stage.
3. Reads its input CSV (or the brief, for Stage 1).
4. Writes its output CSV row-by-row.
5. Tracks per-row state in `progress/<stage>.json` so `--resume` is safe.

Cross-cutting state (the global suppression list and master-contacts dedup)
lives in `data/master_contacts.csv` and `data/suppression.csv`, both
fcntl-locked.

## Common failure modes

- **Brief-hash mismatch** at any stage past the first → user must either
  revert `brief.yaml` or start a fresh campaign in a new folder.
- **Pre-flight missing input** → Stage N exits 2 with "Run stage N-1 first".
- **Brief validation error** → exit 3 with structured JSON on stderr.

## Examples

A clean run from setup to test batch:

```
$ python scripts/setup_campaign.py --slug 2026-05_demo
$ vim campaigns/2026-05_demo/brief.yaml
$ python scripts/source_domains.py    --campaign-dir campaigns/2026-05_demo
$ python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_demo
$ python scripts/verify_emails.py     --campaign-dir campaigns/2026-05_demo
$ python scripts/compose_emails.py    --campaign-dir campaigns/2026-05_demo
$ python scripts/send_emails.py       --campaign-dir campaigns/2026-05_demo
# (Phase A banner prints. User checks Gmail Sent folder.)
$ python scripts/send_emails.py --campaign-dir campaigns/2026-05_demo --confirm-test
$ python scripts/poll_bounces.py   # later, after bounces arrive
```
