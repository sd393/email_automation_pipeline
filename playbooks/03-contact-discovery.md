# Playbook: Stage 2 — Contact discovery

## Purpose

Stage 2 reads `domains.csv` (from Stage 1) and, for each domain, calls the LLM
with hosted `web_search` to find up to `who_to_contact.contacts_per_company`
high-leverage people. Output: `campaigns/<slug>/contacts.csv`, one row per
candidate person.

## When Claude reads this

- Before invoking `scripts/discover_contacts.py` for the first time on a
  campaign.
- When a `--resume` run reports a non-empty `worker_exc` set in progress.
- When the failure-budget halt triggers — diagnose root cause from
  `activity.log` and decide whether to fix and re-run, or accept the
  partial result.

## Strategy

One LLM call per domain (cost headroom is fine because contacts/company is
small and the prompt is short; simpler retry semantics than fan-out).
Workers run in a `ThreadPoolExecutor` (default 5); the main thread is the
sole writer of `contacts.csv` and the sole caller of `progress.mark()`.

Every person the LLM returns must be groundable in web search results. The
prompt explicitly forbids inventing emails — leaving `email_if_known=null`
is acceptable, but a fabricated email is not. The verifier stage (Stage 3)
hard-skips rows with null `email_if_known` (the v1 design dropped the
"pattern-only" tier), so the discovery stage stays honest.

If a domain's website lives at a different domain than the brief
specified, the LLM is told to set `corrected_domain`; the script uses it
for every `ContactRow` written from that domain.

## Failure modes

- **Brief-hash mismatch** — `activity.log` will not exist yet because the
  pre-flight halts before the observer starts. stderr says "Brief changed
  since previous stage". Action: revert `brief.yaml` to the version from
  the previous stage, or start a fresh campaign folder.
- **OpenAI auth error (401/403)** — stage halts; `obs.finish(FAILED)`
  is called and exit code is 2. Action: re-set `OPENAI_API_KEY` in
  `config/secrets.env` and re-run with `--resume`.
- **Failure-budget halt (>20% with n>20)** — message names the failure
  rate and counts. Usually transient (OpenAI quota hit, network blip).
  Re-run with `--resume`; worker_exc entries are retriable so the script
  will pick them up.
- **`dns_fail` cluster** — many domains are bouncing the MX check. This
  almost always means Stage 1's filter was too permissive. Inspect
  `domains.csv` for the failing rows and tighten `target.exclude`, then
  start a fresh campaign.

## Worked example: --resume after a kill

```
$ python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_retailers --workers 5
[discover] milestone: domains_done=20 contacts_found=43 total=200 cost=$1.40
... (Ctrl-C around domain 50)

$ python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_retailers --resume
[discover] milestone: domains_done=70 contacts_found=151 total=200 cost=$3.10
... (continues from where it stopped)
```

The script reads `progress/discover_contacts.json`, skips any domain with a
terminal status (`ok`, `no_people`, `dns_fail`, `discovery_fail`), and
retries any `worker_exc` rows.

## Out of v1 scope

- No pattern-only email tier — Stage 3 will hard-skip null
  `email_if_known`.
- No alternative search backends (Brave/Tavily/Serper).
- No reply detection, no follow-up bump, no LLM cache.
