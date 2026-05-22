I have enough context now to generate the section content.

# section-06-source-domains

## Scope and goal

This section implements Milestone M1 of the outreach-bot pipeline: **Stage 1, domain sourcing**. The deliverable is a single CLI script, `scripts/source_domains.py`, plus its companion playbook, `playbooks/02-domain-sourcing.md`. The script reads a validated `brief.yaml` from a campaign folder and writes a `domains.csv` containing ~`target.target_domain_count` deduped, DNS-validated domains in the brief's segment. The script is fully parameterized by the brief — no segment-specific values are hardcoded.

After this section, M1 is complete. The no-op stage from section-05 should be deleted at the very start of this section.

## Dependencies (do not re-implement)

This section assumes the following sections are already merged and green:
- **section-01-skeleton-and-config**: `pyproject.toml`, `config/defaults.yaml`, `config/verifiers.yaml`, `templates/_brief_template.yaml`, `.gitignore`, empty playbook stubs.
- **section-02-lib-foundations**: `lib/brief.py` (`Brief`, `load`, `BriefValidationError`), `lib/csv_schema.py` (`DomainRow`, `write_csv_row`, `read_csv`), `lib/progress.py` (`ProgressStore`, `write_brief_hash`, `check_brief_hash`), `lib/rate_limit.py`, `lib/dns_check.py` (`has_mail`, `is_null_mx`, `mx_records`).
- **section-03-lib-observability**: `lib/observability.py` (`CampaignObserver`, `StageObserver`), `lib/dedup.py` (`Deduper` with `is_suppressed`, `is_known`, `fcntl.flock` model).
- **section-04-lib-llm-and-gmail**: `lib/llm.py` (`LLMClient`, `ParseResult`, `CostReport`, `cascade`, `parse`) — used here with the hosted `web_search` tool and structured outputs.
- **section-05-noop-and-orchestration**: `scripts/run_pipeline.py`, `scripts/status.py`, the brief-hash invariant helpers, and `scripts/noop_stage.py`. The noop stage is deleted in this section's first task.

Do **not** redefine any of those libraries here — import from them.

## Background context (so this section is self-contained)

### What Stage 1 does, conceptually
1. Reads `brief.yaml` (loaded by `lib.brief.load`).
2. Generates ~10–30 LLM search queries from `brief.target.segment` + `brief.target.include` + `brief.target.geography`.
3. For each query, calls OpenAI `responses.parse(...)` with the hosted `web_search` tool, requesting a strict-mode `DomainExtractionResponse`.
4. Filters out LLM-flagged exclusions, lowercases + strips the domain, runs cross-campaign + suppression dedup, and validates MX records.
5. Writes accepted rows to `domains.csv` and ticks the observer.
6. Stops when `target.target_domain_count` is reached or the query list is exhausted.

### Locked invariants (apply to this section)
- **Pydantic schema rules**: every model declared in this section uses `model_config = ConfigDict(extra="forbid")` and every `Optional[X]` field has `default=None`. The `source_url` field on `DomainExtractionItem` is required and non-null (hallucination guard). The strict-mode test in `tests/lib/test_csv_schema.py` from section-02 will exercise these schemas; make sure they pass it.
- **Exit codes**: 0 success, 1 refused operation (not used here), 2 stage failure / halt, 3 brief validation error (structured JSON on stderr).
- **Brief-hash invariant**: if `progress/brief_hash.txt` does not exist, write it on first run. If it exists and disagrees with `sha256(brief.yaml file bytes)`, exit 2 with the documented remediation message.
- **Concurrency**: this script is **single-threaded** in M1. Do not introduce `ThreadPoolExecutor`. The single-writer convention (`progress.mark` + `csv_schema.write_csv_row` from the main thread only) is mandatory.
- **Observability**: stage name is `"source"`. Use `CampaignObserver` + a `StageObserver(stage="source")`. Transient retries call `obs.event(level="warn")`. Terminal failures call `obs.finish(status="FAILED")` then re-raise / exit 2. There is no `event(level="error")`.
- **Error taxonomy**: 429 / 5xx / timeouts → retry with exp-backoff inside `lib.llm` (already handled). LLM refusal at tier1 → escalate to tier2; if tier2 also refuses or returns empty → mark progress `search_fail`, log warn, continue with next query. 401/403 → halt the stage.

## Files to create / modify

