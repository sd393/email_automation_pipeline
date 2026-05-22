I now have enough context to write a comprehensive section. Let me generate the section content.

# section-01-skeleton-and-config

## Purpose

Stand up the repository skeleton: directory layout, packaging metadata, configuration files, brief template, playbook stubs, and the `CLAUDE.md` orchestrator. **No Python logic** is written in this section (no library code, no scripts) — that is section 02 onward. The output of this section is a clone-able repo that `uv sync`'s cleanly and exposes a coherent layout for sections 02–12 to fill in.

After this section completes:
- A fresh user can clone the repo, run `uv sync`, copy `config/secrets.example.env` to `config/secrets.env`, and have a working Python 3.12 venv with all v1 dependencies installed.
- Every directory referenced by later sections exists (even if empty / stub-only).
- The brief template is a valid YAML file that conforms to the schema later parsed by `lib/brief.py` (section 02).
- `CLAUDE.md` describes the Stage 0 interview at minimum, with stub references to Stages 1–5 (filled in later sections).

This section has **no upstream dependencies**. It unblocks sections 02 and 03.

---

## Context — what the project is

A Python CLI tool driven by Claude Code that runs cold-outreach campaigns end to end. Two-layer architecture:

- **Engine layer** (stable, this section creates): `CLAUDE.md`, `playbooks/`, `scripts/`, `scripts/lib/`, `config/`, `templates/`, `data/`, `tests/`.
- **Campaign layer** (disposable, one folder per run): `campaigns/<YYYY-MM>_<slug>/`.

The interface between layers is `brief.yaml`. **Nothing in the engine layer is allowed to hardcode segment-specific values** (no role names, no value props, no rate limits). All such values come from a loaded brief.

Tech stack (locked):
- Python 3.12 (uv-managed venv).
- OpenAI Python SDK ≥ 1.50 (Structured Outputs via `responses.parse`, hosted `web_search` tool).
- Pydantic v2 for all schemas.
- `google-api-python-client` + `google-auth-oauthlib` for Gmail.
- `dnspython` for MX lookups.
- `pyyaml` for brief loading.
- `pytest` + `aiosmtpd` for tests.
- Pure-Python stdlib otherwise. No web framework, no database. CSVs are the persistence layer.

---

## Files to Create

All paths are absolute under `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/`.

### Top-level packaging and meta

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/pyproject.toml`

Single source of truth for Python deps. Use `uv` as the package manager. Required content:

- `[project]` block with name `outreach-bot`, version `0.1.0`, `requires-python = ">=3.12"`.
- `dependencies` (pin minimums where stated):
  - `openai>=1.50`
  - `pydantic>=2.6`
  - `pyyaml>=6.0`
  - `dnspython>=2.6`
  - `google-api-python-client>=2.130`
  - `google-auth-oauthlib>=1.2`
  - `google-auth>=2.29`
  - `httpx>=0.27` (used by `web_citation` verifier in section 08)
- `[project.optional-dependencies]` with a `dev` extra containing `pytest>=8`, `pytest-mock>=3.12`, `aiosmtpd>=1.4`, `httpx-mock` or `respx` (whichever the implementer prefers; document the choice in this file as a comment).
- `[project.scripts]` block declaring `console_scripts` placeholders for every stage script. These are stubs until later sections create the scripts; the entry points themselves are fine to declare now:
  - `outreach-source-domains = scripts.source_domains:main`
  - `outreach-discover-contacts = scripts.discover_contacts:main`
  - `outreach-verify-emails = scripts.verify_emails:main`
  - `outreach-compose-emails = scripts.compose_emails:main`
  - `outreach-send-emails = scripts.send_emails:main`
  - `outreach-poll-bounces = scripts.poll_bounces:main`
  - `outreach-status = scripts.status:main`
  - `outreach-run-pipeline = scripts.run_pipeline:main`
- `[tool.pytest.ini_options]` block with `testpaths = ["tests"]` and `addopts = "-ra -q"`.
- `[build-system]` block declaring `requires = ["hatchling"]` and `build-backend = "hatchling.build"` (or whichever build backend the implementer prefers; hatchling is the simplest).

Note: the `console_scripts` entries reference modules that don't exist yet. This is fine — `uv sync` only validates that the package builds, not that the entry-point modules are importable. The entry points become real as later sections create the script files.

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/.gitignore`

Required exclusions (each on its own line):

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.pytest_cache/

