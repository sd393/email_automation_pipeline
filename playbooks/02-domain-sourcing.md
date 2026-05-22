# Playbook: Stage 1 — Domain sourcing

## Purpose

Stage 1 turns a brief's segment description into a deduped list of company
domains that can plausibly receive email. Output:
`campaigns/<slug>/domains.csv` — one row per accepted domain, columns from
`DomainRow` in `scripts/lib/csv_schema.py`. The script terminates once
`brief.target.target_domain_count` is reached or the query list is exhausted.

## When Claude reads this

- At Stage 1 start: skim the strategy hierarchy below, then invoke
  `scripts/source_domains.py --campaign-dir <dir>`.
- When `status.md` shows `source = COMPLETED` but the row count is well below
  the target: skim "Common failure modes" and decide whether to accept the
  result, edit the brief and start a fresh campaign, or manually pre-write
  seed rows and re-run with `--resume`.
- When a single query consistently returns 0 retailers across runs: skim
  "Common failure modes / aggregator-only results".

## Strategy hierarchy

1. **LLM query generation.** `SEARCH_QUERY_PROMPT` is fed `target.segment`,
   `target.include`, `target.exclude`, `target.geography`. The model returns
   ~15 diverse queries each tagged with a `sub_segment`.
2. **Per-query extraction with hosted `web_search`.** For each query, the
   script calls `responses.parse` with `tools=[{"type": "web_search"}]` and a
   strict `DomainExtractionResponse` schema. Every returned item must
   include a non-empty `source_url` — the prompt rejects ungrounded findings.
3. **Filter + dedup + DNS validate.** In order: skip `is_excluded=true`,
   normalize the domain (`normalize_domain`), drop within-run duplicates,
   drop cross-campaign known domains (only when `safety.scope=all_campaigns`),
   drop suppressed, drop domains where `dns_check.has_mail` returns False
   (covers no-MX-no-A and RFC 7505 null MX).

We rely on hosted `web_search` rather than direct scraping because OpenAI
handles robots.txt, rate-limits, and rendering, and the strict schema keeps
the data shape predictable.

## Hyper-narrow segments

When a brief targets a segment so narrow that LLM query generation cannot
surface enough candidates (e.g., a regional sub-niche), the v1 escape hatch
is manual pre-population:

1. Drop seed rows into `campaigns/<slug>/domains.csv` matching the
   `DomainRow` schema (include the header if the file does not exist).
2. Invoke `scripts/source_domains.py --campaign-dir <dir> --resume`.

v1 does not support a `target.seed_urls` brief field. Treat it as a TODO.

## Common failure modes

- **All queries return aggregator pages.** `is_excluded=true` rate spikes.
  Read `activity.log` for the rejected items. Tighten `target.exclude` in
  the brief and start a fresh campaign.
- **Target undermet (queries exhausted).** Exit 0 with a status note in
  `status.md` and an activity-log warn line. The user decides whether
  e.g. 1,200/5,000 is enough.
- **Target overshoots.** Capped at exactly `target_domain_count`. The last
  query may be partially consumed.
- **LLM refusal on every query.** Both tier1 and tier2 refused. Almost
  always a prompt-injection-shaped segment description. Refine
  `target.segment`.

## Worked examples

**Medium retailers (US + Canada):**
- Segment: "Medium-sized multi-brand retailers"
- Include: curated marketplaces; hybrid retailer-brands
- Exclude: pure single-brand DTC; enterprise (>$500M rev)
- Geography: US + Canada

Expected queries: "best multi-brand independent retailers 2025", "curated
US marketplaces apparel", "venture-funded retailers Series A B 2024".

**Boutique hotels (DACH):**
- Segment: "Independent boutique hotels"
- Include: 4–5 star; owner-operated; <50 rooms
- Exclude: chains; hostels; OTAs
- Geography: Germany + Austria + Switzerland

Expected queries: "best boutique hotels Berlin 2025", "owner-operated
4 star DACH", "design hotels guild Switzerland".

## Out of v1 scope

Do not extend Stage 1 with Brave/Tavily/Serper, an LLM cache, geo-filtering
beyond `target.geography`, or a domain-level suppression list.
