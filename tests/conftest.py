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
    """A minimal stand-in for scripts.lib.llm.LLMClient.

    Use ``client.queue(parsed_or_exception)`` to enqueue behaviors for upcoming
    ``parse()`` calls. Refusals, low-confidence, and exceptions are all expressed
    by what you queue (see tests/lib/test_llm.py for the canonical fakes).
    """
    from dataclasses import dataclass

    @dataclass
    class _Cost:
        usd: float = 0.0
        input_tokens: int = 0
        output_tokens: int = 0
        web_search_calls: int = 0
        model: str = "fake"

        def __add__(self, other):
            return _Cost(
                usd=self.usd + other.usd,
                input_tokens=self.input_tokens + other.input_tokens,
                output_tokens=self.output_tokens + other.output_tokens,
                web_search_calls=self.web_search_calls + other.web_search_calls,
                model=self.model,
            )

    @dataclass
    class _Result:
        parsed: object = None
        refused: bool = False
        refusal_text: str = ""
        low_confidence: bool = False
        cost: _Cost = field(default_factory=_Cost)

    class _Fake:
        def __init__(self):
            self._behaviors = []
            self.parse_calls = []
            self.cascade_calls = []

        def queue(self, behavior):
            self._behaviors.append(behavior)

        def parse(self, messages, text_format, **kwargs):
            self.parse_calls.append((messages, text_format, kwargs))
            if not self._behaviors:
                raise AssertionError("fake_llm_client: no queued behavior")
            b = self._behaviors.pop(0)
            if isinstance(b, Exception):
                raise b
            return b

        def cascade(self, messages, text_format, **kwargs):
            self.cascade_calls.append((messages, text_format, kwargs))
            return self.parse(messages, text_format, **kwargs)

    return _Fake()


@pytest.fixture
def fake_gmail_client():
    """Minimal stand-in for scripts.lib.gmail.GmailClient.

    ``client.queue_send_response(response_dict_or_exception)`` controls what the
    next ``send()`` returns. Sent payloads are captured on ``client.sent``.
    """

    class _Fake:
        def __init__(self):
            self._responses = []
            self.sent: list[dict] = []
            self.bounces: list = []

        def queue_send_response(self, behavior):
            self._responses.append(behavior)

        def send(self, to, **kwargs):
            payload = {"to": to, **kwargs}
            self.sent.append(payload)
            if not self._responses:
                from scripts.lib.gmail import SendResult
                return SendResult(gmail_message_id=f"mid-{len(self.sent)}", thread_id=f"tid-{len(self.sent)}")
            b = self._responses.pop(0)
            if isinstance(b, Exception):
                raise b
            return b

        def list_bounces(self, since_message_id=None):
            return list(self.bounces)

    return _Fake()