# Secrets and tokens — never commit
config/secrets.env
config/token.json

# Runtime data — never commit
data/
campaigns/

# OS / IDE
.DS_Store
.idea/
.vscode/
```

The `config/secrets.env` and `config/token.json` exclusions are security-critical (per the user's global CLAUDE.md: "Never put API keys, tokens, or secrets in client-side code"). The `data/` and `campaigns/` exclusions keep runtime state out of git.

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/README.md`

The README v1 (a longer v2 lands in section 12). Required sections:

1. **What it is** — one paragraph: a Claude Code-driven cold-outreach tool for sending personalized cold emails at scale, parameterized per campaign by `brief.yaml`.
2. **Prerequisites** — Python 3.12+, an OpenAI API key with access to `gpt-4.1-mini` and `gpt-5`-family models, a Google Workspace Gmail account (consumer Gmail is rate-limited too aggressively for cold outreach), `uv` installed locally.
3. **Setup**:
   ```
   uv sync
   cp config/secrets.example.env config/secrets.env
   # edit config/secrets.env to fill in OPENAI_API_KEY etc.
   python scripts/lib/gmail.py authorize    # one-time OAuth, opens browser
   ```
   Note: `scripts/lib/gmail.py` is implemented in section 04. The README mentions it now so the order-of-operations is documented; section 04 will make the command real.
4. **Running a campaign** — "Open Claude Code in this repo and tell it what you want to do. Example: `contact medium-sized retailers about AI shopping agents`. Claude will interview you, write `brief.yaml`, and drive the pipeline." Note that Phase A (test batch) will pause for the user's go/no-go.
5. **Layout** — one-paragraph summary pointing the reader at the engine vs campaign split.
6. **Out-of-scope for v1** — explicitly call out the deferred features so users don't expect them: no automatic follow-up/reply detection, no warmup, no LLM cache, no Brave search, no `List-Unsubscribe` headers, no pattern-only emails, no geo filtering.
7. **Security note** — never commit `config/secrets.env` or `config/token.json` (the `.gitignore` already excludes them, but say it explicitly).

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/CLAUDE.md`

The orchestrator that Claude Code reads when invoked in this repo. **Section 12 writes v2.** This section writes v1, which contains:

1. **Identity / role** — "You are the campaign driver. The user has installed `outreach-bot` and will give you a one-sentence target. Your job is to interview them, write `brief.yaml`, and then drive the pipeline scripts in order."
2. **Stage 0 — Brief interview** (the only stage with substantive content in v1):
   - Read the user's one-sentence ask.
   - Run a fixed interview to fill any gaps. The questions (in order):
     1. Segment definition (what the brief calls `target.segment`).
     2. Include / exclude refinements (`target.include`, `target.exclude`).
     3. Geography (`target.geography`).
     4. Approximate domain count target (`target.target_domain_count`).
     5. Priority roles to contact (`who_to_contact.priority_roles`).
     6. Roles to deprioritize (`who_to_contact.deprioritize`).
     7. Value prop and message template path (`message.value_prop`, `message.template`).
     8. From-name, from-gmail, reply-to (`message.from_name`, `message.from_gmail`, `message.reply_to`).
     9. Verifier chain (default: `[smtp_probe, web_citation]`).
     10. Send test batch size (default 10) and daily cap (default 1500).
   - Compute the slug: kebab-cased segment description (e.g., `medium-retailers`). Confirm with the user before writing.
   - Compute the campaign folder name: `<YYYY-MM>_<slug>` where `<YYYY-MM>` is the current month.
   - Use `scripts/setup_campaign.py` (created in section 12) to create the folder. **Until section 12 lands**, Claude creates the folder manually: `mkdir -p campaigns/<YYYY-MM>_<slug>/progress`.
   - Copy `templates/_brief_template.yaml` to `campaigns/<YYYY-MM>_<slug>/brief.yaml` and fill in the answers.
   - Confirm the filled brief with the user (read it back, one section per turn).
3. **Stages 1–5 — stub references**: each is a single line pointing at the relevant playbook, e.g., "Stage 1: see `playbooks/02-domain-sourcing.md` for strategy and `scripts/source_domains.py --help` for invocation." The actual stage instructions are filled in by sections 06 through 11.
4. **Global rules** (these are permanent and stay through v2):
   - Never hardcode segment-specific values in any script. If you want to change behavior, change the brief.
   - All sensitive secrets (`OPENAI_API_KEY`, Gmail tokens) live in `config/secrets.env` / `config/token.json`; never echo them to the user.
   - After Phase A (test-batch send), STOP and ask the user before continuing.
   - Pre-flight failures (port 25 blocked, OAuth expired, invalid brief) have clean exit codes; parse stderr for structured errors when a stage exits with code 3 (brief validation error JSON).
   - On any stage exit code != 0, do NOT continue to the next stage. Show the user the error and ask.

---

### Configuration files

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/config/defaults.yaml`

