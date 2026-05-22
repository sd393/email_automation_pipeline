# Playbook: Stage 4 — Email composition

## Purpose

Stage 4 reads `emails.csv` (verified-only addresses from Stage 3) and a
markdown template, renders one per-recipient `OutboxRow` for each, and writes
them to `outbox.csv`. Output: one `OutboxRow` per row in `emails.csv`,
columns `to_email, to_name, subject, body_html, body_plain, first_name_used`.

## When Claude reads this

- Before invoking `scripts/compose_emails.py`.
- When sampling a draft to decide whether the template needs revision before
  Phase A.

## Template authoring

Templates live as markdown files. The path is `brief.message.template`. The
substituter supports exactly these slots: `{{first_name}}`, `{{name}}`,
`{{company}}`, `{{role}}`, `{{value_prop}}`, `{{from_name}}`. Whitespace
inside the braces is allowed.

Subject convention: if the first non-blank line starts with `Subject:`
(case-insensitive), the remainder of that line becomes the subject and the
body picks up after one blank line. Otherwise the first line of the
rendered text becomes the subject and the rest becomes the body.

Body markup: markdown is plain text in v1. `body_plain` is verbatim;
`body_html` is paragraphs (split on blank lines), HTML-escaped, wrapped in
`<p>...</p>`. No bold/italic/link conversion.

## First-name philosophy

The naive split (`name.split()[0]` after stripping titles like Dr./Mr./Mrs./Prof.) handles
~95% of names: "Jane Doe" → "Jane", "Dr. Robert Smith" → "Robert".

When the deterministic ambiguity rules trigger (hyphen in first token,
non-Latin codepoint, suffix `Jr.`/`Sr.`/`II`/…, three+ short tokens, or
not-a-name first token), we call the LLM with strict-mode
`FirstNameResult` at temperature 0. The result is cached in
`progress/first_name_cache.json` so duplicates within or across runs
cost zero.

`brief.message.personalize_first_name=false` skips the LLM unconditionally
and uses naive split for every row.

## Lints (warnings, never blocks)

- Subject is all caps → likely spam-flag bait.
- Body contains a URL shortener (`bit.ly`, `t.co`, `tinyurl.com`,
  `bit.do`) → providers downrank these.
- Body has no paragraph breaks → likely a one-blob email.
- Body is over 500 words → readers won't scroll.

Each lint that fires writes a `WARN` line to `activity.log` with the
recipient address. The `OutboxRow` is still written.

## Common failure modes

- **Missing template file.** Caught at brief-load time — the brief schema
  validates `message.template` exists on disk. Result: exit 3, structured
  JSON on stderr naming the missing path.
- **Brief-hash mismatch.** Mutate `brief.yaml` between stages → exit 2
  with the documented remediation. Revert or start fresh.
- **Unknown slot in template.** Stage exits 2 with the offending slot name.
  Fix the template and re-run.
- **Missing `emails.csv`.** Run Stage 3 first.
