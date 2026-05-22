"""Stage 1: domain sourcing (M1).

Reads a validated ``brief.yaml``, generates ~15 LLM search queries, calls
OpenAI ``responses.parse`` with the hosted ``web_search`` tool per query,
filters/dedups/DNS-validates the extracted retailers, and writes accepted rows
to ``domains.csv`` until ``target.target_domain_count`` is reached.

Single-threaded by design. The LLM-retry / cascade behavior lives entirely in
``scripts.lib.llm.LLMClient``; this script only orchestrates.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from scripts.lib import dns_check
from scripts.lib.brief import Brief, BriefValidationError, emit_brief_error_and_exit, load
from scripts.lib.csv_schema import DomainRow, write_csv_row
from scripts.lib.dedup import Deduper
from scripts.lib.llm import LLMClient
from scripts.lib.observability import CampaignObserver, StageObserver
from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash


# ---------------------------------------------------------------------------
# LLM-response schemas (strict-mode-compliant)
# ---------------------------------------------------------------------------

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
    source_url: str
    notes: str


class DomainExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retailers: list[DomainExtractionItem]


ALL_LLM_MODELS = (SearchQuery, SearchQueryResponse, DomainExtractionItem, DomainExtractionResponse)


SEARCH_QUERY_PROMPT = """\
Generate ~15 diverse web-search queries to find companies matching this segment:

Segment: {segment}
Include: {include}
Exclude: {exclude}
Geography: {geography}

Each query should be a specific phrase a human would type into Google to surface
listings, directories, or aggregator articles about this segment. Vary the
angle: some queries should target listicles, some funding databases, some
press releases. Tag each with a short ``sub_segment`` label.
"""

DOMAIN_EXTRACTION_PROMPT = """\
Use web_search to find up to 15 companies matching the query.

Query: {query}
Sub-segment: {sub_segment}
Include: {include}
Exclude: {exclude}
Geography: {geography}

For each company, return:
  - company_name
  - domain (lowercase, no scheme, no www., no path)
  - domain_inferred (true if you inferred the domain from the company name)
  - category (your label, e.g., "premium retailer", "marketplace")
  - source_url (the EXACT URL where you found this company; REQUIRED, never empty)
  - notes (any context)
  - is_excluded + exclude_reason if the company clearly violates the exclude rules