Engine-wide defaults that the brief can override or that scripts read directly. Required keys:

```yaml
# LLM tier cascade (lib/llm.py reads these)
llm:
  tier1: gpt-4.1-mini
  tier2: gpt-5
  fallbacks: [gpt-5.2, gpt-5, gpt-4.1]
  low_confidence_threshold: 0.4

# Verifier defaults (lib/verifiers/*.py read these; brief.verifier can override)
verifier_defaults:
  rate_per_sec: 0.5
  per_hour_cap: 50
  burst: 10
  greylist_retry: true
  timeout_seconds: 10.0

# Observability cadence (lib/observability.py reads these)
observability:
  cadence_items: 50
  cadence_seconds: 120

# Send-side defaults (brief.sending can override)
sending_defaults:
  throttle_seconds: 45
  send_test_count: 10
```

These values are extracted into a config file (vs. hardcoded constants) because the user explicitly asked the design to be "user-configurable without code changes." Loading is done by `lib/brief.py` (section 02) via a tiny `yaml.safe_load` call; no schema validation is required for `defaults.yaml` in v1 — it is engine-internal.

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/config/verifiers.yaml`

Per-verifier enable/disable flags + provider config for the API verifier. Required content:

```yaml
smtp_probe:
  enabled: true
  rate_per_sec: 0.5
  per_hour_cap: 50

web_citation:
  enabled: true
  fetch_timeout: 8.0

api_provider:
  enabled: false              # off by default; flip to true to use ZeroBounce/NeverBounce
  provider: zerobounce        # zerobounce | neverbounce
  # The api_key is read from secrets.env as ZEROBOUNCE_API_KEY or NEVERBOUNCE_API_KEY
```

Section 08 (`lib/verifiers/`) reads this file. The brief's `verifier.chain` references verifier names that must appear here with `enabled: true`.

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/config/secrets.example.env`

Template file the user copies to `secrets.env`. Required content (commented `KEY=value` lines, no real values):

```dotenv
# OpenAI API
# Required. Get a key from https://platform.openai.com/api-keys
OPENAI_API_KEY=sk-...

# Google OAuth client (used by lib/gmail.py for OAuth flow)
# Download client_secret.json from Google Cloud Console (OAuth 2.0 client ID, Desktop type)
# Then either set GOOGLE_OAUTH_CLIENT_SECRET_PATH to the file, or paste the JSON contents
# into the GOOGLE_OAUTH_CLIENT_SECRET_JSON variable below.
GOOGLE_OAUTH_CLIENT_SECRET_PATH=config/client_secret.json
# GOOGLE_OAUTH_CLIENT_SECRET_JSON='{"installed": {...}}'

# Optional — only required if config/verifiers.yaml has api_provider.enabled=true
# ZEROBOUNCE_API_KEY=
# NEVERBOUNCE_API_KEY=
```

The actual `config/secrets.env` is `.gitignore`d. The user must `cp config/secrets.example.env config/secrets.env` and fill in real values.

---

### Templates

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/templates/_brief_template.yaml`

The canonical brief template. Section 02's `lib/brief.py` defines the Pydantic schema that this template must conform to. The template is the human-facing artifact; the schema enforces correctness.

Required content (copied here so the implementer doesn't have to flip back to `claude-spec.md`):

```yaml
# Identity
slug: medium-retailers           # required, kebab-case (lowercase letters, digits, hyphens)
created_at: 2026-05-21           # ISO date, auto-filled by Claude Code at brief-creation time

# Target — what's being targeted
target:
  segment: "Medium-sized multi-brand retailers"   # required, one-line description
  include: ["curated marketplaces", "hybrid retailer-brands"]
  exclude: ["pure single-brand DTC", "enterprise (>$500M rev)"]
  geography: "US + Canada"
  target_domain_count: 1500       # required, int > 0

