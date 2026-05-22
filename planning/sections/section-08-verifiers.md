Now I have everything I need. Let me generate the section content.

# section-08-verifiers — The Pluggable Verifier Layer

## Purpose

This section implements the pluggable email verification layer used by Stage 3 (`verify_emails.py`, built in section-09). Three concrete verifier implementations plus the shared interface they all conform to. The chain is configured via the brief (`brief.verifier.chain`) and `config/verifiers.yaml`; the verify-emails script (section-09) walks the chain until one verifier returns `accepted`.

This section is parallelizable with section-07 (discover-contacts) — both depend on M1 conceptually (the M1 output `domains.csv` informs them) but produce independent code that doesn't share state.

## Dependencies (already built)

- **section-02-lib-foundations** — `lib/dns_check.py` (`mx_records`, `has_mail`, `is_null_mx`), `lib/rate_limit.py` (`RateLimiter`, `HourlyLimiter`).
- **section-03-lib-observability** — used only insofar as verifiers may be called from a stage that has an observer; verifiers themselves do not emit observer events. The calling script (section-09) does.

This section does NOT depend on section-04 (LLM/Gmail), section-05, section-06, or section-07.

## Files to create

```
scripts/lib/verifiers/__init__.py
scripts/lib/verifiers/base.py
scripts/lib/verifiers/smtp_probe.py
scripts/lib/verifiers/web_citation.py
scripts/lib/verifiers/api_provider.py
playbooks/04-email-verification.md
config/verifiers.yaml          # if not already in section-01; otherwise verify shape matches below

tests/lib/verifiers/__init__.py
tests/lib/verifiers/test_base.py
tests/lib/verifiers/test_smtp_probe.py
tests/lib/verifiers/test_web_citation.py
tests/lib/verifiers/test_api_provider.py
```

The `tests/lib/verifiers/` tree should mirror the production layout.

## Cross-cutting invariants (apply to every file in this section)

- Every Pydantic model declares `model_config = ConfigDict(extra="forbid")`.
- Every `Optional[X]` field uses `default=None` (OpenAI strict-mode rule; carried over from §10 invariants even though these models are not LLM-response schemas — uniformity matters).
- Verifiers MUST be stateless aside from internal rate limiters / DNS cache. A single instance may be called concurrently from multiple worker threads in section-09.
- Verifiers do not write CSVs or progress files. They return a `VerificationResult` to the caller.
- Verifiers do not log to `activity.log` directly; the caller maps result `notes` to observer events.

---

## 1. `scripts/lib/verifiers/base.py` — the interface

### 1.1 Public API

```python
class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["accepted", "catchall", "rejected", "unknown"]
    confidence: Literal["verified-smtp", "verified-web", "verified-api", ""]
    source_url: str       # "https://verified-smtp/" sentinel, the real URL, or ""
    notes: str            # diagnostic info, e.g. "MX tarpit (O365)"


class Verifier(Protocol):
    name: str
    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult: ...
    def assert_available(self) -> None:
        """Pre-flight. Raises VerifierUnavailable with actionable remediation message."""


class VerifierUnavailable(Exception):
    """Raised by assert_available() when the verifier cannot run in this environment.
    Carries a structured remediation message in .args[0] (a plain string the caller
    prints verbatim before exiting with code 2)."""
```

Notes on the status enum:
- `accepted` — strong positive (e.g., SMTP 250 to candidate + 550 to random; or HEAD-200 page containing both local-part and domain).
- `catchall` — domain accepts every recipient (both 250s in SMTP probe, or MX is a known tarpit provider).
- `rejected` — SMTP 5xx to candidate, or null MX, or no MX at all.
- `unknown` — transient or insufficient evidence (4xx after greylist retry, citation missing, HEAD non-200, etc.).

The sentinel `source_url="https://verified-smtp/"` is a literal string (NOT a real URL); section-09 uses it as the source for `EmailRow.source_url` when SMTP accepted the address.