EVERY item MUST include a non-empty source_url. If you cannot ground a finding
to a specific URL, do not return it.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_domain(raw: str | None) -> str | None:
    """Lowercase, strip scheme, strip www., drop path/query/fragment.

    Returns None if the string doesn't yield a syntactically plausible domain.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    parsed = urlparse(s)
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or " " in host:
        return None
    return host


def _format_list(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "; ".join(items)


def _load_existing_domains(domains_csv: Path) -> set[str]:
    if not domains_csv.exists():
        return set()
    seen = set()
    with domains_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = (row.get("domain") or "").lower()
            if d:
                seen.add(d)
    return seen


def _emit_hash_mismatch(progress_dir: Path, brief_path: Path, brief_bytes: bytes) -> None:
    expected_path = progress_dir / "brief_hash.txt"
    expected = expected_path.read_text(encoding="utf-8").strip() if expected_path.exists() else "<none>"
    import hashlib
    found = hashlib.sha256(brief_bytes).hexdigest()
    sys.stderr.write(
        "Brief changed since previous stage. Either revert brief.yaml or start a fresh\n"
        "campaign in a new directory.\n\n"
        f"Expected hash: {expected}\n"
        f"Found hash:    {found}\n"
        f"Brief path:    {brief_path}\n"
    )


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

def generate_queries(llm: LLMClient, brief: Brief, obs: StageObserver) -> list[SearchQuery]:
    prompt = SEARCH_QUERY_PROMPT.format(
        segment=brief.target.segment,
        include=_format_list(brief.target.include),
        exclude=_format_list(brief.target.exclude),
        geography=brief.target.geography,
    )
    messages = [{"role": "user", "content": prompt}]
    result = llm.cascade(messages, text_format=SearchQueryResponse)
    if result.refused or result.parsed is None:
        obs.event("query generation failed; using no queries", level="warn")
        return []
    return list(result.parsed.queries)


def process_query(
    llm: LLMClient,
    query: SearchQuery,
    brief: Brief,
    deduper: Deduper,
    seen_domains: set[str],
    domains_csv: Path,
    progress: ProgressStore,
    obs: StageObserver,
    remaining: int,
    total_cost: list[float],
) -> int:
    prompt = DOMAIN_EXTRACTION_PROMPT.format(
        query=query.query,
        sub_segment=query.sub_segment,
        include=_format_list(brief.target.include),
        exclude=_format_list(brief.target.exclude),
        geography=brief.target.geography,
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search"}]
    result = llm.cascade(messages, text_format=DomainExtractionResponse, tools=tools)
    total_cost[0] += result.cost.usd

    if result.refused or result.parsed is None:
        progress.mark(query.query, "search_fail", reason=result.refusal_text or "empty")
        obs.event(f"query refused/empty: {query.query!r}", level="warn")
        return 0

    n_added = 0
    for item in result.parsed.retailers:
        if n_added >= remaining:
            break
        if item.is_excluded:
            continue
        dom = normalize_domain(item.domain) if item.domain else None
        if dom is None and item.domain_inferred and item.company_name:
            dom = normalize_domain(item.company_name.replace(" ", "") + ".com")
        if dom is None:
            continue
        if dom in seen_domains:
            continue
        if deduper.is_known(dom):
            continue
        if deduper.is_suppressed(dom):
            continue
        if not dns_check.has_mail(dom):
            continue
        row = DomainRow(
            company_name=item.company_name,
            domain=dom,
            domain_inferred=item.domain_inferred,
            category=item.category,
            source_url=item.source_url,
            notes=item.notes,
        )
        write_csv_row(domains_csv, row)
        seen_domains.add(dom)
        n_added += 1
        obs.tick({"rows": len(seen_domains)}, cost=total_cost[0])

    progress.mark(query.query, "ok", n_added=n_added)
    return n_added


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _run(campaign_dir: Path, resume: bool, llm: LLMClient | None = None) -> int:
    obs: StageObserver | None = None
    try:
        brief_path = campaign_dir / "brief.yaml"
        brief_bytes = brief_path.read_bytes() if brief_path.exists() else b""
        try:
            brief = load(brief_path)
        except BriefValidationError as e:
            emit_brief_error_and_exit(e)
        except FileNotFoundError:
            raise BriefValidationError(
                field="<root>", message="brief.yaml not found", brief_path=brief_path
            )

        progress_dir = campaign_dir / "progress"
        progress_dir.mkdir(parents=True, exist_ok=True)
        if not check_brief_hash(progress_dir, brief_bytes):
            _emit_hash_mismatch(progress_dir, brief_path, brief_bytes)
            return 2
        write_brief_hash(progress_dir, brief_bytes)

        campaign_obs = CampaignObserver(campaign_dir)
        obs = StageObserver(campaign_obs, stage="source", cadence_items=50, cadence_seconds=120)
        obs.stage_start()

        progress = ProgressStore(progress_dir / "source_domains.json")
        progress.load()

        deduper = Deduper(scope=brief.safety.scope)
        deduper.load_global()

        if llm is None:
            llm = LLMClient()

        domains_csv = campaign_dir / "domains.csv"
        seen = _load_existing_domains(domains_csv)
        target = brief.target.target_domain_count
        total_cost = [0.0]

        queries = generate_queries(llm, brief, obs)
        if not queries:
            obs.finish("COMPLETED", {
                "rows": len(seen),
                "target": target,
                "queries_used": 0,
                "cost": total_cost[0],
                "note": "no queries generated",
            })
            return 0

        for q in queries:
            if len(seen) >= target:
                break
            if resume and progress.is_done(q.query):
                continue
            remaining = target - len(seen)
            process_query(
                llm, q, brief, deduper, seen, domains_csv, progress, obs,
                remaining=remaining, total_cost=total_cost,
            )

        summary = {
            "rows": len(seen),
            "target": target,
            "queries_used": len(queries),
            "cost": total_cost[0],
        }
        if len(seen) < target:
            obs.event(f"queries exhausted with {len(seen)}/{target} rows", level="warn")
            summary["note"] = "queries exhausted; target undermet"
        obs.finish("COMPLETED", summary)
        return 0
    except BriefValidationError as e:
        emit_brief_error_and_exit(e)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        if obs is not None:
            try:
                obs.finish("FAILED", {"error": repr(e)})
            except Exception:
                pass
        sys.stderr.write(f"source_domains failed: {type(e).__name__}: {e}\n")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1: domain sourcing")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    return _run(args.campaign_dir, args.resume)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
