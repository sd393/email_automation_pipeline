# Playbook: Stage 3 — Email verification

## Purpose

Stage 3 walks each `ContactRow` from Stage 2 through the verifier chain
configured in `brief.verifier.chain` (default `[smtp_probe, web_citation]`)
until one verifier returns `status="accepted"`. Output:
`campaigns/<slug>/emails.csv` — one `EmailRow` per verified address.

## When Claude reads this

- Before invoking `scripts/verify_emails.py`.
- When the verification yield is unexpectedly low (consider enabling
  `api_provider`).
- When `assert_available()` fails on stage start (port 25 blocked, missing
  API key).

## Strategy

The chain is short and ordered. The first verifier that accepts wins; if all
return `unknown`/`catchall`/`rejected`, the row is skipped (Stage 3 hard-skips
pattern-only candidates — the v1 design dropped that tier, and the
discovery stage is told not to invent emails).

Verifiers are stateless aside from internal rate limiters and the DNS LRU
cache. The chain is configured by `config/verifiers.yaml` (engine defaults)
overlaid with `brief.verifier` (campaign overrides).

## MX tarpit hard-skip

O365 (`*.mail.protection.outlook.com`, `*.olc.protection.outlook.com`),
Proofpoint (`*.pphosted.com`, `*.ppe-hosted.com`), and Mimecast
(`*.mimecast.com`) accept every RCPT regardless of recipient — RFC-5321
probes against them produce false positives. `smtp_probe` detects these by
glob-matching the highest-priority MX hostname against
`TARPIT_MX_PATTERNS` and short-circuits to `status="catchall"` WITHOUT
opening a socket. The chain then falls through to `web_citation`.

## Web-citation grounding rule

A citation URL passes `web_citation` only when:

1. The URL is not from a contact-data aggregator (`AGGREGATOR_HOSTS` —
   ~20 sites including RocketReach, ContactOut, ZoomInfo, Apollo, …)
   and doesn't redirect to one.
2. HEAD returns 200.
3. The GETted body, lowercased, contains BOTH the local-part and the
   domain as literal substrings.

Residual risk (documented in the design doc): a hallucinated URL pointing
to a directory page that happens to contain the local-part by coincidence
still passes. Multi-source agreement is v2.

## Greylist retry

If the candidate RCPT returns a 4xx code and `brief.verifier.greylist_retry`
is true, the verifier sleeps 90 seconds and probes once more. A 4xx on the
retry is recorded as `status="unknown"`; a 250 on the retry is `accepted`.
We do not loop indefinitely — 90s is a single retry window, not a backoff
schedule.

## When SMTP is unavailable

`SmtpProbeVerifier.assert_available()` opens a TCP connection to
`gmail-smtp-in.l.google.com:25`. If port 25 is blocked (most residential
networks, most coffee shops), the pre-flight raises `VerifierUnavailable`
with this message:

```
Port 25 blocked. Connect to Dartmouth VPN, or set verifier.chain to
["web_citation"] in the brief, or enable api_provider.
```

The caller (Stage 3) prints this verbatim and exits 2. Three escape
hatches:

1. Connect to a VPN that allows outbound 25.
2. Set `brief.verifier.chain: [web_citation]` and re-run.
3. Set `config/verifiers.yaml: api_provider.enabled: true`, populate
   `ZEROBOUNCE_API_KEY` in `config/secrets.env`, and add `api_provider`
   to the chain.

## Common failure modes

- **VerifierUnavailable on stage start.** Print the message verbatim;
  pick one of the three escape hatches.
- **All-catchall domains.** Domain accepts every RCPT (e.g., the
  domain runs an O365 tenant). `smtp_probe` flags it, `web_citation`
  may still accept if the contact appears on a real page; otherwise
  the row is skipped.
- **Hyper-strict tarpit MX.** Always `catchall`. Same handling as above.
- **Citation URL behind a paywall.** HEAD 200 but the body shows a
  login wall and lacks the local-part. `web_citation` returns
  `status="unknown"` with note "local-part not on citation page".

## Worked examples

**SMTP-accepted:** brief chain = `[smtp_probe, web_citation]`. The probe
gets 250 to candidate + 550 to random → `accepted`,
`confidence="verified-smtp"`. EmailRow written, chain stops.

**SMTP-catchall → web-citation-verified:** the probe sees tarpit MX →
`catchall`. Falls through to `web_citation`; HEAD-200 on the citation
URL; body contains both local-part and domain → `accepted`,
`confidence="verified-web"`. EmailRow written.

**Fully unknown:** both verifiers return `unknown`. No EmailRow written;
progress marks the row "unknown" so `--resume` can re-attempt (e.g., after
manually adding a stronger citation URL).
