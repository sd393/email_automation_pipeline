# Purpose

Stage 2 strategy: for each sourced domain, query the web for contacts matching `who_to_contact.priority_roles`, extract structured contact rows via LLM, dedup against prior campaigns, and respect the per-company contact cap. Filled in by section 07.

# When Claude reads this

Before invoking `scripts/discover_contacts.py`, especially when adjusting the priority-role list mid-campaign or when reviewing the failure-budget report after a partial run.
