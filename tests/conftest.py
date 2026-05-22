"""Shared pytest fixtures for the outreach-bot test suite.

Lives at the top of tests/ so every subdirectory (tests/lib/, tests/lib/verifiers/, ...)
automatically picks these up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

import pytest


# ---------------------------------------------------------------------------
# Campaign / brief fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_campaign_dir(tmp_path: Path) -> Path:
    """Empty tmp directory shaped like a campaign.

    Layout:
        <tmp>/                  (returned)
        <tmp>/progress/         (created)
        <tmp>/brief.yaml        (absent — caller writes it if needed)
    """
    (tmp_path / "progress").mkdir()
    return tmp_path


@pytest.fixture
def sample_template(tmp_campaign_dir: Path) -> Path:
    """Write a minimal message template inside the tmp campaign and return its path.

    The brief's message.template field must point at an existing file; the schema
    validator hits the filesystem at load time.
    """
    template = tmp_campaign_dir / "message_template.md"
    template.write_text(
        dedent(
            """\
            Subject: Quick question, {{first_name}}

            Hi {{first_name}}, this is a test template used by the conftest fixture.

            — {{from_name}}
            """
        ),
        encoding="utf-8",
    )
    return template


@pytest.fixture
def sample_brief_yaml(sample_template: Path) -> str:
    """A complete, valid brief.yaml as a string.

    Tests that need a *broken* brief load this with yaml.safe_load, mutate the
    dict, then yaml.safe_dump back to a file.
    """
    return dedent(
        f"""\
        slug: test-campaign
        created_at: 2026-05-22
        target:
          segment: "Test segment"
          include: ["thing-one"]
          exclude: ["thing-two"]
          geography: "US"
          target_domain_count: 20
        who_to_contact:
          priority_roles:
            - Founder
            - CEO
          deprioritize:
            - Marketing
          contacts_per_company: 3
        message:
          template: {sample_template}
          value_prop: "Make widgets faster"
          personalize_first_name: true
          from_name: "Test Sender"
          from_gmail: "test@example.com"
          reply_to: "test@example.com"
        verifier:
          chain: [smtp_probe, web_citation]
          rate_per_sec: 0.5
          per_hour_cap: 50
          burst: 10
          greylist_retry: true
        sending:
          send_test_count: 5
          send_rate_per_day: 100
          throttle_seconds: 1.0
        safety:
          scope: this_campaign
        notes: "Fixture brief for tests."
        """
    )


@pytest.fixture
def sample_brief(tmp_campaign_dir: Path, sample_brief_yaml: str):
    """Write sample_brief_yaml to tmp_campaign_dir/brief.yaml and return loaded Brief."""
    from scripts.lib.brief import load

    path = tmp_campaign_dir / "brief.yaml"
    path.write_text(sample_brief_yaml, encoding="utf-8")
    return load(path)


# ---------------------------------------------------------------------------
# Clock / sleep fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeClock:
    """Mutable monotonic-clock substitute.

    Pass `clock.now` as the `clock` kwarg and `clock.sleep` as the `sleep` kwarg
    to RateLimiter / HourlyLimiter. Tests advance "time" by calling clock.sleep().
    """
    t: float = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("negative sleep")
        self.t += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# DNS / LLM / Gmail fixture placeholders
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_dns_answer():
    """Factory for fake dns.resolver answer objects.

    Returns a callable that builds an answer with the given (preference, exchange)
    tuples. Used by tests/lib/test_dns_check.py.
    """

    @dataclass
    class _MX:
        preference: int
        exchange: object  # quacks as dns.name.Name via .to_text() if needed

    @dataclass
    class _Answer:
        records: list = field(default_factory=list)

        def __iter__(self):
            return iter(self.records)

    def _build(pairs: list[tuple[int, str]]) -> _Answer:
        return _Answer(records=[_MX(preference=p, exchange=_TextExchange(e)) for p, e in pairs])

    @dataclass
    class _TextExchange:
        text: str

        def to_text(self) -> str:
            return self.text

    return _build


@pytest.fixture
def fake_llm_client():
    pytest.skip("fake_llm_client is filled in by section 04")


@pytest.fixture
def fake_gmail_client():
    pytest.skip("fake_gmail_client is filled in by section 04")