### Delete first
- `scripts/noop_stage.py` — delete at the start of this section. It was the M0 plumbing-verifier; its job is done.
- `tests/test_noop_stage.py` — delete with it (per user CLAUDE.md, clean up scratch tests after the feature is committed).

### Create
- `scripts/source_domains.py` — the stage script (this section's main deliverable).
- `tests/test_source_domains.py` — the test file.
- `playbooks/02-domain-sourcing.md` — fill in (stub from section-01 already exists).

### Modify
- `pyproject.toml` — add `outreach-source-domains = "scripts.source_domains:main"` under `[project.scripts]` if section-01 didn't already include the placeholder.

## Implementation details

### CLI surface

```
python scripts/source_domains.py \
  --campaign-dir campaigns/<slug> \
  [--resume]
```

`--campaign-dir` is required and must contain a `brief.yaml`. `--resume` reuses any rows already in `domains.csv` and skips any progress keys already marked terminal.

Outputs (all under `<campaign-dir>/`):
- `domains.csv` — appended via `lib.csv_schema.write_csv_row`. Columns are exactly `DomainRow`'s fields.
- `progress/source_domains.json` — written by `ProgressStore("progress/source_domains.json")`.
- `progress/brief_hash.txt` — written on first run (handled by the helper from section-05).
- `status.md` / `activity.log` — driven by the observer.

### Internal LLM schemas (strict-mode-compliant)

Declare these inside `scripts/source_domains.py` (not in `lib/csv_schema.py`, since they are stage-local LLM-response shapes, not persisted CSV rows):

```python
class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    sub_segment: str

class SearchQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[SearchQuery]

class DomainExtractionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: str
    domain: Optional[str] = None
    domain_inferred: bool
    is_excluded: bool
    exclude_reason: Optional[str] = None
    category: str
    source_url: str          # REQUIRED, non-null (hallucination guard)
    notes: str

class DomainExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retailers: list[DomainExtractionItem]
```

Both response models must round-trip through the strict-mode check in `tests/lib/test_csv_schema.py` (or an equivalent local assertion) — add them to that test's iteration set.

### Prompts

Two module-level constants in `scripts/source_domains.py`:

- `SEARCH_QUERY_PROMPT` — takes `target.segment`, `target.include` (bullet list), `target.exclude` (bullet list), and `target.geography`, and asks the model for ~15 diverse queries with a `sub_segment` label. Output shape: `SearchQueryResponse`.
- `DOMAIN_EXTRACTION_PROMPT` — takes one `SearchQuery` plus the same `include`/`exclude` bullets, and instructs the model to use the hosted `web_search` tool to return up to 15 retailers per query. **Every item MUST include a `source_url`** — emphasize this in the prompt. The model is told to set `is_excluded=true` and fill `exclude_reason` when a candidate clearly violates the exclude rules; do not just silently drop them (lets us audit the filter at debug time).

Prompt strings should be plain Python triple-quoted constants with `{}`-style format placeholders. Use `str.format(**brief_fields)` or `string.Template` — no Jinja dependency.

### Domain normalization

Implement a small helper inside the script (no need to lift to `lib/`):

```python
def normalize_domain(raw: str) -> str | None:
    """Lowercase, strip scheme, strip 'www.', drop path/query/fragment.
    Returns None if the string doesn't yield a syntactically plausible
    domain (no dot, all whitespace, etc.)."""
```

Examples (must be covered by tests):
- `"Https://Www.RetailerX.com/path?q=1"` → `"retailerx.com"`.
- `"  https://shop.example.co.uk/  "` → `"shop.example.co.uk"`.
- `"not a url"` → `None`.

### Main loop sketch

```python
def main() -> int:
    args = parse_args()
    campaign_dir = Path(args.campaign_dir)

    # Brief load (exit 3 on validation error — structured JSON on stderr).
    try:
        brief = lib.brief.load(campaign_dir / "brief.yaml")
    except lib.brief.BriefValidationError as e:
        print_brief_error_json(e, campaign_dir / "brief.yaml")
        return 3

    # Brief-hash invariant (helper from section-05).
    if not check_brief_hash(campaign_dir, brief):
        return 2  # message already printed by helper

    # Observability setup.
    campaign_obs = CampaignObserver(campaign_dir)
    obs = StageObserver(campaign_obs, stage="source",
                        cadence_items=50, cadence_seconds=120)
    obs.stage_start()

    # Progress + dedup.
    progress = ProgressStore(campaign_dir / "progress" / "source_domains.json")
    progress.load()
    deduper = Deduper(scope=brief.target.dedup_scope)  # field name per brief schema
    deduper.load_global()

    # LLM client (uses defaults; tier1 = gpt-4.1-mini).
    llm = LLMClient()

    try:
        # 1. Generate queries.
        queries = generate_queries(llm, brief, obs)

        # 2. Iterate queries until target reached or queries exhausted.
        rows_written = count_existing_rows(campaign_dir / "domains.csv")
        target = brief.target.target_domain_count
        seen_domains_in_run: set[str] = load_existing_domains(campaign_dir / "domains.csv")

        for q in queries:
            if rows_written >= target:
                break
            if progress.is_done(q.query):
                continue
            rows_written += process_query(
                llm, q, brief, deduper, seen_domains_in_run,
                progress, obs, campaign_dir, remaining=target - rows_written,
            )

        # 3. Finalize.
        summary = {"rows": rows_written, "target": target,
                   "queries_used": len(queries), "cost": obs.total_cost()}
        if rows_written < target:
            obs.event(f"queries exhausted with {rows_written}/{target} rows",
                      level="warn")
        obs.finish(status="COMPLETED", summary=summary)
        return 0

    except (AuthenticationError, PermissionDeniedError) as e:
        obs.finish(status="FAILED", summary={"error": repr(e)})
        return 2
    except Exception:
        obs.finish(status="FAILED", summary={})
        raise
```

Notes on the sketch:
- `process_query` is the per-query worker. It calls `llm.cascade(messages, text_format=DomainExtractionResponse, tools=[{"type": "web_search"}])`. On refusal at both tiers OR an empty `parsed`, it calls `progress.mark(q.query, "search_fail", ...)`, `obs.event(level="warn", ...)`, and returns 0.
- Filtering order inside `process_query` for each `DomainExtractionItem`:
  1. `if item.is_excluded: continue` (record in `notes` if useful).
  2. `dom = normalize_domain(item.domain)`; if `None`, skip.
  3. `if dom in seen_domains_in_run: continue` (within-run dedup).
  4. `if deduper.is_known(dom): continue` (cross-campaign dedup — only fires when scope=all_campaigns; see §2.4 invariant).
  5. `if deduper.is_suppressed(dom): continue` (email-level suppression doesn't normally fire here, but a future domain-level suppression list would).
  6. `if not dns_check.has_mail(dom): continue` (covers both no-MX-no-A and RFC 7505 null MX).
  7. Build `DomainRow(...)` from the item + normalized domain; `write_csv_row(...)`; `seen_domains_in_run.add(dom)`; `obs.tick({"rows": rows_written, "cost": obs.total_cost()})`.
- Cap rows by `remaining` so we never write more than `target_domain_count` even if the last query overshoots.
- Mark the query as terminal in progress: `progress.mark(q.query, "ok", n_added=k)` (or `search_fail` on LLM failure). This is what makes `--resume` skip processed queries.

### Brief schema fields this section relies on

If section-02's `brief.py` doesn't already expose them, add them to the relevant section models (with the rest of section-02's review still in scope). Required by this script:
- `brief.target.segment: str`
- `brief.target.include: list[str]`
- `brief.target.exclude: list[str]`
- `brief.target.geography: str`
- `brief.target.target_domain_count: int` (validator: > 0)
- `brief.target.dedup_scope: Literal["this_campaign","all_campaigns"]`

If any of these are not present in section-02's `Brief`, file a follow-up — but for this section assume they are, since section-02 is a strict dependency.

### Playbook: `playbooks/02-domain-sourcing.md`

Fill in the existing stub from section-01. Required sections:
1. **Purpose** — what Stage 1 produces and why.
2. **When Claude reads this** — at Stage 1 start, and when a query returns 0 retailers, and when the target is undermet.
3. **Strategy hierarchy** — query generation first, then per-query extraction with hosted `web_search`, then DNS validation. Why we rely on `web_search` over direct scraping (no rate-limit / robots.txt drama; OpenAI handles it).
4. **Hyper-narrow segments** — escape hatch: user can paste seed URLs into `brief.notes`; Stage 1 in v1 does NOT auto-consume them, but Claude Code can manually pre-write rows to `domains.csv` before invoking the script.
5. **Common failure modes** — (a) all queries return aggregator pages → `is_excluded=true` rate spikes; (b) target undermet → exit 0 with a status note (not an error); (c) target overshoots — capped at `target_domain_count` exactly.
6. **Examples** — two worked examples: medium retailers (US) and boutique hotels (DACH). Both should produce sensible queries from the same prompt.

Out-of-v1-scope items to **avoid mentioning** as if they exist: Brave search, LLM cache, pattern-only tier, geo filtering, domain-level suppression list.

## Tests (TDD — write these first)

All tests in `tests/test_source_domains.py`. Use `pytest`, share fixtures with `tests/conftest.py` from section-02 (`sample_brief`, `tmp_campaign_dir`, `fake_dns`). Mock the LLM by patching `scripts.source_domains.LLMClient` with a fake that returns canned `ParseResult(parsed=..., refused=..., cost=...)` objects. Do not call the real OpenAI API in tests.

The test stubs below are the **complete** test list extracted from the TDD plan. Each is a function definition with a docstring stating the assertion; the body is the implementer's job.

```python
# tests/test_source_domains.py
"""Tests for scripts/source_domains.py (Stage 1, M1)."""

# --- Happy path ----------------------------------------------------------
def test_happy_path_20_domains(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """Brief with target_domain_count=20; LLM returns 5 retailers per query x 4
    queries -> domains.csv contains exactly 20 unique rows; exit 0."""

# --- Filter / dedup ------------------------------------------------------
def test_excluded_rows_dropped(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """LLM returns 3 rows with is_excluded=true -> those rows absent from
    domains.csv."""

def test_within_run_dedup(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """LLM returns 'huckberry.com' three times across different queries -> one
    row in domains.csv."""

def test_cross_campaign_dedup_all_scope(tmp_campaign_dir, sample_brief, fake_llm,
                                        fake_dns, populated_master_contacts):
    """Brief.dedup_scope=all_campaigns; 'huckberry.com' already in
    data/master_contacts.csv -> excluded from output."""

def test_cross_campaign_dedup_this_scope(tmp_campaign_dir, sample_brief, fake_llm,
                                         fake_dns, populated_master_contacts):
    """Same scenario as above but dedup_scope=this_campaign -> 'huckberry.com'
    IS included in output."""

# --- DNS -----------------------------------------------------------------
def test_dns_no_mail(tmp_campaign_dir, sample_brief, fake_llm, monkeypatch):
    """has_mail() returns False for 'fake.example' -> row dropped."""

def test_dns_null_mx(tmp_campaign_dir, sample_brief, fake_llm, monkeypatch):
    """is_null_mx() returns True for a domain -> row dropped (has_mail
    short-circuits)."""

# --- LLM behavior --------------------------------------------------------
def test_llm_429_then_success(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """First parse call raises 429-equivalent, second succeeds; LLMClient's
    own retry handles it; final domains.csv unchanged from happy-path counts."""

def test_llm_refusal_marks_search_fail(tmp_campaign_dir, sample_brief, fake_llm,
                                       fake_dns):
    """Both tier1 and tier2 return refused=True for one query -> that query's
    progress key has status='search_fail', no rows from that query in output,
    other queries still processed."""

def test_llm_empty_cascade(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """tier1 returns parsed=None, refused=False -> cascade tries tier2; if
    tier2 also returns parsed=None -> search_fail; if tier2 succeeds -> rows
    from tier2 written."""

# --- Resume --------------------------------------------------------------
def test_resume_after_kill(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """Run, kill after ~10 rows written, invoke again with --resume -> final
    domains.csv identical (rowwise) to a non-killed run; no duplicate rows;
    progress keys preserved."""

# --- Observability -------------------------------------------------------
def test_milestone_at_50_rows(tmp_campaign_dir, sample_brief, fake_llm,
                              fake_dns, capsys):
    """After 50 rows written, exactly one milestone line printed to stdout and
    one appended to activity.log."""

def test_status_md_counters(tmp_campaign_dir, sample_brief, fake_llm, fake_dns):
    """After a complete run, status.md contains a 'Domains sourced: X / Y' line
    where X equals the row count of domains.csv and Y equals
    target_domain_count."""

# --- Normalization -------------------------------------------------------
def test_domain_normalization():
    """normalize_domain('Https://Www.RetailerX.com/path?q=1') == 'retailerx.com'.
    normalize_domain('  https://shop.example.co.uk/  ') == 'shop.example.co.uk'.
    normalize_domain('not a url') is None."""

# --- Termination ---------------------------------------------------------
def test_target_reached_caps_output(tmp_campaign_dir, sample_brief, fake_llm,
                                    fake_dns):
    """target_domain_count=1500; LLM would yield 2000 unique domains -> exactly
    1500 rows in output; later queries not processed."""

def test_queries_exhausted_target_undermet(tmp_campaign_dir, sample_brief,
                                           fake_llm, fake_dns):
    """target_domain_count=5000; LLM yields only 1200 unique across all queries
    -> 1200 rows in output, status.md notes 'queries exhausted', exit code 0."""

# --- Pre-flight ----------------------------------------------------------
def test_missing_brief_exits_3(tmp_campaign_dir, capsys):
    """No brief.yaml in campaign-dir -> exit 3, structured JSON on stderr."""

def test_invalid_brief_exits_3(tmp_campaign_dir, capsys):
    """brief.yaml fails Pydantic validation -> exit 3, JSON on stderr names
    the offending field."""

def test_brief_hash_mismatch_exits_2(tmp_campaign_dir, sample_brief, fake_llm,
                                     fake_dns):
    """Write progress/brief_hash.txt with a stale hash, then run -> exit 2
    with the documented 'Brief changed' remediation message."""

# --- Concurrency model ---------------------------------------------------
def test_single_threaded_writer(tmp_campaign_dir, sample_brief, fake_llm,
                                fake_dns):
    """Confirm via instrumentation (e.g., assert `threading.active_count() == 1`
    inside write_csv_row, or assert no ThreadPoolExecutor is referenced in
    scripts.source_domains) that M1's source_domains.py is single-threaded."""
```

### Fixture helpers needed (add to `tests/conftest.py` if missing)

- `fake_llm` — a fixture that yields a stub `LLMClient` with a `set_responses(queue: list[ParseResult])` method. Tests pre-load the queue with the canned results they need.
- `fake_dns` — monkeypatches `lib.dns_check.has_mail` and `lib.dns_check.is_null_mx` to return user-controlled values.
- `populated_master_contacts` — writes `data/master_contacts.csv` with a couple of known rows before the test runs.

Tests must clean up `data/` and `campaigns/` fixtures after running (tmp_path-based dirs).

## Acceptance criteria (must all hold before this section is merged)

1. `pytest tests/test_source_domains.py` is green.
2. The strict-mode schema test from section-02 (`tests/lib/test_csv_schema.py`) now also exercises `SearchQuery`, `SearchQueryResponse`, `DomainExtractionItem`, `DomainExtractionResponse` and passes.
3. Two different briefs (e.g., a "medium retailers" fixture and a "boutique hotels" fixture, both checked into `tests/fixtures/`) produce sensible `domains.csv` files with **no code changes** between runs — only brief changes.
4. Live `status.md` shows incremental progress while the script runs; the file contains a `Domains sourced: N / target` line.
5. `Ctrl-C` mid-run followed by `--resume` produces a byte-equivalent `domains.csv` (modulo row order — the implementer may choose to sort on a stable key if order matters) compared to a non-killed run.
6. `scripts/noop_stage.py` and `tests/test_noop_stage.py` are deleted; `git grep noop_stage` returns nothing.
7. `playbooks/02-domain-sourcing.md` is filled in per the structure above (not a stub).
8. `python scripts/status.py --campaign-dir <dir>` (from section-05) correctly reports `source=COMPLETED` after a successful run.
9. Exit codes follow the locked taxonomy: 0 on success (even when target undermet but queries exhausted), 2 on brief-hash mismatch / auth halt, 3 on brief validation error.

## Out of scope for this section

Do not implement here (they belong to later sections):
- Contact discovery / `scripts/discover_contacts.py` (section-07).
- Any verifier logic (section-08, section-09).
- Domain-level suppression list — v1 only has email-level (and `Deduper.is_suppressed` at the domain level is effectively a no-op for now; the call remains as a future hook).
- Seed-URL ingestion as a brief field — left as a TODO in v1 per the design doc; the playbook documents the manual escape hatch.
- Brave / Tavily / Serper search backends — hosted OpenAI `web_search` only.

Relevant absolute paths the implementer will touch:
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/source_domains.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/test_source_domains.py`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/playbooks/02-domain-sourcing.md`
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/scripts/noop_stage.py` (delete)
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/tests/test_noop_stage.py` (delete)
- `/Users/Alphastar/Documents/Code/Spring_2026/email_automation/pyproject.toml` (add console-script entry if not already present)