# Who to contact (leverage)
who_to_contact:
  priority_roles:                 # required, at least 1
    - Founder
    - CEO
    - VP E-commerce
    - Head of Digital
    - CTO
  deprioritize:
    - Marketing
    - PR
    - HR
    - "generic info@"
  contacts_per_company: 3         # default 3, max 12

# Message
message:
  template: templates/ai-agent-integration.md     # required, path relative to repo root; file must exist
  value_prop: "Integrate AI shopping agents on your storefront"
  personalize_first_name: true    # whether to LLM-canonicalize first names; default true
  from_name: "Smrjit"
  from_gmail: "smrjit@example.com"
  reply_to: "smrjit@example.com"

# Verification
verifier:
  chain: [smtp_probe, web_citation]    # ordered cascade; verifier names must exist in config/verifiers.yaml
  greylist_retry: true            # if true, 4xx → wait 90s → 1 retry → mark "unknown"
  rate_limit: 3.0                 # SMTP probes/sec; also used as upper bound

# Sending
sending:
  send_test_count: 10             # send this many first, then PAUSE for approval
  send_rate_per_day: 1500         # Workspace default; safety cap is 2000
  throttle_seconds: 45            # base gap; actual delay = base * uniform(0.5, 1.5)

# Safety
safety:
  dedup_scope: all_campaigns      # all_campaigns | this_campaign
  require_approval_after: [send_test]   # the only hard stop in v1

# Notes (free text, optional)
notes: |
  Anything Claude Code should know about this segment.
```

Important constraints baked into the schema (enforced by `lib/brief.py` in section 02 — listed here so the template doesn't accidentally violate them):

- `slug` must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$` (kebab-case).
- `target.target_domain_count` must be a positive integer.
- `who_to_contact.priority_roles` must have at least 1 entry.
- `who_to_contact.contacts_per_company` is bounded `1 ≤ x ≤ 12`.
- `message.template` is a relative path that must exist when the brief is loaded.
- `message.from_gmail`, `message.reply_to` must look like emails (have an `@`).
- `sending.send_rate_per_day` must be `≤ 2000` (safety cap).
- `verifier.chain` is non-empty and each entry is one of: `smtp_probe`, `web_citation`, `api_provider`.
- `safety.dedup_scope` ∈ `{all_campaigns, this_campaign}`.
- Pydantic `extra="forbid"` — unknown top-level keys are rejected.

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/templates/_example.md`

A short doc explaining the message-template slot syntax. Required content:

- Templates are markdown files with `{{slot}}` placeholders.
- Slots supported in v1: `first_name`, `name`, `company`, `role`, `value_prop`, `from_name`.
- The first non-blank line MAY begin with `Subject: ...`; if so, that line becomes the subject and is stripped from the body. Otherwise the first line is used as-is for the subject.
- The body is rendered to both `body_plain` (verbatim) and `body_html` (paragraphs wrapped in `<p>`).
- One example with the user's actual value prop (filled in section 10):
  ```
  Subject: Quick question, {{first_name}}

  Hi {{first_name}},

  Saw your work at {{company}}. {{value_prop}}...

  — {{from_name}}
  ```

#### `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/templates/ai-agent-integration.md`

Placeholder file for the user's first real template. Section 10 fills in the full body. For this section, create the file with a one-line stub:

```
Subject: Quick question, {{first_name}}

Hi {{first_name}}, this template is filled in during section 10 (compose_emails).
```

The file must exist now so the example brief (`_brief_template.yaml`) passes validation when the brief schema is implemented in section 02 (the `message.template` field requires an existing file).

---

### Playbook stubs

For each playbook listed below, create the file with **only two sections**: `# Purpose` (one paragraph) and `# When Claude reads this` (one paragraph). The detailed content is filled in by sections 06–12.

Required playbook files:

- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/00-pipeline-overview.md` — Purpose: one-paragraph summary of the 5-stage pipeline; when Claude reads: at campaign start, to remind itself of the overall shape.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/01-target-definition.md` — Purpose: guidelines for the Stage 0 interview; when Claude reads: when filling `target.*` sections of brief.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/02-domain-sourcing.md` — Purpose: Stage 1 strategy; filled in section 06.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/03-contact-discovery.md` — Purpose: Stage 2 strategy; filled in section 07.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/04-email-verification.md` — Purpose: Stage 3 strategy; filled in section 08.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/05-email-composition.md` — Purpose: Stage 4 strategy; filled in section 10.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/06-sending.md` — Purpose: Stage 5 strategy + test-batch philosophy; filled in section 11.
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/07-tracking-followup.md` — Purpose: Stage 6 (bounce-only in v1) + manual follow-up notes; filled in section 12.

