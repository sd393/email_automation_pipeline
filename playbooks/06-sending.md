# Purpose

Stage 5 strategy and test-batch philosophy: Phase A sends the first `send_test_count` real recipients (default 10), then STOPS. The user reviews the live sends in their Gmail Sent folder. Phase B requires `--confirm-test` and resumes from row 11. Documents pessimistic daily-counter behavior, throttle jitter, and the `.send.pid` lockfile model. Filled in by section 11.

# When Claude reads this

Before invoking `scripts/send_emails.py` for Phase A, and again at the Phase A → Phase B approval gate to remind itself to stop and ask the user.
