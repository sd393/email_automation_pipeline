# Manual smoke test — M4 (end-to-end with real bounces)

A manual test the author runs to confirm the full pipeline works against a
real Gmail account and produces the expected `data/suppression.csv` updates.

Prereqs:
- `config/secrets.env` populated (`OPENAI_API_KEY`).
- `config/credentials.json` is a valid Google OAuth2 client.
- Workspace Gmail account; OAuth scope `gmail.send` already authorized.
- Port 25 reachable (Dartmouth VPN if needed for SMTP probes).

Steps:

1. **Create a test campaign.**
   ```
   python scripts/setup_campaign.py --slug 2026-05_smoke
   ```
   Edit `campaigns/2026-05_smoke/brief.yaml`:
   - `target.target_domain_count: 3`
   - Include at least 2 fake domains by manually pre-populating the
     `domains.csv` (see Stage 1 playbook escape hatch) — e.g.,
     `nosuchcompany12345.example.org`, `definitelyfake67890.example.org`.
   - `sending.send_test_count: 3` and `sending.send_rate_per_day: 5`.

2. **Run the pipeline through Stage 4.**
   ```
   python scripts/source_domains.py    --campaign-dir campaigns/2026-05_smoke --resume
   python scripts/discover_contacts.py --campaign-dir campaigns/2026-05_smoke
   python scripts/verify_emails.py     --campaign-dir campaigns/2026-05_smoke
   python scripts/compose_emails.py    --campaign-dir campaigns/2026-05_smoke
   ```

3. **Phase A test send.**
   ```
   python scripts/send_emails.py --campaign-dir campaigns/2026-05_smoke
   ```
   Verify the banner prints and `progress/send_emails.json` shows
   `__phase_a_complete__: {"done": true}`.

4. **Phase B bulk send.**
   ```
   python scripts/send_emails.py --campaign-dir campaigns/2026-05_smoke --confirm-test
   ```

5. **Wait ~5 minutes** for Gmail to receive bounce notifications from the
   fake domains.

6. **Poll bounces.**
   ```
   python scripts/poll_bounces.py
   ```
   First invocation will trigger the documented re-auth flow (token has
   `gmail.send` only; adds `gmail.readonly`). A browser opens; grant
   consent. Output:
   ```
   poll_bounces: examined 2 bounces, added 2 new suppressions, skipped 0 already-suppressed.
   ```

7. **Verify suppression list.**
   ```
   tail data/suppression.csv
   ```
   Expect to see the 2 fake-domain addresses with `reason=hard_bounce` and
   `source` = a real Gmail message ID.

8. **Idempotency check.**
   ```
   python scripts/poll_bounces.py
   ```
   Re-run should print `added 0 new suppressions` (the two are already in
   the file).

If all eight steps pass, M4 is good.