### 1.2 Tests — `tests/lib/verifiers/test_base.py`

Stubs only; one assertion per test:

```python
# Test: Verifier protocol — a minimal dummy class with .name, .verify, .assert_available
#       satisfies the Protocol (isinstance(dummy, Verifier) under @runtime_checkable, or
#       a structural check via getattr).
# Test: VerificationResult schema — status field rejects any value not in the four-element
#       enum (Pydantic ValidationError on status="bogus").
# Test: VerificationResult — confidence field rejects values outside the four-element enum.
# Test: VerifierUnavailable carries a structured remediation message — raising it with a
#       string arg and catching it, the .args[0] is the original string verbatim.
```

---

## 2. `scripts/lib/verifiers/smtp_probe.py` — RFC 5321 probe

### 2.1 Behavior

```python
class SmtpProbeVerifier:
    name = "smtp_probe"

    TARPIT_MX_PATTERNS = [
        "*.mail.protection.outlook.com",
        "*.olc.protection.outlook.com",
        "*.pphosted.com",
        "*.ppe-hosted.com",
        "*.mimecast.com",
    ]

    def __init__(self, *, rate_per_sec: float, per_hour_cap: int,
                 greylist_retry: bool, timeout: float = 10.0): ...

    def assert_available(self) -> None:
        """Open TCP to gmail-smtp-in.l.google.com:25. On any socket error, raise
        VerifierUnavailable with this exact message:
          'Port 25 blocked. Connect to Dartmouth VPN, or set verifier.chain to
           ["web_citation"] in the brief, or enable api_provider.'"""

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        """1. Acquire rate limiter (the internal RateLimiter at rate_per_sec) and
              the HourlyLimiter at per_hour_cap.
           2. Look up MX records via lib.dns_check.mx_records(domain).
              - If empty list OR lib.dns_check.is_null_mx(domain) → return
                VerificationResult(status='rejected', confidence='', source_url='',
                notes='no MX' or 'null MX').
           3. Walk TARPIT_MX_PATTERNS; if the highest-priority MX hostname matches
              any glob, return status='catchall', notes='MX tarpit (<pattern>)'.
              Do NOT open a socket in this case.
           4. Else open SMTP to the MX hostname on port 25, with timeout.
                HELO <local hostname>
                MAIL FROM:<probe@<local hostname>>
                RCPT TO:<candidate>
                RSET
                RCPT TO:<random 20-char local-part>@<domain>
                QUIT
           5. Map response codes (per claude-research.md §B.2):
                - candidate=250, random=550 → accepted, confidence='verified-smtp',
                  source_url='https://verified-smtp/'.
                - candidate=250, random=250 → catchall (no confidence; caller decides
                  whether to keep).
                - candidate=550 → rejected.
                - candidate=4xx and greylist_retry=true → sleep 90s (use time.sleep
                  but allow injection via an internal clock for tests), retry ONCE.
                  Still 4xx → unknown. 250 on retry → accepted.
                - candidate=4xx and greylist_retry=false → unknown.
                - connect refused, 421, generic socket error → unknown.
           6. Always close the socket. Never raise — wrap probe in try/except and
              map any unexpected exception to status='unknown', notes='exc: <type>'.
        """
```

Implementation notes:
- Use stdlib `smtplib.SMTP` (NOT `SMTP_SSL`; port 25 is plaintext). Set `local_hostname` to `socket.getfqdn()`.
- The MAIL FROM probe address should look benign; pick a domain you own or use `postmaster@<local-hostname>`. The probe MUST NOT use the campaign's actual `from_gmail`.
- The TARPIT match is **glob-style** (`fnmatch.fnmatchcase`), case-insensitive on the hostname (lowercase the MX answer before matching).
- The HourlyLimiter is the binding constraint per §2.9 invariants (defaults 50/hour). The RateLimiter handles short bursts.
- Greylist sleep should be implemented via an injectable `sleep` callable so the unit test can use a mocked clock.

