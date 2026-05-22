# Purpose

Stage 3 strategy: walk the verifier chain for each candidate email (default `smtp_probe` → `web_citation`), handle catch-all domains, hard-skip tarpit MX hosts (O365 / Proofpoint / Mimecast), and respect greylisting retries. Documents how to interpret each verifier's `verdict` and `confidence`. Filled in by section 08.

# When Claude reads this

Before invoking `scripts/verify_emails.py`, and when troubleshooting a campaign whose verification yield is unexpectedly low (e.g., to decide whether to enable the `api_provider` verifier).
