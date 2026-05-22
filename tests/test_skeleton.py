"""Shape-validation tests for the section 01 skeleton.

These tests run BEFORE any library code is written and verify that:
- pyproject.toml is valid TOML and declares the right deps
- config YAML files parse with yaml.safe_load
- secrets.example.env is a well-formed dotenv file (no real secrets)
- templates/_brief_template.yaml parses as YAML and has every required top-level section
- every playbook stub has the Purpose + When-Claude-reads-this sections
- .gitignore excludes config/secrets.env and config/token.json
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


# --- pyproject ---------------------------------------------------------------

REQUIRED_DEPS = {
    "openai",
    "pydantic",
    "pyyaml",
    "dnspython",
    "google-api-python-client",
    "google-auth-oauthlib",
    "google-auth",
    "httpx",
}

REQUIRED_SCRIPTS = {
    "outreach-source-domains",
    "outreach-discover-contacts",
    "outreach-verify-emails",
    "outreach-compose-emails",
    "outreach-send-emails",
    "outreach-poll-bounces",
    "outreach-status",
    "outreach-run-pipeline",
}


def _load_pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_pyproject_parses_and_declares_required_deps() -> None:
    data = _load_pyproject()
    assert data["project"]["name"] == "outreach-bot"
    assert data["project"]["requires-python"].startswith(">=3.12")
    declared = {
        # Match the leading PEP 508 name (everything up to the first version specifier / extra / whitespace).
        re.split(r"[<>=!~\[ ]", dep, maxsplit=1)[0].lower()
        for dep in data["project"]["dependencies"]
    }
    missing = REQUIRED_DEPS - declared
    assert not missing, f"pyproject.toml missing deps: {missing}"


def test_pyproject_declares_all_console_scripts() -> None:
    data = _load_pyproject()
    scripts = set(data["project"]["scripts"].keys())
    missing = REQUIRED_SCRIPTS - scripts
    assert not missing, f"pyproject.toml missing console scripts: {missing}"


# --- .gitignore --------------------------------------------------------------


def test_gitignore_excludes_secrets_and_runtime_state() -> None:
    body = _read(".gitignore")
    required = [
        "config/secrets.env",
        "config/token.json",
        "data/",
        "campaigns/",
        "__pycache__/",
        ".venv/",
    ]
    for line in required:
        assert line in body, f".gitignore missing entry: {line}"


# --- config YAML -------------------------------------------------------------


def test_defaults_yaml_parses_and_has_required_keys() -> None:
    data = yaml.safe_load(_read("config/defaults.yaml"))
    for key in ("llm", "verifier_defaults", "observability", "sending_defaults"):
        assert key in data, f"config/defaults.yaml missing top-level key: {key}"
    assert data["llm"]["tier1"] == "gpt-4.1-mini"
    assert data["verifier_defaults"]["per_hour_cap"] == 50
    assert data["observability"]["cadence_items"] == 50


def test_verifiers_yaml_parses_and_has_required_keys() -> None:
    data = yaml.safe_load(_read("config/verifiers.yaml"))
    for key in ("smtp_probe", "web_citation", "api_provider"):
        assert key in data, f"config/verifiers.yaml missing key: {key}"
    assert data["smtp_probe"]["enabled"] is True
    assert data["api_provider"]["enabled"] is False


# --- secrets.example.env -----------------------------------------------------


def test_secrets_example_env_is_well_formed_dotenv() -> None:
    body = _read("config/secrets.example.env")
    assert "OPENAI_API_KEY" in body, "missing OPENAI_API_KEY in secrets.example.env"
    assert "GOOGLE_OAUTH_CLIENT_SECRET_PATH" in body, (
        "missing GOOGLE_OAUTH_CLIENT_SECRET_PATH in secrets.example.env"
    )
    # No real secrets. A real OpenAI key has 20+ alnum chars after "sk-"; placeholder is "sk-...".
    real_key_pat = re.compile(r"^[A-Z_]+=sk-[a-zA-Z0-9]{20,}\b", re.MULTILINE)
    assert not real_key_pat.search(body), "secrets.example.env appears to contain a real secret"


# --- brief template ----------------------------------------------------------


def _load_brief_template() -> dict:
    return yaml.safe_load(_read("templates/_brief_template.yaml"))


def test_brief_template_parses_as_yaml() -> None:
    data = _load_brief_template()
    assert isinstance(data, dict)
    expected = {
        "slug",
        "created_at",
        "target",
        "who_to_contact",
        "message",
        "verifier",
        "sending",
        "safety",
        "notes",
    }
    missing = expected - set(data.keys())
    assert not missing, f"_brief_template.yaml missing top-level keys: {missing}"


def test_brief_template_target_section_complete() -> None:
    data = _load_brief_template()
    target = data["target"]
    for key in ("segment", "include", "exclude", "geography", "target_domain_count"):
        assert key in target, f"target.{key} missing"
    assert isinstance(target["target_domain_count"], int)
    assert target["target_domain_count"] > 0


def test_brief_template_who_to_contact_complete() -> None:
    data = _load_brief_template()
    wtc = data["who_to_contact"]
    assert isinstance(wtc["priority_roles"], list)
    assert len(wtc["priority_roles"]) >= 1
    cpc = wtc["contacts_per_company"]
    assert isinstance(cpc, int)
    assert 1 <= cpc <= 12


def test_brief_template_message_template_path_exists() -> None:
    data = _load_brief_template()
    template_path = REPO_ROOT / data["message"]["template"]
    assert template_path.exists(), f"message.template points at missing file: {template_path}"


def test_brief_template_verifier_chain_valid() -> None:
    data = _load_brief_template()
    chain = data["verifier"]["chain"]
    assert isinstance(chain, list)
    assert len(chain) >= 1
    valid = {"smtp_probe", "web_citation", "api_provider"}
    assert set(chain).issubset(valid), f"verifier.chain has invalid entries: {set(chain) - valid}"


def test_brief_template_sending_rate_under_safety_cap() -> None:
    data = _load_brief_template()
    assert data["sending"]["send_rate_per_day"] <= 2000


def test_brief_template_slug_is_kebab_case() -> None:
    data = _load_brief_template()
    assert re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", data["slug"]), f"slug not kebab-case: {data['slug']}"


# --- playbook stubs ---------------------------------------------------------

EXPECTED_PLAYBOOKS = [
    "00-pipeline-overview.md",
    "01-target-definition.md",
    "02-domain-sourcing.md",
    "03-contact-discovery.md",
    "04-email-verification.md",
    "05-email-composition.md",
    "06-sending.md",
    "07-tracking-followup.md",
]


def test_all_playbook_stubs_exist_with_required_sections() -> None:
    for name in EXPECTED_PLAYBOOKS:
        path = REPO_ROOT / "playbooks" / name
        assert path.exists(), f"missing playbook: {path}"
        body = path.read_text(encoding="utf-8")
        assert "# Purpose" in body, f"{name} missing '# Purpose' section"
        assert "# When Claude reads this" in body, f"{name} missing '# When Claude reads this' section"


# --- templates ---------------------------------------------------------------


def test_template_example_file_exists_and_documents_slots() -> None:
    body = _read("templates/_example.md")
    for slot in ("first_name", "name", "company", "role", "value_prop", "from_name"):
        assert slot in body, f"_example.md doesn't document slot {{{{ {slot} }}}}"


def test_ai_agent_integration_template_exists() -> None:
    assert (REPO_ROOT / "templates" / "ai-agent-integration.md").exists()


# --- CLAUDE.md ---------------------------------------------------------------


def test_claude_md_describes_stage_0_interview() -> None:
    body = _read("CLAUDE.md")
    for needle in ("Stage 0", "brief.yaml", "kebab-case", "priority_roles"):
        assert needle in body, f"CLAUDE.md missing required mention of: {needle}"


# --- directory layout --------------------------------------------------------


def test_required_directories_exist() -> None:
    for rel in (
        "scripts",
        "scripts/lib",
        "scripts/lib/verifiers",
        "tests",
        "tests/lib",
        "tests/lib/verifiers",
        "playbooks",
        "config",
        "templates",
    ):
        path = REPO_ROOT / rel
        assert path.is_dir(), f"required directory missing: {path}"
