# Purpose

Stage 4 strategy: render the message template with slot substitutions (`{{first_name}}`, `{{company}}`, etc.), canonicalize first names with the formal ambiguity rules (hyphenated, suffixed, multi-token, non-Latin → LLM call), and write per-recipient `composed.csv` rows with subject + body_plain + body_html. Filled in by section 10.

# When Claude reads this

Before invoking `scripts/compose_emails.py`, and when reviewing a sampled draft to decide whether the template needs revision before Phase A.