### 2.2 Tests — `tests/lib/verifiers/test_smtp_probe.py`

Use `aiosmtpd` (already declared as a test dep in section-01's `pyproject.toml`) as a controllable fake SMTP server, OR mock `smtplib.SMTP` directly. Either is acceptable; the prior art uses socket-level mocks, which are simpler.

```python
# Happy path:
# Test: HELO ok, candidate RCPT → 250, random RCPT → 550 → status=accepted,
#       confidence='verified-smtp', source_url='https://verified-smtp/'.

# Catch-all:
# Test: both candidate and random RCPTs → 250 → status=catchall.

# Rejection:
# Test: candidate RCPT → 550 → status=rejected.

# Connection failure:
# Test: socket.gaierror on connect → status=unknown.
# Test: server returns 421 on connect → status=unknown.

# Greylisting:
# Test: candidate 4xx, greylist_retry=true, retry returns 250 → status=accepted.
#       Verify the injected sleep callable was invoked with 90.
# Test: candidate 4xx, greylist_retry=true, retry also 4xx → status=unknown.
# Test: candidate 4xx, greylist_retry=false → status=unknown immediately; sleep
#       callable NOT invoked.

# MX tarpit hard-skip (each its own test):
# Test: MX hostname 'foo.mail.protection.outlook.com' → status=catchall,
#       notes contains 'tarpit', SMTP socket NEVER opened (assert the mock SMTP
#       constructor was not called).
# Test: MX hostname 'mx.olc.protection.outlook.com' → same.
# Test: MX hostname 'mail.pphosted.com' → same.
# Test: MX hostname 'mx0.ppe-hosted.com' → same.
# Test: MX hostname 'eu-smtp.mimecast.com' → same.
# Test: MX hostname 'mail.example.com' (non-tarpit) → probe proceeds (mock SMTP
#       constructor IS called).

# DNS edge cases:
# Test: lib.dns_check.mx_records returns [] → status=rejected, notes mentions
#       'no MX'. SMTP socket NEVER opened.
# Test: lib.dns_check.is_null_mx returns True → status=rejected, notes mentions
#       'null MX'. SMTP socket NEVER opened.

# Pre-flight:
# Test: assert_available with mocked socket success → no exception.
# Test: assert_available with mocked socket OSError → raises VerifierUnavailable
#       whose .args[0] == the exact remediation string above (substring match on
#       'Port 25 blocked' and 'Dartmouth VPN' is fine).

# Rate limiting:
# Test: 10 calls at rate_per_sec=2.0 → with a mocked monotonic clock, elapsed
#       time ≈ 5s ± epsilon.
# Test: HourlyLimiter integration — 60 verify() calls against per_hour_cap=30
#       takes ≥ 1 hour under a mocked clock (the cap is the binding constraint;
#       cf. tests/lib/test_rate_limit.py sustained-rate test).
```

---

## 3. `scripts/lib/verifiers/web_citation.py` — primary-source grounding

### 3.1 Behavior

```python
class WebCitationVerifier:
    name = "web_citation"

    AGGREGATOR_HOSTS = set([
        "contactout.com", "rocketreach.co", "rocketreach.com",
        "zoominfo.com", "apollo.io", "lusha.com", "hunter.io",
        "success.ai", "snov.io", "leadiq.com", "salesintel.com",
        "dropcontact.com", "getprospect.com", "kendo.tools",
        "signalhire.com", "swordfish.ai", "voilanorbert.com",
        "skrapp.io", "anymailfinder.com", "nymeria.io", "uplead.com",
    ])

    def __init__(self, *, fetch_timeout: float = 8.0): ...

    def assert_available(self) -> None:
        """No-op. Always available."""

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        """Multi-step grounding check:
          1. If citation_url is None or empty → return status='unknown',
             notes='no citation URL provided'.
          2. Parse URL. If parse fails → status='unknown'.
          3. Check host (and parent domain) against AGGREGATOR_HOSTS — if the host
             equals or is a subdomain of any aggregator → status='unknown',
             notes='aggregator citation rejected'.
          4. HEAD request via httpx (timeout=fetch_timeout, follow_redirects=True).
             - If response status != 200 → status='unknown',
               notes='citation URL not reachable (HTTP <status>)'.
             - If the FINAL URL host (after redirects) is an aggregator → 
               status='unknown', notes='redirected to aggregator'.
          5. GET the URL (same timeout). Decompress (httpx does this automatically
             for gzip/deflate/br when Accept-Encoding header is sent).
          6. Lowercase the response body. Search for:
             - local_part = email.split('@', 1)[0].lower()
             - domain    = email.split('@', 1)[1].lower()
             Both present in body → status='accepted', confidence='verified-web',
               source_url=citation_url (the ORIGINAL, not the redirected target —
               that's what we cite in EmailRow).
             Only domain present → status='unknown', notes='local-part not on
               citation page'.
             Neither present → status='unknown', notes='neither local-part nor
               domain on citation page'.
          7. On any httpx exception (Timeout, ConnectError, etc.) → status='unknown',
             notes='fetch exc: <type>'. Do NOT raise."""
```

Implementation notes:
- Use `httpx` (synchronous client) — it ships gzip/br decompression and `follow_redirects=True`.
- For the host check, lowercase, strip leading `www.`, and check both `host in AGGREGATOR_HOSTS` and `any(host.endswith("." + a) for a in AGGREGATOR_HOSTS)` to cover subdomains.
- The local-part / domain search is a literal substring match on the lowercased body. Do NOT regex-escape and do NOT use word boundaries (the prior art's research found that breaks too many real pages); pure `in` is fine.
- Known residual risk (documented in `claude-plan.md §5.2`): hallucinated URL pointing to a directory page that happens to contain the local-part by coincidence still passes. Acceptable for v1; multi-source agreement is v2.

### 3.2 Tests — `tests/lib/verifiers/test_web_citation.py`

Use `httpx.MockTransport` or `respx` to mock HTTP. The body fixtures should be tiny strings.

```python
# Test: citation_url=None → status=unknown, notes mentions 'no citation'.
# Test: citation_url='' → status=unknown.
# Test: citation_url='not a url at all' → status=unknown (URL parse handled).

# Aggregator filtering:
# Test: citation_url='https://rocketreach.co/jane' → status=unknown,
#       no HTTP call made (verify mock transport never invoked).
# Test: citation_url='https://subdomain.contactout.com/x' → status=unknown
#       (subdomain match).
# Test: citation_url='https://www.contactout.com/x' → status=unknown
#       (www. stripped before match).

# HEAD non-200:
# Test: HEAD returns 404 → status=unknown, notes='citation URL not reachable
#       (HTTP 404)' (substring match).
# Test: HEAD returns 500 → status=unknown.

# Redirect to aggregator:
# Test: HEAD-200 but the final response URL host is 'apollo.io' →
#       status=unknown, notes mentions 'redirected'.

# Body match (the happy paths):
# Test: HEAD 200, GET body contains both 'aforch' and 'huckberry.com' →
#       status=accepted, confidence='verified-web',
#       source_url=<the original citation URL passed in>.
# Test: HEAD 200, body contains domain only → status=unknown,
#       notes mentions 'local-part'.
# Test: HEAD 200, body contains neither → status=unknown.

# Robustness:
# Test: HEAD/GET raises httpx.TimeoutException → status=unknown, notes
#       contains 'fetch exc'. Verifier does NOT re-raise.
# Test: gzipped response body decompresses correctly — test fixture sends
#       gzip-encoded body containing local-part+domain → status=accepted.
#       (httpx handles this; the test exists to lock in that we don't
#       accidentally pass raw bytes through .lower() and miss matches.)
```

---

## 4. `scripts/lib/verifiers/api_provider.py` — feature-flagged escape hatch

### 4.1 Behavior

```python
class ApiProviderVerifier:
    name = "api_provider"

    def __init__(self, *, provider: Literal["zerobounce", "neverbounce"],
                 api_key: str): ...

    def assert_available(self) -> None:
        """1. If api_key is falsy → raise VerifierUnavailable(
              'ZEROBOUNCE_API_KEY not set in config/secrets.env').
              (Adjust the env-var name for neverbounce.)
           2. Ping provider /health (zerobounce: GET /v2/getcredits?api_key=...;
              neverbounce: GET /v4/account/info?key=...).
              - 401/403 → raise VerifierUnavailable('Invalid <provider> API key').
              - Any other non-2xx → raise VerifierUnavailable('<provider>
                unreachable: HTTP <status>')."""

    def verify(self, email: str, *, citation_url: str | None) -> VerificationResult:
        """Call provider's single-email verify endpoint.
           Map provider responses to VerificationResult:
             zerobounce status 'valid'       → accepted, confidence='verified-api',
                                                source_url='https://zerobounce-api/'.
             zerobounce status 'invalid'     → rejected.
             zerobounce status 'catch-all'   → catchall.
             zerobounce status 'unknown' /
                  'spamtrap' / 'abuse'        → unknown.
             Anything unmapped               → unknown, notes='unmapped:<raw>'.
           Network / 5xx errors              → status='unknown', notes='api exc'.
              (Do not raise; caller's chain continues to the next verifier or to
              the next row.)"""
```

Implementation notes:
- This verifier is OFF by default (`config/verifiers.yaml: api_provider.enabled: false`). The constructor is only called by section-09 when the flag is set.
- API key comes from `config/secrets.env` via `os.environ[...]` (e.g., `ZEROBOUNCE_API_KEY`). The constructor takes the resolved string, not the env-var name.
- The `source_url` sentinel for accepted results is `'https://zerobounce-api/'` (analogous to the SMTP sentinel).
- v1 ships zerobounce wiring as the primary provider; neverbounce is a stub the constructor accepts but `verify()` may raise `NotImplementedError` if invoked. The TEST for the unimplemented branch is optional.

### 4.2 Tests — `tests/lib/verifiers/test_api_provider.py`

```python
# Test: mock provider returns {'status': 'valid'} → status=accepted,
#       confidence='verified-api'.
# Test: mock provider returns {'status': 'invalid'} → status=rejected.
# Test: mock returns 'catch-all' → status=catchall.
# Test: mock returns 'unknown' → status=unknown.
# Test: mock returns unmapped status 'do-not-mail' → status=unknown,
#       notes contains 'unmapped'.

# Pre-flight:
# Test: assert_available with empty api_key → raises VerifierUnavailable whose
#       message names the missing env var.
# Test: assert_available with HTTP 401 from /health → raises VerifierUnavailable
#       with 'Invalid' in the message.
# Test: assert_available with HTTP 200 → no exception.

# Robustness:
# Test: provider raises ConnectionError during verify → status=unknown,
#       notes='api exc'; no exception propagates.

# Feature-flag wiring (will actually run in section-09's test file, listed here
# for traceability):
# Test: verifiers.yaml api_provider.enabled=false → ApiProviderVerifier is NOT
#       instantiated by verify_emails.py.  (Implemented in tests/test_verify_emails.py.)
```

---

## 5. `config/verifiers.yaml` shape (verify against section-01 stub)

If section-01 created this file as a placeholder, ensure its content matches:

```yaml
smtp_probe:
  enabled: true
  rate_per_sec: 0.5        # research-aligned default; brief may override
  per_hour_cap: 50         # under Spamhaus flagging threshold for static IPs
  greylist_retry: true
  timeout: 10.0
web_citation:
  enabled: true
  fetch_timeout: 8.0
api_provider:
  enabled: false
  provider: zerobounce
  # api_key is read from config/secrets.env (ZEROBOUNCE_API_KEY) at runtime;
  # do NOT put it in this file.
```

These are the engine-level defaults. The brief's `verifier:` section may override `rate_per_sec`, `per_hour_cap`, `greylist_retry`, and may set `chain: [smtp_probe, web_citation]` or any other ordering. Section-09 is what reads both files and constructs the chain; this section only ensures the YAML keys exist with the right shape.

---

## 6. `playbooks/04-email-verification.md` — fill in

Replace the stub (created in section-01) with the following sections:

- **Purpose** — why this stage exists; what `emails.csv` is for.
- **When Claude reads this** — when invoking `scripts/verify_emails.py` (section-09).
- **Strategy** — the verifier chain philosophy: primary-source citation first OR SMTP first (brief choice), no aggregator scraping, no pattern-only tier (v1 hard-skips pattern-only rows).
- **The MX tarpit hard-skip** — explain why O365/Proofpoint/Mimecast/PPE always pass RCPT and why we treat any such MX as catchall WITHOUT opening a socket. Cite `TARPIT_MX_PATTERNS` from `smtp_probe.py`.
- **The web-citation grounding rule (HEAD-200 + body contains local-part + domain)** — explain the residual risk and why it's acceptable in v1.
- **Greylist retry** — what 4xx codes are, why 90s, why we only retry once.
- **When SMTP is unavailable** — port 25 blocked off-VPN; the three escape hatches surfaced in the error message (`VerifierUnavailable`): VPN, drop SMTP from the chain, or enable `api_provider`.
- **Common failure modes** — `VerifierUnavailable` on stage start, all-catchall domains, hyper-strict tarpit MX, citation URL behind a paywall (HEAD 200 but body shows login wall and lacks the local-part).
- **Examples** — three worked examples: SMTP-accepted, SMTP-catchall + web-citation-verified, fully unknown row (skipped).

---

## 7. Build order (TDD)

Following the M2 TDD note in `claude-plan-tdd.md §10`:

1. Write `tests/lib/verifiers/test_base.py` → implement `base.py`.
2. Write `tests/lib/verifiers/test_smtp_probe.py` → implement `smtp_probe.py`.
3. Write `tests/lib/verifiers/test_web_citation.py` → implement `web_citation.py`.
4. Write `tests/lib/verifiers/test_api_provider.py` → implement `api_provider.py`.
5. Fill in `playbooks/04-email-verification.md`.

After all four files are green under `uv run pytest tests/lib/verifiers/`, this section is complete. Integration with the actual stage script happens in section-09; the integration test (`tests/test_verify_emails.py`) lives there and is NOT part of this section.

## 8. Acceptance criteria for section-08

- `uv run pytest tests/lib/verifiers/` is green.
- Each verifier exposes `name`, `verify(email, *, citation_url)`, and `assert_available()` with signatures matching the `Verifier` Protocol.
- `VerificationResult` rejects out-of-enum `status` and `confidence` values at validation time.
- `VerifierUnavailable` raised by `smtp_probe.assert_available` contains the substring "Port 25 blocked" and "Dartmouth VPN".
- `VerifierUnavailable` raised by `api_provider.assert_available` for a missing key names the env var.
- The five tarpit MX patterns each short-circuit to `status=catchall` without opening any socket (verified by tests asserting the SMTP mock constructor was NOT invoked).
- `web_citation` rejects all 20+ aggregator hosts AND their subdomains AND any HEAD-redirect that lands on an aggregator host.
- `web_citation` requires BOTH local-part and domain in the page body to return `accepted`.
- All Pydantic models in this section pass `extra="forbid"` round-trip tests (loose-input → `ValidationError`).
- `playbooks/04-email-verification.md` is filled in (no longer a stub).