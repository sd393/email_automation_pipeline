# Purpose

Stage 1 strategy: how to generate diverse seed queries from the brief's segment + include/exclude, how to run the per-query LLM extraction with `web_search`, and how to know when enough domains have been collected (target count reached, or three consecutive queries yield zero new domains). Filled in by section 06.

# When Claude reads this

Before invoking `scripts/source_domains.py` for the first time on a campaign, and again if Stage 1 returns fewer domains than `target.target_domain_count` to plan an additional query batch.