Each stub looks like:

```markdown
# Purpose

(one paragraph describing what this playbook covers when complete)

# When Claude reads this

(one paragraph: at what stage transition and for what decision)
```

---

### Empty directories (must exist, no files in this section)

Create the directory so later sections can write into it. The cleanest way in git is to add a `.gitkeep` file (empty), since git doesn't track empty directories.

- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/.gitkeep`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/lib/.gitkeep`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/lib/verifiers/.gitkeep`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/.gitkeep`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/lib/.gitkeep`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/lib/verifiers/.gitkeep`

Note: `data/` and `campaigns/` are `.gitignore`d — do NOT create them in this section; they're runtime-only directories created by the scripts on first use.

---

## Tests

This section's tests are **shape-validation only** — no Python logic exists yet to test. The tests verify that the files this section creates are well-formed.

Test location: `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/test_skeleton.py`.

Tests to write (stub signatures + docstrings — full bodies are short):

```python
# tests/test_skeleton.py
"""Shape-validation tests for the section 01 skeleton.

These tests run BEFORE any library code is written and verify that:
- pyproject.toml is valid TOML and declares the right deps
- config YAML files parse with yaml.safe_load
- secrets.example.env is a well-formed dotenv file
- templates/_brief_template.yaml parses as YAML and has every required top-level section
- every playbook stub has the Purpose + When-Claude-reads-this sections
- .gitignore excludes config/secrets.env and config/token.json
"""

def test_pyproject_parses_and_declares_required_deps():
    """Parse pyproject.toml. Assert: name == 'outreach-bot'; requires-python startswith '>=3.12';
    every required dep name (openai, pydantic, pyyaml, dnspython, google-api-python-client,
    google-auth-oauthlib, google-auth, httpx) appears in the dependencies list."""

def test_pyproject_declares_all_console_scripts():
    """Assert every entry-point listed in section 01's pyproject.toml description is present
    under [project.scripts]: outreach-source-domains, outreach-discover-contacts,
    outreach-verify-emails, outreach-compose-emails, outreach-send-emails, outreach-poll-bounces,
    outreach-status, outreach-run-pipeline."""

def test_gitignore_excludes_secrets_and_runtime_state():
    """Read .gitignore. Assert lines for: config/secrets.env, config/token.json, data/,
    campaigns/, __pycache__/, .venv/."""

def test_defaults_yaml_parses_and_has_required_keys():
    """yaml.safe_load(defaults.yaml). Assert top-level keys: llm, verifier_defaults,
    observability, sending_defaults. Assert llm.tier1 == 'gpt-4.1-mini',
    verifier_defaults.per_hour_cap == 50, observability.cadence_items == 50."""

def test_verifiers_yaml_parses_and_has_required_keys():
    """yaml.safe_load(verifiers.yaml). Assert keys smtp_probe, web_citation, api_provider
    exist; smtp_probe.enabled is True, api_provider.enabled is False."""

def test_secrets_example_env_is_well_formed_dotenv():
    """Read secrets.example.env line-by-line. Assert: at least one OPENAI_API_KEY line
    (commented or not), at least one GOOGLE_OAUTH_CLIENT_SECRET_PATH line. No real-looking
    values (no line matches r'^[A-Z_]+=sk-[a-zA-Z0-9]{20,}' that isn't a clear placeholder
    like 'sk-...')."""

def test_brief_template_parses_as_yaml():
    """yaml.safe_load(templates/_brief_template.yaml). Assert it's a dict with top-level keys:
    slug, created_at, target, who_to_contact, message, verifier, sending, safety, notes."""

def test_brief_template_target_section_complete():
    """The target subsection of the brief template has segment, include, exclude, geography,
    target_domain_count. target_domain_count is a positive int."""

def test_brief_template_who_to_contact_complete():
    """priority_roles is a non-empty list. contacts_per_company is an int in [1, 12]."""

def test_brief_template_message_template_path_exists():
    """message.template points to templates/ai-agent-integration.md and that file exists
    (this is what lib/brief.py will validate too — surfacing it here prevents a broken
    template shipping)."""

def test_brief_template_verifier_chain_valid():
    """verifier.chain is a non-empty list whose entries are subsets of
    {smtp_probe, web_citation, api_provider}."""

def test_brief_template_sending_rate_under_safety_cap():
    """sending.send_rate_per_day <= 2000 (the documented safety cap)."""

def test_brief_template_slug_is_kebab_case():
    """slug matches r'^[a-z0-9][a-z0-9-]*[a-z0-9]$'."""

def test_all_playbook_stubs_exist_with_required_sections():
    """For each of the 8 expected playbook filenames, the file exists and contains
    the substrings '# Purpose' and '# When Claude reads this'."""

def test_template_example_file_exists_and_documents_slots():
    """templates/_example.md exists and mentions the supported slot names: first_name,
    name, company, role, value_prop, from_name."""

def test_ai_agent_integration_template_exists():
    """templates/ai-agent-integration.md exists (stub is fine; section 10 fills it in).
    Required so brief validation has a real path to point at."""

def test_claude_md_describes_stage_0_interview():
    """CLAUDE.md exists and contains substrings: 'Stage 0', 'brief.yaml', 'kebab-case',
    'priority_roles'. Verifies the orchestrator covers the v1 interview."""

def test_required_directories_exist():
    """The directories scripts/, scripts/lib/, scripts/lib/verifiers/, tests/, tests/lib/,
    tests/lib/verifiers/, playbooks/, config/, templates/ all exist (via .gitkeep or files)."""
```

