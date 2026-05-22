# Purpose

Stage 6 strategy (bounce-only in v1): poll Gmail for delivery failure notifications (MAILER-DAEMON, 5xx SMTP DSNs), tag those recipients in the suppression list, and surface a summary to the user. Manual follow-up notes (which addresses to retry by hand, which to give up on) round out the playbook. Filled in by section 12.

# When Claude reads this

A few hours after Phase B completes (Stage 5), to poll for bounces; and again the next day to update the suppression list before any subsequent campaign reuses the engine.
