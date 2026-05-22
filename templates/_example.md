# Message template guide

Message templates live in `templates/*.md` and are referenced by `message.template` in the brief.

## Slot syntax

Templates use `{{slot}}` placeholders. Slots supported in v1:

- `{{first_name}}` — recipient's first name (LLM-canonicalized if ambiguous; see section 10).
- `{{name}}` — recipient's full name as discovered.
- `{{company}}` — company / domain display name.
- `{{role}}` — recipient's role / title.
- `{{value_prop}}` — `message.value_prop` from the brief.
- `{{from_name}}` — `message.from_name` from the brief.

## Subject line

The first non-blank line MAY begin with `Subject: ...`. If so, that line becomes the subject and is stripped from the body. Otherwise the first line of the rendered body becomes the subject as-is.

## Plain text + HTML

The body is rendered to both `body_plain` (verbatim, with slot substitutions) and `body_html` (paragraphs wrapped in `<p>` tags). Gmail sends a multipart/alternative message containing both.

## Example

```
Subject: Quick question, {{first_name}}

Hi {{first_name}},

Saw your work at {{company}}. {{value_prop}}.

Worth a 15-minute chat next week?

— {{from_name}}
```