All tests use only the standard library plus `pyyaml` and `tomllib` (stdlib in 3.11+). No project-internal imports — these tests run BEFORE `lib/` exists.

---

## Acceptance Criteria

- `uv sync` runs cleanly from a fresh clone with no errors.
- `uv run pytest tests/test_skeleton.py` is green.
- `.gitignore` actually prevents `config/secrets.env`, `config/token.json`, `data/`, `campaigns/` from being committed (verify with `git status` after creating dummy files at those paths).
- `templates/_brief_template.yaml` parses with `yaml.safe_load` and matches the shape that section 02's `lib/brief.py` will later validate against. (Cannot be machine-verified in this section — the schema doesn't exist yet — but the test suite above sanity-checks the shape.)
- `CLAUDE.md` mentions Stage 0 (the interview) substantively. Stages 1–5 may be stubs/pointers.
- Every playbook file exists with the two required headings.
- `config/secrets.example.env` contains NO real secret values (this is a hard security requirement per the user's global CLAUDE.md).

---

## Out-of-Scope for This Section

To keep section 01 small and unblocking:

- **No Python library code** — `lib/*.py` is section 02. `lib/observability.py`, `lib/dedup.py` are section 03. `lib/llm.py`, `lib/gmail.py` are section 04.
- **No stage scripts** — `scripts/source_domains.py` etc. start in section 06.
- **No conftest.py** — that's section 02 (it needs the brief and csv_schema fixtures).
- **No real OAuth flow** — the README documents `python scripts/lib/gmail.py authorize` but the script doesn't exist yet. Section 04 makes it real.
- **No data/ or campaigns/ directories on disk** — those are runtime-created.
- **No playbook content beyond stubs** — sections 06–12 fill these in.
- **No CLAUDE.md content for stages 1–5 beyond stub references** — sections 06–11 add the per-stage instructions; section 12 produces CLAUDE.md v2.

---

## Cross-cutting invariants (referenced, not re-stated)

The following invariants apply to every section in this project. Section 01 enforces them via the `.gitignore`, the safety cap in the brief template, and the schema-shape choices listed above. Subsequent sections will reference these by name:

- **Security**: secrets only in `config/secrets.env` and `config/token.json`, both gitignored. Never log secrets; never bundle them into any client-side artifact.
- **Brief is single source of truth**: nothing in the engine layer hardcodes segment-specific values.
- **Schema rules** (every Pydantic model in this codebase): `extra="forbid"`, `Optional[X]` fields use `default=None`, LLM response schemas require non-null `source_url`. Section 02 enforces these for code; this section just ensures the example brief is shaped consistently.
- **Exit codes** (used by `CLAUDE.md` to recover): 0 success, 1 user-correctable refusal, 2 stage failure, 3 brief-validation error (with structured JSON on stderr).
- **Safety caps**: `send_rate_per_day ≤ 2000`, `contacts_per_company ≤ 12`. The brief template uses values well below those caps but section 02 enforces them at validation time.